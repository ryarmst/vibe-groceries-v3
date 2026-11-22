"""Who's calling: Cloudflare Access.

Access sits in front of the app and signs every request it lets through with a JWT
(`Cf-Access-Jwt-Assertion` header, also the `CF_Authorization` cookie). We verify that JWT
ourselves (RS256 against the team's published keys, audience, issuer, expiry), then require the
email to be one of ALLOWED_USERS. So a misconfigured Access policy, or a request that reaches the
app some other way, still gets nothing.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
from dataclasses import dataclass

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

log = logging.getLogger(__name__)

LEEWAY = 60  # seconds of clock skew allowed on exp/nbf/iat
KEYS_TTL = 3600  # re-fetch the team's signing keys hourly (and on an unknown kid)


class AccessDenied(Exception):
    pass


@dataclass(frozen=True)
class AccessUser:
    id: str  # stable id derived from the email (a row in `users`)
    email: str
    name: str


def user_id(email: str) -> str:
    return "u" + hashlib.sha256(email.lower().encode()).hexdigest()[:31]


def _b64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _int(part: str) -> int:
    return int.from_bytes(_b64(part), "big")


class AccessVerifier:
    def __init__(self, team_domain: str, audience: str, allowed: dict[str, str],
                 transport: httpx.AsyncBaseTransport | None = None):
        self.issuer = f"https://{team_domain}"
        self.certs_url = f"{self.issuer}/cdn-cgi/access/certs"
        self.audience = audience
        self.allowed = {k.lower(): v for k, v in allowed.items()}
        self._transport = transport
        self._keys: dict[str, rsa.RSAPublicKey] = {}
        self._fetched = 0.0
        self._lock = asyncio.Lock()

    async def _load_keys(self, force: bool = False) -> None:
        async with self._lock:
            if not force and self._keys and time.time() - self._fetched < KEYS_TTL:
                return
            if force and time.time() - self._fetched < 30:
                return  # don't hammer the endpoint on a stream of bad kids
            async with httpx.AsyncClient(timeout=10, transport=self._transport) as c:
                r = await c.get(self.certs_url)
            r.raise_for_status()
            keys = {}
            for k in r.json().get("keys", []):
                if k.get("kty") == "RSA" and k.get("kid"):
                    keys[k["kid"]] = rsa.RSAPublicNumbers(_int(k["e"]), _int(k["n"])).public_key()
            if not keys:
                raise AccessDenied("Cloudflare Access published no signing keys.")
            self._keys, self._fetched = keys, time.time()

    async def verify(self, token: str) -> AccessUser:
        try:
            h64, p64, s64 = token.split(".")
            header, claims, sig = json.loads(_b64(h64)), json.loads(_b64(p64)), _b64(s64)
        except ValueError as exc:
            raise AccessDenied("Malformed Access token.") from exc
        if header.get("alg") != "RS256":
            raise AccessDenied("Unexpected Access token algorithm.")
        try:
            await self._load_keys()
            key = self._keys.get(header.get("kid"))
            if key is None:
                await self._load_keys(force=True)  # keys rotate
                key = self._keys.get(header.get("kid"))
        except httpx.HTTPError as exc:
            log.warning("couldn't fetch Cloudflare Access keys: %s", exc)
            raise AccessDenied("Can't verify sign-in right now (Access keys unavailable).") from exc
        if key is None:
            raise AccessDenied("Access token signed by an unknown key.")
        try:
            key.verify(sig, f"{h64}.{p64}".encode(), padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature as exc:
            raise AccessDenied("Access token signature is invalid.") from exc

        now = time.time()
        aud = claims.get("aud")
        if self.audience not in (aud if isinstance(aud, list) else [aud]):
            raise AccessDenied("Access token is for a different application.")
        if claims.get("iss") != self.issuer:
            raise AccessDenied("Access token from an unexpected issuer.")
        if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < now - LEEWAY:
            raise AccessDenied("Access sign-in expired; reload the page.")
        if claims.get("nbf", 0) > now + LEEWAY:
            raise AccessDenied("Access token not valid yet.")
        email = str(claims.get("email") or "").lower()
        if not email or email not in self.allowed:
            raise AccessDenied("This account isn't allowed to use the app.")
        return AccessUser(user_id(email), email, self.allowed[email])
