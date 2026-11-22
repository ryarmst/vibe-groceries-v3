"""Process-wide singletons: the secrets vault, the No Frills (PC ID) session, the retailer client."""

from __future__ import annotations

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from shopping.auth.session import AuthSession
from shopping.config import settings
from shopping.pcid.oauth import PcidAuth
from shopping.retailer.client import PCExpressClient
from shopping.secrets.vault import Vault, load_master_key

PUBLIC_URL = settings.public_url

_master = load_master_key(settings.master_key_b64, settings.master_key_file)
vault = Vault(_master)


def _subkey(label: bytes) -> bytes:
    """A separate key per purpose, derived from MASTER_KEY."""
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"vibe-groceries:" + label).derive(_master)


pcid = PcidAuth(client_secret=settings.pcid_client_secret, state_dir=settings.pcid_dir, key=_subkey(b"pcid-tokens"))
auth = AuthSession(pcid)
_client: PCExpressClient | None = None


def client() -> PCExpressClient:
    global _client
    if _client is None:
        _client = PCExpressClient(
            api_base=settings.pcx_api_base,
            api_key=settings.pcx_api_key,
            banner=settings.banner,
            store_id=settings.store_id,
            auth=auth,
            raw_dir=settings.raw_dir,
        )
    return _client


async def shutdown() -> None:
    if _client is not None:
        await _client.aclose()
