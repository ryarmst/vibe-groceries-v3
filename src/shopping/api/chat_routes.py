"""Chat with the agent (streamed as Server-Sent Events), plus LLM settings helpers and usage."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from shopping import appsettings, context
from shopping.agent import budget, loop, playbooks
from shopping.agent.providers import registry as providers
from shopping.agent.providers.base import ProviderError
from shopping.agent.setup import ensure_tools
from shopping.api.deps import Caller, require_user
from shopping.database import session_scope
from shopping.models import AgentAudit, ChatMessage, ChatThread

router = APIRouter(prefix="/api", dependencies=[Depends(require_user)])


def _sse(events):
    async def gen():
        try:
            async for ev in events:
                yield f"data: {json.dumps(ev, default=str)}\n\n"
        except Exception as exc:  # noqa: BLE001 - surface to the client instead of a broken stream
            yield f"data: {json.dumps({'type': 'error', 'message': f'Internal error: {type(exc).__name__}'})}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'awaiting_confirmation': False})}\n\n"
            raise
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"x-accel-buffering": "no", "cache-control": "no-store"})


def _message_dict(m: ChatMessage) -> dict:
    d = {"id": m.id, "role": m.role, "content": m.content, "created_at": m.created_at.isoformat(), "status": m.status,
         "device_id": m.device_id}
    if m.role == "assistant":
        d.update({"tool_calls": m.tool_calls, "provider": m.provider, "model": m.model, "cost_usd": m.cost_usd})
    if m.role == "tool":
        d.update({"tool_name": m.tool_name, "tool_call_id": m.tool_call_id})
        if m.status == "pending_confirmation":
            from shopping.agent.tools import preview_text, registry

            tool = registry.get(m.tool_name)
            args = json.loads(m.content or "{}")
            d.update({"arguments": args, "preview": preview_text(tool, args) if tool else m.tool_name})
        elif m.content and len(m.content) > 600:
            d["content"] = m.content[:600] + "…"
    if m.role == "user":
        d["mode"] = m.tool_name
    return d


class NewThread(BaseModel):
    mode: str = Field(default="auto", pattern="^(auto|ask|shop|quick|meals|pantry|recipes)$")


@router.post("/chat/threads")
def new_thread(body: NewThread, caller: Caller = Depends(require_user)):
    with session_scope() as session:
        t = ChatThread(mode=body.mode, created_by=caller.id)
        session.add(t)
        session.flush()
        return {"thread_id": t.id, "mode": t.mode}


@router.get("/chat/threads")
def threads(limit: int = 20):
    with session_scope() as session:
        rows = session.scalars(select(ChatThread).order_by(ChatThread.updated_at.desc()).limit(min(limit, 100)))
        return {"threads": [{"thread_id": t.id, "title": t.title, "mode": t.mode, "updated_at": t.updated_at.isoformat(),
                             "system": t.system} for t in rows], "modes": ["auto", *playbooks.MODES]}


@router.get("/chat/threads/{thread_id}")
def thread(thread_id: str):
    with session_scope() as session:
        t = session.get(ChatThread, thread_id)
        if t is None:
            return {"error": "not found"}
        return {"thread_id": t.id, "title": t.title, "mode": t.mode, "system": t.system,
                "messages": [_message_dict(m) for m in t.messages]}


@router.delete("/chat/threads/{thread_id}")
def delete_thread(thread_id: str):
    with session_scope() as session:
        t = session.get(ChatThread, thread_id)
        if t is not None:
            session.delete(t)
    return {"ok": True}


class SendBody(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    mode: str | None = Field(default=None, pattern="^(auto|ask|shop|quick|meals|pantry|recipes)$")


@router.post("/chat/threads/{thread_id}/messages")
async def send(thread_id: str, body: SendBody, caller: Caller = Depends(require_user)):
    ensure_tools()
    return _sse(loop.run(thread_id, user_text=body.text, device_id=caller.id, person=caller.name, mode=body.mode))


class ConfirmBody(BaseModel):
    approve: bool
    note: str | None = Field(default=None, max_length=1000)


@router.post("/chat/threads/{thread_id}/confirm/{message_id}")
async def confirm(thread_id: str, message_id: int, body: ConfirmBody, caller: Caller = Depends(require_user)):
    ensure_tools()
    return _sse(loop.resolve_confirmation(thread_id, message_id, approve=body.approve, note=body.note,
                                          device_id=caller.id, person=caller.name))


# -- LLM helpers -------------------------------------------------------------------------------

class TestBody(BaseModel):
    provider: str
    model: str


@router.post("/llm/test")
async def test_llm(body: TestBody):
    """One tiny request to check the key and model work (counts toward usage)."""
    with session_scope() as session:
        try:
            provider = providers.build(session, context.vault, body.provider)
        except ProviderError as exc:
            return {"ok": False, "error": str(exc)}
    text, usage = "", None
    try:
        async for ev in provider.stream(system="Reply with exactly: OK", messages=[{"role": "user", "content": "ping"}],
                                        tools=[], model=body.model, temperature=0, max_tokens=1000):  # room for "thinking" models
            if hasattr(ev, "text"):
                text += ev.text
            elif ev.__class__.__name__ == "Usage":
                usage = ev
    except ProviderError as exc:
        return {"ok": False, "error": str(exc)}
    with session_scope() as session:
        budget.record(session, provider=body.provider, model=body.model, tokens_in=getattr(usage, "input_tokens", 0),
                      tokens_out=getattr(usage, "output_tokens", 0), thread_id=None)
    return {"ok": True, "reply": text.strip()[:100]}


@router.get("/llm/models/{provider_name}")
async def models(provider_name: str):
    with session_scope() as session:
        try:
            provider = providers.build(session, context.vault, provider_name)
        except ProviderError as exc:
            return {"models": [], "error": str(exc)}
    try:
        return {"models": await provider.list_models()}
    except ProviderError as exc:
        return {"models": [], "error": str(exc)}


@router.get("/llm/usage")
def usage():
    from shopping.agent import quota

    with session_scope() as session:
        return {"summary": budget.summary(session), "daily": budget.daily_breakdown(session),
                "models_today": quota.status(session, appsettings.get(session, "llm"))}


@router.get("/audit")
def audit_log(limit: int = 100):
    with session_scope() as session:
        rows = session.scalars(select(AgentAudit).order_by(AgentAudit.at.desc()).limit(min(limit, 500)))
        return {"entries": [{"at": a.at.isoformat(), "thread_id": a.thread_id, "tool": a.tool, "tier": a.tier, "outcome": a.outcome,
                             "arguments": a.arguments, "detail": a.detail, "duration_ms": a.duration_ms, "device_id": a.device_id}
                            for a in rows]}
