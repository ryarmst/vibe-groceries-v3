"""The No Frills (PC ID) session, as the rest of the app sees it.

Someone in the household signs in to PC ID once in a browser (Settings → Sign in to PC ID). From then
on `PcidAuth` keeps the session alive with refresh tokens over plain HTTPS. The rest of the
application only uses `status()`, `ensure_authenticated()` and `get_headers()`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

from shopping.pcid.oauth import AuthError, NeedsLogin, PcidAuth, Unavailable


class AuthRequired(Exception):
    """Raised when an operation needs a retailer login and no usable session exists."""


class SessionUnavailable(Exception):
    """PC ID couldn't be reached to renew the session; nothing is wrong with the sign-in itself."""


@dataclass
class AuthStatus:
    state: str  # "connected" | "required"
    expires_at: datetime | None  # the current access token's expiry (renewed automatically)
    seconds_remaining: int | None
    last_updated: datetime | None  # last renewal
    customer_id: str | None
    signed_in_at: datetime | None = None
    signed_in_by: str | None = None

    @property
    def label(self) -> str:
        return {"connected": "Connected", "required": "Sign-in required"}[self.state]


def _dt(ts: float | None) -> datetime | None:
    return datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None


class AuthSession:
    def __init__(self, pcid: PcidAuth):
        self.pcid = pcid
        self._last_token: str | None = None

    def status(self) -> AuthStatus:
        st = self.pcid.status()
        if not st.signed_in:
            return AuthStatus("required", None, None, None, None)
        return AuthStatus("connected", _dt(time.time() + (st.access_expires_in or 0)), st.access_expires_in,
                          _dt(st.last_refresh_at), st.customer_id, _dt(st.signed_in_at), st.signed_in_by)

    async def ensure_authenticated(self) -> str:
        """Return the customer id (PC id) for the signed-in account, or raise AuthRequired."""
        await self._token()
        return self.pcid.status().customer_id or ""

    async def get_headers(self) -> dict[str, str]:
        return {"authorization": "Bearer " + await self._token(), "is-helios-account": "true"}

    async def token_rejected(self) -> None:
        """The retailer refused the last token (401): renew it before the caller retries."""
        if self._last_token:
            await self._token(rejected=self._last_token)

    async def clear(self) -> None:
        await self.pcid.logout()

    async def _token(self, rejected: str | None = None) -> str:
        try:
            self._last_token = await self.pcid.access_token(rejected=rejected)
        except NeedsLogin as exc:
            raise AuthRequired(f"{exc} Sign in under Settings → No Frills account.") from exc
        except Unavailable as exc:
            raise SessionUnavailable(str(exc)) from exc
        except AuthError as exc:
            raise AuthRequired(str(exc)) from exc
        return self._last_token
