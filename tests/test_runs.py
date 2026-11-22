import asyncio

import pytest

from shopping.agent import runs


async def slow_reply(n=3, gate: asyncio.Event | None = None):
    for i in range(n):
        if gate is not None and i == 1:
            await gate.wait()
        yield {"type": "text", "text": f"part {i}"}
    yield {"type": "done", "awaiting_confirmation": False}


async def test_reply_finishes_after_the_page_disconnects():
    gate = asyncio.Event()
    runs.start("t-dis", slow_reply(gate=gate))
    follower = runs.follow("t-dis")
    first = await follower.__anext__()
    assert first == {"type": "text", "text": "part 0", "seq": 1}
    await follower.aclose()  # the browser went away
    gate.set()
    for _ in range(50):
        if not runs.running("t-dis"):
            break
        await asyncio.sleep(0.01)
    assert not runs.running("t-dis")
    rest = [ev async for ev in runs.follow("t-dis", after=1)]  # reconnect where we left off
    assert [e.get("text") for e in rest] == ["part 1", "part 2", None] and rest[-1]["type"] == "done"
    assert [e["seq"] for e in rest] == [2, 3, 4]


async def test_one_reply_per_conversation_and_stop():
    gate = asyncio.Event()
    runs.start("t-busy", slow_reply(gate=gate))
    with pytest.raises(runs.Busy):
        runs.start("t-busy", slow_reply())
    assert runs.stop("t-busy")
    events = [ev async for ev in runs.follow("t-busy")]
    assert events[-1]["type"] == "done" and any(e.get("message") == "Stopped." for e in events)


async def test_follow_with_nothing_running_says_idle():
    events = [ev async for ev in runs.follow("never-started")]
    assert events == [{"type": "done", "awaiting_confirmation": False, "seq": 0, "idle": True}]


async def test_a_crash_is_reported_not_hung():
    async def broken():
        yield {"type": "text", "text": "hi"}
        raise RuntimeError("boom")
    runs.start("t-crash", broken())
    events = [ev async for ev in runs.follow("t-crash")]
    assert [e["type"] for e in events] == ["text", "error", "done"]
