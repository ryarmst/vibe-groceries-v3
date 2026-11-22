"""The agent loop: model ↔ tools until a final answer, a needed confirmation, or a limit.

Yields events for the chat stream:
  {"type": "mode", "mode", "title"}             which playbook is running
  {"type": "text", "text"}                       streamed answer text
  {"type": "tool", "id", "name", "status", ...}  tool activity (running / done / error)
  {"type": "confirm", "message_id", "name", "preview", "arguments"}  waiting for a person
  {"type": "notice", "text"}                     e.g. falling back to another provider
  {"type": "usage", "provider", "model", "tokens_in", "tokens_out", "cost_usd"}
  {"type": "error", "message"} / {"type": "done", "awaiting_confirmation": bool}
The database never stays open across a model or tool call.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import AsyncIterator
from zoneinfo import ZoneInfo

from sqlalchemy import select

from shopping import appsettings, context, prompts
from shopping.agent import budget, playbooks, quota
from shopping.agent.providers import registry as providers
from shopping.agent.providers.base import Done, ProviderError, TextDelta, ToolCall, Usage
from shopping.agent.tools import CART_WRITE, CONFIRM, ToolContext, audit, execute, preview_text, registry
from shopping.config import settings
from shopping.database import session_scope
from shopping.models import ChatMessage, ChatThread

log = logging.getLogger(__name__)

HISTORY_USER_TURNS = 12
MAX_TOKENS = 8192  # includes "thinking" tokens on reasoning models


def _history(messages: list[ChatMessage]) -> list[dict]:
    """Neutral messages for the model: the last N user turns (cut at a user message, so tool calls and
    their results stay together). Declined confirmations are sent as tool results saying so."""
    users = [i for i, m in enumerate(messages) if m.role == "user"]
    start = users[-HISTORY_USER_TURNS] if len(users) > HISTORY_USER_TURNS else 0
    out = []
    for m in messages[start:]:
        if m.role == "user":
            out.append({"role": "user", "content": m.content or ""})
        elif m.role == "assistant":
            out.append({"role": "assistant", "content": m.content or "", "tool_calls": m.tool_calls or []})
        elif m.role == "tool" and m.status != "pending_confirmation":
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "name": m.tool_name, "content": m.content or ""})
    return out


def _system(pb: playbooks.Playbook, device_name: str, jobs: dict) -> str:
    now = datetime.now(ZoneInfo(settings.timezone))
    fill = {"today": now.strftime("%Y-%m-%d"), "weekday": now.strftime("%A"), "store_id": settings.store_id,
            "device_name": device_name or "a household member", "household": settings.household, "meals_per_week": jobs.get("meals_per_week", 5)}
    return f"{prompts.render(playbooks.common(), fill)}\n\n## Mode: {pb.title}\n{prompts.render(pb.body, fill)}"


def _candidates(llm: dict, tier: str) -> list[tuple[str, str]]:
    """The tier's model, then the other tier's (a separate free quota), then any configured fallbacks."""
    first = llm["tiers"].get(tier) or llm["tiers"]["fast"]
    other = llm["tiers"]["smart" if tier == "fast" else "fast"]
    seen, out = set(), []
    for c in [first, other] + list(llm.get("fallback") or []):
        key = (c["provider"], c["model"])
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _add_message(thread_id: str, **fields) -> int:
    with session_scope() as session:
        m = ChatMessage(thread_id=thread_id, **fields)
        session.add(m)
        t = session.get(ChatThread, thread_id)
        if t is not None:
            t.updated_at = datetime.now(ZoneInfo("UTC"))
        session.flush()
        return m.id


def pending_confirmations(thread_id: str) -> list[ChatMessage]:
    with session_scope() as session:
        return list(session.scalars(select(ChatMessage).where(ChatMessage.thread_id == thread_id,
                                                              ChatMessage.status == "pending_confirmation")))


async def run(thread_id: str, *, user_text: str | None = None, device_id: str | None = None, device_name: str = "",
              mode: str | None = None, proactive: bool = False, playbook: str | None = None) -> AsyncIterator[dict]:
    ctx = ToolContext(thread_id=thread_id, device_id=device_id, proactive=proactive)
    with session_scope() as session:
        thread = session.get(ChatThread, thread_id)
        if thread is None:
            yield {"type": "error", "message": "Unknown conversation."}
            return
        llm = appsettings.get(session, "llm")
        jobs = appsettings.get(session, "jobs")
        if user_text is not None:
            # A new message supersedes questions still waiting for confirmation.
            for p in session.scalars(select(ChatMessage).where(ChatMessage.thread_id == thread_id,
                                                               ChatMessage.status == "pending_confirmation")):
                p.status, p.content = "declined", "Not confirmed: the person sent a new message instead."
            if thread.title == "New chat":
                thread.title = (user_text.strip().splitlines() or ["Chat"])[0][:60]
            if mode:
                thread.mode = mode
        thread_mode = thread.mode
        last_mode = next((m.tool_name for m in reversed(thread.messages) if m.role == "user" and m.tool_name), None)
    if user_text is not None:
        resolved = playbook or (thread_mode if thread_mode in playbooks.MODES else playbooks.route(user_text, last_mode))
        # The user message remembers which mode answered it (tool_name doubles as that marker for user rows).
        _add_message(thread_id, role="user", content=user_text, device_id=device_id, tool_name=resolved)
    else:
        resolved = playbook or (thread_mode if thread_mode in playbooks.MODES else (last_mode or "ask"))
    if pending_confirmations(thread_id):
        yield {"type": "done", "awaiting_confirmation": True}
        return

    pb = playbooks.load(resolved)
    yield {"type": "mode", "mode": resolved, "title": pb.title}
    system = _system(pb, device_name, jobs)
    tools = [t for t in registry.select(list(pb.tools))]
    allowed = {t.name: t for t in tools}
    specs = [t.spec() for t in tools]

    for step in range(int(llm.get("max_steps", 12))):
        with session_scope() as session:
            try:
                budget.check(session)
            except budget.BudgetExceeded as exc:
                yield {"type": "error", "message": str(exc)}
                yield {"type": "done", "awaiting_confirmation": False}
                return
            history = _history(list(session.scalars(select(ChatMessage).where(ChatMessage.thread_id == thread_id).order_by(ChatMessage.id))))

        text, calls, usage, used, last_error = "", [], Usage(), None, None
        for provider_name, model in _candidates(llm, pb.tier):
            with session_scope() as session:
                ok, reason, wait = quota.check(session, llm, provider_name, model)
            if not ok:
                yield {"type": "notice", "text": f"{reason}; using the next model."}
                continue
            if wait > 0:
                yield {"type": "notice", "text": f"Pausing {wait:.0f}s for {model}'s per-minute limit…"}
                await asyncio.sleep(wait)
            try:
                with session_scope() as session:
                    provider = providers.build(session, context.vault, provider_name)
                for attempt in range(3):
                    try:
                        quota.note_request(provider_name, model)
                        async for ev in provider.stream(system=system, messages=history, tools=specs, model=model,
                                                        temperature=float(llm.get("temperature", 0.4)), max_tokens=MAX_TOKENS):
                            if isinstance(ev, TextDelta):
                                text += ev.text
                                yield {"type": "text", "text": ev.text}
                            elif isinstance(ev, ToolCall):
                                calls.append(ev)
                            elif isinstance(ev, Usage):
                                usage = ev
                            elif isinstance(ev, Done):
                                pass
                        used = (provider_name, model)
                        break
                    except ProviderError as exc:
                        kind = quota.classify_error(provider_name, model, exc)
                        if exc.status is not None and exc.status not in (401, 403):
                            with session_scope() as session:
                                quota.record_failed_attempt(session, provider_name, model, thread_id)
                        if attempt < 2 and not text and (kind == "minute" or (exc.retryable and kind is None)):
                            pause = 20 if kind == "minute" else (3 if attempt == 0 else 8)  # 503s and timeouts: back off
                            yield {"type": "notice", "text": f"{model} is busy; retrying in {pause}s…"}
                            await asyncio.sleep(pause)
                            continue
                        raise
                if used:
                    break
            except ProviderError as exc:
                last_error = exc
                if text:  # partial answer already shown: don't restart on another model
                    break
                yield {"type": "notice", "text": f"{provider_name}/{model} failed: {exc}. Trying the next model…"}
        if used is None:
            msg = str(last_error) if last_error else "No LLM is configured."
            yield {"type": "error", "message": f"The assistant couldn't answer: {msg}"}
            yield {"type": "done", "awaiting_confirmation": False}
            return

        with session_scope() as session:
            cost = budget.record(session, provider=used[0], model=used[1], tokens_in=usage.input_tokens,
                                 tokens_out=usage.output_tokens, thread_id=thread_id)
        yield {"type": "usage", "provider": used[0], "model": used[1], "tokens_in": usage.input_tokens,
               "tokens_out": usage.output_tokens, "cost_usd": cost}
        _add_message(thread_id, role="assistant", content=text,
                     tool_calls=[{"id": c.id, "name": c.name, "arguments": c.arguments, **({"extra": c.extra} if c.extra else {})}
                                 for c in calls],
                     provider=used[0], model=used[1], tokens_in=usage.input_tokens, tokens_out=usage.output_tokens, cost_usd=cost)
        if not calls:
            yield {"type": "done", "awaiting_confirmation": False}
            return

        waiting = False
        for c in calls:
            tool = allowed.get(c.name)
            if tool is None:
                result = f"Error: tool {c.name!r} isn't available in {pb.title} mode."
                _add_message(thread_id, role="tool", tool_call_id=c.id, tool_name=c.name, content=result)
                yield {"type": "tool", "id": c.id, "name": c.name, "status": "error", "summary": result}
                continue
            if proactive and c.name in CART_WRITE:
                result = "Not done: only a person can put items straight into the cart."
                audit(tool=c.name, tier=tool.tier, args=c.arguments, outcome="refused", detail="proactive", ctx=ctx)
                _add_message(thread_id, role="tool", tool_call_id=c.id, tool_name=c.name, content=result, status="declined")
                yield {"type": "tool", "id": c.id, "name": c.name, "status": "error", "summary": result}
                continue
            if tool.tier == CONFIRM:
                if proactive:
                    result = "Not done: changes to the pantry, grocery list or Cooking need a person. List it in your report instead."
                    audit(tool=c.name, tier=tool.tier, args=c.arguments, outcome="refused", detail="proactive", ctx=ctx)
                    _add_message(thread_id, role="tool", tool_call_id=c.id, tool_name=c.name, content=result, status="declined")
                    yield {"type": "tool", "id": c.id, "name": c.name, "status": "error", "summary": result}
                    continue
                preview = preview_text(tool, c.arguments)
                mid = _add_message(thread_id, role="tool", tool_call_id=c.id, tool_name=c.name, status="pending_confirmation",
                                   content=json.dumps(c.arguments))
                audit(tool=c.name, tier=tool.tier, args=c.arguments, outcome="pending", detail=None, ctx=ctx)
                yield {"type": "confirm", "message_id": mid, "name": c.name, "preview": preview, "arguments": c.arguments}
                waiting = True
                continue
            yield {"type": "tool", "id": c.id, "name": c.name, "status": "running", "arguments": c.arguments}
            ok, result = await execute(tool, c.arguments, ctx)
            _add_message(thread_id, role="tool", tool_call_id=c.id, tool_name=c.name, content=result)
            yield {"type": "tool", "id": c.id, "name": c.name, "status": "done" if ok else "error", "summary": result[:300]}
        if waiting:
            yield {"type": "done", "awaiting_confirmation": True}
            return

    yield {"type": "notice", "text": "Stopped after the maximum number of steps; ask me to continue if needed."}
    yield {"type": "done", "awaiting_confirmation": False}


async def resolve_confirmation(thread_id: str, message_id: int, *, approve: bool, note: str | None, device_id: str | None,
                               device_name: str = "") -> AsyncIterator[dict]:
    """A person approved or declined a pending change. Run it (or record the refusal), then continue the
    conversation once nothing else is waiting."""
    ctx = ToolContext(thread_id=thread_id, device_id=device_id)
    with session_scope() as session:
        m = session.get(ChatMessage, message_id)
        if m is None or m.thread_id != thread_id or m.status != "pending_confirmation":
            yield {"type": "error", "message": "That change is no longer waiting for confirmation."}
            return
        name, args = m.tool_name, json.loads(m.content or "{}")
    tool = registry.get(name)
    if approve and tool is not None:
        yield {"type": "tool", "id": str(message_id), "name": name, "status": "running", "arguments": args}
        ok, result = await execute(tool, args, ctx)
        status = "done"
        yield {"type": "tool", "id": str(message_id), "name": name, "status": "done" if ok else "error", "summary": result[:300]}
    else:
        result = "The person declined this change." + (f" Their note: {note}" if note else "")
        status = "declined"
        audit(tool=name, tier=CONFIRM, args=args, outcome="declined", detail=note, ctx=ctx)
        yield {"type": "tool", "id": str(message_id), "name": name, "status": "declined", "summary": result}
    with session_scope() as session:
        m = session.get(ChatMessage, message_id)
        m.status, m.content, m.device_id = status, result, device_id
    if pending_confirmations(thread_id):
        yield {"type": "done", "awaiting_confirmation": True}
        return
    async for ev in run(thread_id, device_id=device_id, device_name=device_name):
        yield ev
