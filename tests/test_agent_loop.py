import json

import pytest

from shopping import appsettings
from shopping.agent import loop
from shopping.agent.providers import registry as providers
from shopping.agent.providers.base import Done, ProviderError, TextDelta, ToolCall, Usage
from shopping.agent.setup import ensure_tools
from shopping.agent.tools import registry
from shopping.database import session_scope
from shopping.models import AgentAudit, ChatMessage, ChatThread, LLMUsage


class FakeProvider:
    """Plays back scripted turns; records what it was sent."""

    def __init__(self, turns, name="gemini", fail_first: ProviderError | None = None, fail_always: ProviderError | None = None):
        self.turns, self.name, self.sent, self.fail_first, self.fail_always = list(turns), name, [], fail_first, fail_always

    async def stream(self, *, system, messages, tools, model, temperature, max_tokens):
        self.sent.append({"system": system, "messages": messages, "tools": [t.name for t in tools]})
        if self.fail_always:
            raise self.fail_always
        if self.fail_first:
            err, self.fail_first = self.fail_first, None
            raise err
        for ev in self.turns.pop(0):
            yield ev
        yield Usage(100, 20)
        yield Done()

    async def list_models(self):
        return []


@pytest.fixture
def tools(monkeypatch):
    ensure_tools()
    calls = {"pantry": [], "update": []}

    async def fake_pantry(args, ctx):
        calls["pantry"].append(args)
        return {"rows": [{"id": "p1", "Name": "Eggs", "Expiration": "2026-11-22"}]}

    async def fake_update(args, ctx):
        calls["update"].append(args)
        return {"results": [{"updated": "p1"}]}

    monkeypatch.setattr(registry.get("notion_pantry"), "run", fake_pantry)
    monkeypatch.setattr(registry.get("pantry_update"), "run", fake_update)
    return calls


def new_thread():
    with session_scope() as s:
        t = ChatThread()
        s.add(t)
        s.flush()
        return t.id


async def collect(gen):
    return [e async for e in gen]


def use(monkeypatch, *providers_):
    by_name = {p.name: p for p in providers_}
    monkeypatch.setattr(providers, "build", lambda session, vault, name, transport=None: by_name[name])


async def test_read_tool_then_answer(monkeypatch, tools):
    fake = FakeProvider([[ToolCall("c1", "notion_pantry", {"query": "eggs"})], [TextDelta("You have eggs until Nov 22.")]])
    use(monkeypatch, fake)
    tid = new_thread()
    evs = await collect(loop.run(tid, user_text="do we have eggs in the fridge?", device_name="Sam"))
    assert evs[0] == {"type": "mode", "mode": "pantry", "title": "Pantry"}
    assert tools["pantry"] == [{"query": "eggs"}]
    assert "".join(e["text"] for e in evs if e["type"] == "text") == "You have eggs until Nov 22."
    assert evs[-1] == {"type": "done", "awaiting_confirmation": False}
    # Second call saw the tool result; system prompt names the person; tools limited to the playbook.
    second = fake.sent[1]["messages"]
    assert second[-1]["role"] == "tool" and "Eggs" in second[-1]["content"]
    assert "Sam" in fake.sent[0]["system"] and "shopping_create_run" not in fake.sent[0]["tools"]
    with session_scope() as s:
        assert s.query(LLMUsage).count() >= 2 and s.get(ChatThread, tid).title.startswith("do we have eggs")


async def test_confirm_tier_pauses_then_resumes(monkeypatch, tools):
    args = {"items": [{"page_id": "p1", "name": "Eggs", "values": {"Expiration": ""}}]}
    fake = FakeProvider([[TextDelta("I'll clear it."), ToolCall("c1", "pantry_update", args)], [TextDelta("Done — eggs cleared.")]])
    use(monkeypatch, fake)
    tid = new_thread()
    evs = await collect(loop.run(tid, user_text="we used up the eggs"))
    confirm = next(e for e in evs if e["type"] == "confirm")
    assert "Eggs: Expiration = (clear)" in confirm["preview"] and tools["update"] == []
    assert evs[-1]["awaiting_confirmation"] is True
    # Nothing runs again until a person answers.
    assert (await collect(loop.run(tid)))[-1]["awaiting_confirmation"] is True
    evs2 = await collect(loop.resolve_confirmation(tid, confirm["message_id"], approve=True, note=None, device_id="dev1"))
    assert tools["update"] == [args]
    assert "".join(e["text"] for e in evs2 if e["type"] == "text") == "Done — eggs cleared."
    with session_scope() as s:
        m = s.get(ChatMessage, confirm["message_id"])
        assert m.status == "done" and m.device_id == "dev1"
        assert {a.outcome for a in s.query(AgentAudit).filter_by(tool="pantry_update")} == {"pending", "ok"}


