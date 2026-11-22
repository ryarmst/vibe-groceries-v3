"""Assistant replies run on the server, independent of the browser connection.

A reply (the agent loop for one message or confirmation) runs as a background task that records its events.
The page streams those events, and can reconnect after a dropped connection, a locked phone or a page change
and pick up where it left off. Leaving the page never cuts a reply short (e.g. a Quick Shop half-way through
adding items). One reply runs per conversation at a time.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import AsyncIterator

log = logging.getLogger(__name__)

KEEP_FINISHED = 600  # seconds a finished reply's events stay available for a late reconnect


def _readable(exc: Exception) -> str:
    """Our own errors are written for people (e.g. "Notion isn't connected…"); anything else gets a generic line."""
    from shopping.agent.providers.base import ProviderError
    from shopping.auth.session import AuthRequired, SessionUnavailable
    from shopping.core.service import ShoppingError
    from shopping.notion.client import NotionError
    from shopping.retailer.client import RetailerError
    from shopping.secrets.vault import VaultError

    if isinstance(exc, (ProviderError, AuthRequired, SessionUnavailable, ShoppingError, NotionError, RetailerError, VaultError)):
        return str(exc)
    return "Something went wrong on the server. Try again, and check the server log if it keeps happening."


class Busy(Exception):
    """The conversation is already working on a reply."""


@dataclass
class Reply:
    thread_id: str
    events: list[dict] = field(default_factory=list)
    done: bool = False
    finished_at: float | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None

    def add(self, ev: dict) -> None:
        self.events.append(ev)
        self.changed.set()


_replies: dict[str, Reply] = {}


def _prune() -> None:
    now = time.time()
    for tid, r in list(_replies.items()):
        if r.done and r.finished_at and now - r.finished_at > KEEP_FINISHED:
            del _replies[tid]


def running(thread_id: str) -> bool:
    r = _replies.get(thread_id)
    return bool(r and not r.done)


def start(thread_id: str, events: AsyncIterator[dict]) -> Reply:
    """Run `events` (the agent loop) in the background, recording what it yields."""
    _prune()
    if running(thread_id):
        raise Busy("The assistant is still working on this conversation.")
    reply = Reply(thread_id)

    async def run() -> None:
        try:
            async for ev in events:
                reply.add(ev)
        except asyncio.CancelledError:
            reply.add({"type": "error", "message": "Stopped."})
            raise
        except Exception as exc:  # noqa: BLE001 - reported to the page, logged here
            log.exception("assistant reply failed thread=%s", thread_id)
            reply.add({"type": "error", "message": _readable(exc)})
        finally:
            if not any(e.get("type") == "done" for e in reply.events[-1:]):
                reply.add({"type": "done", "awaiting_confirmation": False})
            reply.done, reply.finished_at = True, time.time()
            reply.changed.set()

    def finished(task: asyncio.Task) -> None:
        # Also covers a task cancelled before it started, when run()'s own cleanup never ran.
        if not reply.done:
            reply.add({"type": "error", "message": "Stopped."})
            reply.add({"type": "done", "awaiting_confirmation": False})
            reply.done, reply.finished_at = True, time.time()

    reply.task = asyncio.create_task(run(), name=f"reply-{thread_id}")
    reply.task.add_done_callback(finished)
    _replies[thread_id] = reply
    return reply


def stop(thread_id: str) -> bool:
    r = _replies.get(thread_id)
    if r and not r.done and r.task:
        r.task.cancel()
        return True
    return False


async def follow(thread_id: str, after: int = 0) -> AsyncIterator[dict]:
    """The reply's events from index `after` on, live, until it's done. Each event carries its index (`seq`)."""
    reply = _replies.get(thread_id)
    if reply is None:
        yield {"type": "done", "awaiting_confirmation": False, "seq": after, "idle": True}
        return
    i = max(0, after)
    while True:
        while i < len(reply.events):
            yield {**reply.events[i], "seq": i + 1}
            i += 1
        if reply.done:
            return
        reply.changed.clear()
        if i < len(reply.events):
            continue
        try:
            await asyncio.wait_for(reply.changed.wait(), timeout=15)
        except asyncio.TimeoutError:
            yield {"type": "ping"}  # keeps proxies from closing a quiet connection
