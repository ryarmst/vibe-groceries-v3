"""PC ID sign-in (OAuth2 code + PKCE, the PC Express app's client) and access-token upkeep.

The sign-in page is behind bot detection, so a person signs in once in a real browser.
After that the server keeps itself signed in with refresh tokens over plain HTTPS.
Tokens never leave this module except as an Authorization header on PC Express calls.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import httpx

from shopping.pcid.store import SecretStore

# PC Express Android app's OAuth client (PC ID is Oracle IDCS). Fixed app constants; the secret comes from env.
CLIENT_ID = "ef9659ede6d44c7ab417f3485c11286c"
AUTHORIZE_URL = "https://accounts.pcid.ca/oauth2/v1/authorize"
TOKEN_URL = "https://accounts.pcid.ca/oauth2/v1/token"
REVOKE_URL = "https://accounts.pcid.ca/oauth2/v1/revoke"
# IDCS glues the audience "grocery-prod" onto "grocery-customer"; offline_access yields a refresh token.
SCOPE = "openid grocery-prodgrocery-customer offline_access"
REDIRECT_URI = "com.loblaw.pcx://pcx-android/login/appredirect"
PCID_HEADERS = {"source": "ANDROID", "relying-party": "pcexpress-android", "User-Agent": "okhttp/4.12.0"}

PENDING_TTL = 1800  # seconds a started sign-in stays valid
EARLY_REFRESH = 120  # refresh this long before the access token expires


class AuthError(Exception):
    pass


class NeedsLogin(AuthError):
    """No usable refresh token: someone has to sign in again."""


class Unavailable(AuthError):
    """PC ID didn't answer properly (network trouble or a 5xx); try again later."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def jwt_claims(token: str) -> dict:
    """Unverified claims, for display and the customer id only. Never used for trust decisions."""
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError):
        return {}


def code_from_redirect(text: str) -> tuple[str, str | None]:
    """Pull (code, state) from what the browser ended on: the com.loblaw.pcx:// redirect,
    PC ID's .../login/success?redirectURL=... page, or a bare code."""
    text = text.strip()
    if "code=" not in text and "redirectURL=" not in text:
        if not text or any(c in text for c in " /?&="):
            raise AuthError("That doesn't look like the sign-in redirect URL.")
        return text, None
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(text).query)
    if "redirectURL" in query:
        return code_from_redirect(query["redirectURL"][0])
    if "error" in query:
        raise AuthError(f"Sign-in failed: {query['error'][0]} {query.get('error_description', [''])[0]}".strip())
    if "code" not in query:
        raise AuthError("No authorization code in that URL.")
    return query["code"][0], query.get("state", [None])[0]


@dataclass
class Status:
    signed_in: bool
    customer_id: str | None = None
    access_expires_in: int | None = None
    signed_in_at: float | None = None
    last_refresh_at: float | None = None
    signed_in_by: str | None = None


