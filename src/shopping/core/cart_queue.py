"""Background cart updates.

Adding approved items to the No Frills cart can take several seconds per item. Approving in the web UI
records the decision and returns at once; this single worker then adds the items one job at a time
(which also keeps cart writes from racing each other) and reports what happened as events the page polls.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections import deque
from dataclasses import dataclass, field

from shopping.models import utcnow

log = logging.getLogger(__name__)


@dataclass
class CartEvent:
    id: int
    run_id: str
    ok: bool
    message: str
    at: str = field(default_factory=lambda: utcnow().isoformat())


class CartQueue:
    def __init__(self) -> None:
        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None
        self._pending: dict[str, str] = {}  # selection id -> run id, while queued or running
        self._events: deque[CartEvent] = deque(maxlen=50)
        self._ids = itertools.count(1)

    # -- lifecycle -------------------------------------------------------------------------
    def start(self) -> None:
        self._queue = asyncio.Queue()
        self._task = asyncio.create_task(self._worker(), name="cart-queue")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = self._queue = None

    # -- API -------------------------------------------------------------------------------
    def enqueue(self, run_id: str, selection_ids: list[str], *, source: str = "approve") -> int:
        ids = [s for s in selection_ids if s not in self._pending]
        if not ids:
            return 0
        if self._queue is None:
            raise RuntimeError("cart queue is not running")
        for s in ids:
            self._pending[s] = run_id
        self._queue.put_nowait((run_id, ids, source))
        return len(ids)

    def pending(self, run_id: str | None = None) -> set[str]:
        return {s for s, r in self._pending.items() if run_id is None or r == run_id}

    def events_since(self, after: int = 0) -> list[CartEvent]:
        return [e for e in self._events if e.id > after]

    @property
    def last_event_id(self) -> int:
        return self._events[-1].id if self._events else 0

    def _event(self, run_id: str, ok: bool, message: str) -> None:
        self._events.append(CartEvent(next(self._ids), run_id, ok, message))

    # -- worker ----------------------------------------------------------------------------
    async def _worker(self) -> None:
        while True:
            run_id, ids, source = await self._queue.get()
            try:
                await self._process(run_id, ids, source)
            except Exception as exc:  # noqa: BLE001 - keep the worker alive
                log.exception("cart queue job failed run=%s", run_id)
                self._event(run_id, False, f"Couldn't add to the cart: {exc}")
            finally:
                for s in ids:
                    self._pending.pop(s, None)
                self._queue.task_done()

    async def _process(self, run_id: str, ids: list[str], source: str) -> None:
        from shopping import context
        from shopping.core import service
        from shopping.database import session_scope

        with session_scope() as session:
            result = await service.apply_approved(session, context.client(), run_id, only_ids=ids, source=source)
        if result.get("message"):
            return  # nothing left to add (e.g. already applied)
        if result["applied"]:
            names = ", ".join(r["name"] for r in result["results"] if r["ok"])
            self._event(run_id, True, f"Added to the cart: {names}.")
        if result["failed"]:
            self._event(run_id, False, f"{result['failed']} item(s) couldn't be added: {result['error']}")


queue = CartQueue()
