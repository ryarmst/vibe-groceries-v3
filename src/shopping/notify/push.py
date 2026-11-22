"""Push notifications through ntfy (https://ntfy.sh, free iOS/Android apps, or a self-hosted server).

The topic URL (e.g. https://ntfy.sh/vibe-groceries-k3j9…) is the only "secret": anyone who knows it can read
the alerts, so it lives in the vault and should be long and random. Off until it's set.
"""

from __future__ import annotations

import logging

import httpx

from shopping import context
from shopping.database import session_scope

log = logging.getLogger(__name__)
SECRET = "notify:ntfy"


class NotifyError(Exception):
    pass


def normalize_topic(value: str | None) -> str | None:
    """Accept a bare topic name, "ntfy.sh/name" or a full https URL. Plain http is refused (the topic is a secret)."""
    v = (value or "").strip()
    if not v:
        return None
    if v.startswith("http://"):
        raise NotifyError("Use https:// for the ntfy topic (the topic name works like a password).")
    if v.startswith("https://"):
        return v
    if "/" in v:
        return "https://" + v
    return "https://ntfy.sh/" + v


def topic_url() -> str | None:
    with session_scope() as session:
        return normalize_topic(context.vault.get(session, SECRET))


async def send(title: str, message: str, *, click: str | None = None, priority: int = 4,
               transport: httpx.AsyncBaseTransport | None = None) -> bool:
    """Returns False (quietly) when no topic is configured."""
    url = topic_url()
    if not url:
        return False
    headers = {"Title": title.encode("utf-8").decode("latin-1", "ignore")[:200], "Priority": str(priority), "Tags": "shopping_cart"}
    if click and click.startswith("https://"):
        headers["Click"] = click
    async with httpx.AsyncClient(timeout=15, transport=transport) as c:
        r = await c.post(url, content=message.encode(), headers=headers)
    if r.status_code >= 400:
        raise NotifyError(f"ntfy refused the message ({r.status_code}).")
    return True
