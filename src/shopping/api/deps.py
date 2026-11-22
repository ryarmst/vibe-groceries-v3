"""Request plumbing: who's calling (Cloudflare Access), CSRF, and errors as JSON."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from shopping import attribution
from shopping.appsettings import SettingsError
from shopping.auth.access import AccessDenied, AccessVerifier, User, user_id
from shopping.auth.ratelimit import RateLimiter
from shopping.auth.session import AuthRequired, SessionUnavailable
from shopping.config import settings
from shopping.core.service import ShoppingError
from shopping.database import session_scope
from shopping.models import Device, utcnow
from shopping.retailer.client import RetailerError, RetailerUnavailable
from shopping.notion.client import NotionError
from shopping.secrets.vault import VaultError

log = logging.getLogger(__name__)

api_limiter = RateLimiter(limit=600, window_seconds=60)  # per user
CSRF_HEADER = "x-vibe"  # the web app sends it on every call; a cross-site form or image can't

_last_seen_write: dict[str, float] = {}
_verifier: AccessVerifier | None = None


@dataclass(frozen=True)
class Caller:
    device_id: str  # the user's id (stored in the `devices` table)
    name: str
    email: str = ""


def client_ip(request: Request) -> str:
    """For logs and limits. Behind Cloudflare use its CF-Connecting-IP (set by Cloudflare, can't be spoofed)."""
    if settings.client_ip_header and request.headers.get(settings.client_ip_header):
        return request.headers[settings.client_ip_header].strip()
    return request.client.host if request.client else "unknown"


def verifier() -> AccessVerifier:
    global _verifier
    if _verifier is None:
        if not (settings.access_team and settings.access_aud):
            raise HTTPException(503, "Cloudflare Access isn't configured (ACCESS_TEAM_DOMAIN / ACCESS_AUD).")
        _verifier = AccessVerifier(settings.access_team, settings.access_aud, settings.allowed_users)
    return _verifier


def dev_mode() -> bool:
    """DEV_USER skips Access, but only for a server bound to loopback (never behind the tunnel)."""
    hosts = {h.strip() for h in settings.host.split(",") if h.strip()}
    return bool(settings.dev_user) and hosts <= {"127.0.0.1", "::1", "localhost"}


async def current_user(request: Request) -> User:
    if dev_mode():
        email = settings.dev_user.lower()
        return User(user_id(email), email, settings.allowed_users.get(email, email.split("@")[0]))
    token = request.headers.get("cf-access-jwt-assertion") or request.cookies.get("CF_Authorization") or ""
    if not token:
        raise HTTPException(401, "Not signed in through Cloudflare Access.")
    try:
        return await verifier().verify(token)
    except AccessDenied as exc:
        raise HTTPException(403 if "allowed" in str(exc) else 401, str(exc)) from exc


def check_csrf(request: Request) -> None:
    """State-changing calls must come from the app's own pages: Access's cookie rides along on any request
    to the host, so require a custom header (forces a CORS preflight cross-site) and a same-origin Origin."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if request.headers.get(CSRF_HEADER) != "1":
        raise HTTPException(403, "Missing app header.")
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host", ""):
        raise HTTPException(403, "Cross-site request refused.")


async def require_user(request: Request) -> Caller:
    """FastAPI dependency: a Cloudflare Access user on the allow-list, calling from the app itself."""
    user = await current_user(request)
    check_csrf(request)
    if not api_limiter.allow(user.id):
        raise HTTPException(429, "Slow down.")
    now = time.time()
    if now - _last_seen_write.get(user.id, 0) > 300:  # don't write on every request
        with session_scope() as session:
            row = session.get(Device, user.id)
            if row is None:
                row = Device(id=user.id, name=user.name)
                session.add(row)
            row.name, row.last_seen_at = user.name, utcnow()
        _last_seen_write[user.id] = now
    attribution.current_device.set(user.id)
    return Caller(user.id, user.name, user.email)


def install_error_handlers(app) -> None:
    def err(status: int, message: str, code: str | None = None):
        return JSONResponse({"error": message, **({"code": code} if code else {})}, status_code=status)

    @app.exception_handler(HTTPException)
    async def _http(_, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(AuthRequired)
    async def _needs_session(_, exc):
        return err(409, str(exc), "needs_session")

    @app.exception_handler(SessionUnavailable)
    async def _session_down(_, exc):
        return err(503, str(exc), "retailer_unavailable")

    @app.exception_handler(RetailerUnavailable)
    async def _unavailable(_, exc):
        return err(503, str(exc), "retailer_unavailable")

    @app.exception_handler(RetailerError)
    async def _retailer(_, exc):
        return err(502, str(exc), "retailer_error")

    @app.exception_handler(ShoppingError)
    async def _shopping(_, exc):
        return err(400, str(exc))

    @app.exception_handler(SettingsError)
    async def _settings(_, exc):
        return err(400, str(exc))

    @app.exception_handler(VaultError)
    async def _vault(_, exc):
        return err(400, str(exc))

    @app.exception_handler(NotionError)
    async def _notion(_, exc):
        return err(502 if (exc.status or 0) >= 500 else 400, str(exc), "notion_error")