async def test_declined_and_superseded_confirmations(monkeypatch, tools):
    args = {"items": [{"page_id": "p1", "values": {"Expiration": ""}}]}
    fake = FakeProvider([[ToolCall("c1", "pantry_update", args)], [TextDelta("OK, left it.")],
                         [ToolCall("c2", "pantry_update", args)], [TextDelta("Fine.")]])
    use(monkeypatch, fake)
    tid = new_thread()
    confirm = next(e for e in await collect(loop.run(tid, user_text="clear the eggs expiry")) if e["type"] == "confirm")
    await collect(loop.resolve_confirmation(tid, confirm["message_id"], approve=False, note="they're still good", device_id=None))
    assert tools["update"] == [] and "still good" in fake.sent[1]["messages"][-1]["content"]
    await collect(loop.run(tid, user_text="clear the eggs expiry again"))
    await collect(loop.run(tid, user_text="actually never mind"))  # supersedes the pending one
    with session_scope() as s:
        assert s.query(ChatMessage).filter_by(thread_id=tid, status="pending_confirmation").count() == 0


async def test_proactive_mode_refuses_confirm_tier(monkeypatch, tools):
    fake = FakeProvider([[ToolCall("c1", "pantry_update", {"items": []})], [TextDelta("Report.")]])
    use(monkeypatch, fake)
    tid = new_thread()
    evs = await collect(loop.run(tid, user_text="tidy the pantry", proactive=True, playbook="pantry"))
    assert not any(e["type"] == "confirm" for e in evs) and tools["update"] == []
    assert "need a person" in fake.sent[1]["messages"][-1]["content"]


async def test_tool_outside_playbook_is_refused(monkeypatch, tools):
    fake = FakeProvider([[ToolCall("c1", "shopping_create_run", {"summary": "x"})], [TextDelta("Sorry.")]])
    use(monkeypatch, fake)
    evs = await collect(loop.run(new_thread(), user_text="is this a good price?", mode="ask"))
    assert any(e["type"] == "tool" and e["status"] == "error" and "isn't available" in e["summary"] for e in evs)


async def test_fallback_to_next_provider(monkeypatch, tools):
    with session_scope() as s:
        appsettings.update(s, "llm", {"fallback": [{"provider": "groq", "model": "llama"}]})
    daily = ProviderError("quota", retryable=True, status=429, body='{"error": {"details": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}}')
    broken = FakeProvider([], name="gemini", fail_always=daily)  # both Gemini models are out for the day
    backup = FakeProvider([[TextDelta("From the backup.")]], name="groq")
    use(monkeypatch, broken, backup)
    evs = await collect(loop.run(new_thread(), user_text="hello", mode="ask"))
    assert any(e["type"] == "notice" for e in evs) and any(e.get("text") == "From the backup." for e in evs)
    usage = next(e for e in evs if e["type"] == "usage")
    assert usage["provider"] == "groq" and usage["cost_usd"] > 0  # unpriced paid model: conservative estimate
    with session_scope() as s:
        appsettings.update(s, "llm", {"fallback": []})


async def test_budget_stops_calls(monkeypatch, tools):
    fake = FakeProvider([[TextDelta("never")]])
    use(monkeypatch, fake)
    with session_scope() as s:
        appsettings.update(s, "budget", {"monthly_usd": 0.01})
        s.add(LLMUsage(provider="openai", model="x", tokens_in=1, tokens_out=1, cost_usd=0.02))
    evs = await collect(loop.run(new_thread(), user_text="hello", mode="ask"))
    assert any(e["type"] == "error" and "budget" in e["message"].lower() for e in evs) and fake.sent == []
    with session_scope() as s:
        appsettings.update(s, "budget", {"monthly_usd": 10})
        s.query(LLMUsage).filter_by(provider="openai").delete()