class PcidAuth:
    def __init__(self, *, client_secret: str, state_dir: Path, key: bytes, client_id: str = CLIENT_ID,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.client_id, self.client_secret = client_id, client_secret
        self.tokens = SecretStore(state_dir / "pcid_tokens.bin", key)
        self.pending = SecretStore(state_dir / "pcid_pending.bin", key)
        self._transport = transport
        self._lock = asyncio.Lock()
        self._cache: dict | None = None  # in-memory copy of the token state

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), transport=self._transport)

    # --- sign-in ---------------------------------------------------------------------------

    def start_login(self) -> str:
        """Begin a sign-in; returns the URL to open in a browser."""
        verifier = _b64url(secrets.token_bytes(64))
        state = _b64url(secrets.token_bytes(24))
        self.pending.write({"verifier": verifier, "state": state, "created": time.time()})
        if not self.client_secret:
            raise AuthError("PCID_CLIENT_SECRET is not set on the server.")
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "scope": SCOPE,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
            "code_challenge_method": "S256",
            "state": state,
            "nonce": _b64url(secrets.token_bytes(16)),
        }
        return AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)

    async def finish_login(self, redirect: str, *, by: str | None = None) -> Status:
        pending = self.pending.read()
        if not pending or time.time() - pending.get("created", 0) > PENDING_TTL:
            self.pending.clear()
            raise AuthError("No sign-in in progress (or it expired). Start again.")
        code, state = code_from_redirect(redirect)
        if state is not None and not secrets.compare_digest(state, pending["state"]):
            raise AuthError("This redirect belongs to a different sign-in attempt. Start again.")
        data = await self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": pending["verifier"],
        })
        self.pending.clear()
        if not data.get("refresh_token"):
            raise AuthError("PC ID didn't return a refresh token.")
        async with self._lock:
            with self.tokens.locked():
                self._store(data, signed_in_at=time.time(), by=by)
        return self.status()

    async def logout(self) -> None:
        async with self._lock:
            with self.tokens.locked():
                state = self.tokens.read()
                self.tokens.clear()
                self._cache = None
        if state.get("refresh_token"):
            try:  # best effort: also end it at PC ID
                async with self._client() as c:
                    await c.post(REVOKE_URL, headers=self._headers(), data={
                        "token": state["refresh_token"], "token_type_hint": "refresh_token",
                        "client_id": self.client_id, "client_secret": self.client_secret})
            except httpx.HTTPError:
                pass

    # --- tokens ----------------------------------------------------------------------------

    async def access_token(self, *, rejected: str | None = None) -> str:
        """A valid access token, refreshing it (and persisting the rotated refresh token) when due.
        Pass `rejected` with a token the API just refused (401) to force a new one."""
        def usable(state: dict | None) -> bool:
            return bool(state and state.get("access_token") and state["access_token"] != rejected
                        and time.time() < state.get("expires_at", 0) - EARLY_REFRESH)

        async with self._lock:
            if usable(self._cache):
                return self._cache["access_token"]
            with self.tokens.locked():
                state = self.tokens.read()  # another process may have refreshed already
                if not state.get("refresh_token"):
                    raise NeedsLogin("No Frills isn't signed in.")
                if usable(state):
                    self._cache = state
                    return state["access_token"]
                data = await self._token_request({"grant_type": "refresh_token",
                                                   "refresh_token": state["refresh_token"]})
                return self._store(data, prev=state)["access_token"]

    def status(self) -> Status:
        state = self.tokens.read()
        if not state.get("refresh_token"):
            return Status(False)
        return Status(True, customer_id=state.get("customer_id"),
                      access_expires_in=max(0, int(state.get("expires_at", 0) - time.time())),
                      signed_in_at=state.get("signed_in_at"), last_refresh_at=state.get("refreshed_at"),
                      signed_in_by=state.get("signed_in_by"))

    def _store(self, data: dict, prev: dict | None = None, signed_in_at: float | None = None, by: str | None = None) -> dict:
        prev = prev or {}
        claims = jwt_claims(data["access_token"])
        state = {
            "access_token": data["access_token"],
            "expires_at": time.time() + int(data.get("expires_in") or 3600),
            # Refresh tokens rotate: keep the new one; only keep the old if none came back.
            "refresh_token": data.get("refresh_token") or prev.get("refresh_token"),
            "customer_id": claims.get("sub") or prev.get("customer_id"),
            "signed_in_at": signed_in_at or prev.get("signed_in_at"),
            "signed_in_by": by or prev.get("signed_in_by"),
            "refreshed_at": time.time(),
        }
        self.tokens.write(state)  # persisted before anyone uses the new token
        self._cache = state
        return state

    def _headers(self) -> dict:
        return {"Accept": "application/json", **PCID_HEADERS}

    async def _token_request(self, fields: dict) -> dict:
        if not self.client_secret:
            raise AuthError("PCID_CLIENT_SECRET is not set on the server.")
        body = {**fields, "client_id": self.client_id, "client_secret": self.client_secret}
        try:
            async with self._client() as c:
                r = await c.post(TOKEN_URL, data=body, headers=self._headers())
        except httpx.HTTPError as exc:
            raise Unavailable(f"PC ID unreachable ({type(exc).__name__}).") from exc
        if r.status_code >= 500:
            raise Unavailable(f"PC ID is having trouble (HTTP {r.status_code}); try again shortly.")
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code == 200 and data.get("access_token"):
            return data
        err = data.get("error") or f"HTTP {r.status_code}"
        if err == "invalid_grant":
            if fields["grant_type"] == "refresh_token":
                raise NeedsLogin("The No Frills sign-in has ended.")
            raise AuthError("PC ID rejected the sign-in code (expired or already used). Start again.")
        if err == "invalid_client":
            raise AuthError("PC ID rejected the app client (PCID_CLIENT_SECRET wrong or rotated).")
        raise AuthError(f"PC ID token request failed: {err} {str(data.get('error_description', ''))[:200]}".strip())
