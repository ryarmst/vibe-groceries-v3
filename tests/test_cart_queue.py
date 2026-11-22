import asyncio

from shopping.core.cart_queue import CartQueue


async def test_queue_serializes_dedupes_and_reports(monkeypatch):
    q = CartQueue()
    seen, gate = [], asyncio.Event()

    async def fake_process(self, run_id, ids, source):
        seen.append(list(ids))
        await gate.wait()
        self._event(run_id, True, f"added {len(ids)}")

    monkeypatch.setattr(CartQueue, "_process", fake_process)
    q.start()
    try:
        assert q.enqueue("r1", ["a", "b"]) == 2
        assert q.enqueue("r1", ["b", "c"]) == 1  # b is already queued
        await asyncio.sleep(0)
        assert q.pending("r1") == {"a", "b", "c"} and q.pending("other") == set()
        gate.set()
        for _ in range(20):
            if not q.pending():
                break
            await asyncio.sleep(0.01)
        assert seen == [["a", "b"], ["c"]] and not q.pending()
        assert [e.message for e in q.events_since(0)] == ["added 2", "added 1"]
        assert q.events_since(q.last_event_id) == []
    finally:
        await q.stop()


async def test_worker_survives_a_crashing_job(monkeypatch):
    q = CartQueue()

    async def boom(self, run_id, ids, source):
        raise ValueError("bad")

    monkeypatch.setattr(CartQueue, "_process", boom)
    q.start()
    try:
        q.enqueue("r", ["x"])
        for _ in range(20):
            if not q.pending():
                break
            await asyncio.sleep(0.01)
        assert not q.pending() and q.events_since(0)[0].ok is False
        q.enqueue("r", ["x"])  # worker still alive and the item can be retried
        await asyncio.sleep(0.05)
        assert len(q.events_since(0)) == 2
    finally:
        await q.stop()