def test_router():
    from shopping.agent.playbooks import route

    assert route("Here's my receipt from today") == "pantry"
    assert route("add milk to the list") == "pantry"
    assert route("plan the week around the deals") == "meals"
    assert route("what can I make with leeks?") == "recipes"
    assert route("do a grocery shop for me") == "shop"
    assert route("is $4.99 good for 18 eggs?") == "ask"
    assert route("yes, go ahead", previous="shop") == "shop"



@pytest.fixture
def clean_quota():
    from shopping.agent import quota

    quota.reset()
    yield quota
    quota.reset()


def test_quota_rules(clean_quota):
    from shopping.agent.providers.base import ProviderError as PE

    quota = clean_quota
    llm = {"limits": {"m-flash": {"rpm": 2, "rpd": 3}}, "tiers": {"fast": {"provider": "gemini", "model": "m-lite"},
                                                                  "smart": {"provider": "gemini", "model": "m-flash"}}}
    with session_scope() as s:
        assert quota.check(s, llm, "gemini", "m-flash") == (True, None, 0.0)
        quota.note_request("gemini", "m-flash")
        quota.note_request("gemini", "m-flash")
        ok, _, wait = quota.check(s, llm, "gemini", "m-flash")
        assert ok and 55 < wait <= 60.5  # third call in a minute waits for the window
        for _ in range(3):
            s.add(LLMUsage(provider="gemini", model="m-flash", tokens_in=1, tokens_out=1, cost_usd=0))
        s.flush()
        ok, reason, _ = quota.check(s, llm, "gemini", "m-flash")
        assert not ok and "3 requests" in reason
        assert quota.check(s, llm, "gemini", "m-lite")[0]  # no limits configured: always usable
        assert quota.classify_error("gemini", "m-lite", PE("x", status=429, body="Requests per minute exceeded")) == "minute"
        assert quota.classify_error("gemini", "m-lite", PE("x", status=429, body="GenerateRequestsPerDayPerProjectPerModel")) == "daily"
        assert not quota.check(s, llm, "gemini", "m-lite")[0]
        assert {r["model"]: r["exhausted"] for r in quota.status(s, llm)} == {"m-flash": True, "m-lite": True}
        s.query(LLMUsage).filter_by(model="m-flash").delete()


async def test_exhausted_model_is_skipped_for_the_other_tier(monkeypatch, tools, clean_quota):
    with session_scope() as s:
        appsettings.update(s, "llm", {"limits": {"gemini-flash-lite-latest": {"rpm": 5, "rpd": 1}}})
        s.add(LLMUsage(provider="gemini", model="gemini-flash-lite-latest", tokens_in=1, tokens_out=1, cost_usd=0))
    seen = []

    class Recorder(FakeProvider):
        async def stream(self, *, model, **kw):
            seen.append(model)
            async for ev in super().stream(model=model, **kw):
                yield ev

    use(monkeypatch, Recorder([[TextDelta("Hi from Flash.")]]))
    evs = await collect(loop.run(new_thread(), user_text="hello", mode="ask"))  # Ask uses the fast tier (Lite)
    assert seen == ["gemini-flash-latest"]
    assert any(e["type"] == "notice" and "requests for today" in e["text"] for e in evs)
    with session_scope() as s:
        appsettings.update(s, "llm", {"limits": {"gemini-flash-latest": {"rpm": 5, "rpd": 20}}})
        s.query(LLMUsage).filter_by(model="gemini-flash-lite-latest").delete()


async def test_busy_model_retried_with_backoff_and_attempts_counted(monkeypatch, tools, clean_quota):
    sleeps = []

    async def no_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(loop.asyncio, "sleep", no_sleep)

    class Flaky(FakeProvider):
        failures = 2

        async def stream(self, **kw):
            if self.failures:
                self.failures -= 1
                raise ProviderError("busy", retryable=True, status=503)
            async for ev in super().stream(**kw):
                yield ev

    use(monkeypatch, Flaky([[TextDelta("Answer after retries.")]]))
    with session_scope() as s:
        before = s.query(LLMUsage).filter_by(model="gemini-flash-lite-latest").count()
    evs = await collect(loop.run(new_thread(), user_text="hello", mode="ask"))
    assert any(e.get("text") == "Answer after retries." for e in evs) and sleeps == [3, 8]
    with session_scope() as s:
        rows = s.query(LLMUsage).filter_by(model="gemini-flash-lite-latest").count() - before
        assert rows == 3  # two failed attempts + the successful call: all count toward Google's daily quota
