"""Application entry point: the web app and its JSON API, background jobs, and (optionally) MCP."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import socket

from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse

from shopping import context
from shopping.api import chat_routes, inbox_routes, meals_routes, retailer_routes, settings_routes, shop_routes
from shopping.api.deps import install_error_handlers
from shopping.config import settings
from shopping.core.cart_queue import queue as cart_queue
from shopping.database import run_migrations
from shopping.jobs import scheduler
from shopping.logging_setup import setup_logging

setup_logging(settings.log_level)
log = logging.getLogger("shopping")


class MCPBearerAuth:
    """Shared-secret bearer token for the optional MCP endpoint (MCP_ENABLED=1 and MCP_TOKEN set)."""

    def __init__(self, app, token: str):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            supplied = headers.get(b"authorization", b"").decode()
            if not self.token or not hmac.compare_digest(supplied, f"Bearer {self.token}"):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


WEB = Path(__file__).parent / "web"
# Scripts and styles only from this app; product images come from the retailer's CDN; no framing, no plugins.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https: data:; "
       "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


class SecurityHeaders:
    """No caching, no MIME sniffing, no framing, and a strict CSP for the web app."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers += [(b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                            (b"x-frame-options", b"DENY"), (b"referrer-policy", b"no-referrer"),
                            (b"content-security-policy", CSP.encode()),
                            (b"permissions-policy", b"camera=(), microphone=(), geolocation=()")]
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


mcp = None
if settings.mcp_enabled:
    from mcp.server.transport_security import TransportSecuritySettings

    from shopping.mcp_server.server import mcp

    hosts = [x for h in settings.allowed_hosts for x in (h, f"{h}:*")]
    origins = [f"{s}://{h}{p}" for h in settings.allowed_hosts for s in ("http", "https") for p in ("", ":*")]
    mcp_app = mcp.streamable_http_app(streamable_http_path="/", host=settings.host.split(",")[0].strip(),
                                      transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                                                                   allowed_hosts=hosts, allowed_origins=origins))


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    run_migrations()
    if not settings.pcx_api_key:
        log.warning("PCX_API_KEY is not set; retailer calls will fail (see SETUP.md).")
    if not settings.store_id:
        log.warning("STORE_ID is not set; prices and deals need your store (see SETUP.md).")
    cart_queue.start()
    tasks = []
    if settings.jobs_enabled:  # includes the daily deal scan + assessment (DEAL_SCAN_HOURS=0 turns that part off)
        tasks.append(asyncio.create_task(scheduler.run_forever(), name="jobs"))
    async with (mcp.session_manager.run() if mcp else contextlib.nullcontext()):
        log.info("vibe-groceries ready: store=%s data=%s access=%s mcp=%s", settings.store_id, settings.data_dir,
                 settings.access_team or ("DEV_USER" if settings.dev_user else "NOT CONFIGURED"), bool(mcp))
        yield
    for t in tasks:
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t
    await cart_queue.stop()
    await context.shutdown()


app = FastAPI(title="vibe-groceries", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
install_error_handlers(app)
for r in (settings_routes, retailer_routes, inbox_routes, shop_routes, chat_routes, meals_routes):
    app.include_router(r.router)
if mcp:
    app.mount("/mcp", MCPBearerAuth(mcp_app, settings.mcp_token))
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts + ["testserver"])
app.add_middleware(SecurityHeaders)


app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/", include_in_schema=False)
def index():
    # The page itself holds no data; every /api call is checked against Cloudflare Access.
    return FileResponse(WEB / "index.html")


def _listen_sockets() -> list[socket.socket]:
    """HOST may list several addresses (comma-separated) so the app can listen on specific interfaces."""
    socks = []
    for host in [h.strip() for h in settings.host.split(",") if h.strip()]:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        s = socket.socket(family, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, settings.port))
        s.set_inheritable(True)
        socks.append(s)
        log.info("listening on http://%s:%s", f"[{host}]" if family == socket.AF_INET6 else host, settings.port)
    return socks


def main() -> None:
    config = uvicorn.Config("shopping.app:app", port=settings.port, log_config=None, proxy_headers=True,
                            forwarded_allow_ips=settings.trusted_proxies or "127.0.0.1")
    uvicorn.Server(config).run(sockets=_listen_sockets())


if __name__ == "__main__":
    main()
