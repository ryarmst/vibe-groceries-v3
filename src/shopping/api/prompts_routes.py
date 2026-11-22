"""The Prompts page: read, edit, reset and preview the assistant's prompts (see shopping/prompts/README.md)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from shopping import appsettings, prompts
from shopping.agent import playbooks
from shopping.api.deps import Caller, require_user
from shopping.database import session_scope
from shopping.models import PromptRevision

router = APIRouter(prefix="/api/prompts", dependencies=[Depends(require_user)])


def _get(pid: str) -> prompts.Prompt:
    try:
        return prompts.get(pid)
    except prompts.PromptError as exc:
        raise HTTPException(404, str(exc)) from exc


def _summary(session, p: prompts.Prompt) -> dict:
    body, tier, row = prompts.current(session, p.id)
    return {"id": p.id, "group": p.group, "title": p.title, "description": p.description, "tier": tier,
            "customized": row is not None, "updated_at": row.updated_at.isoformat() if row else None,
            "updated_by": row.updated_by if row else None, "chars": len(body)}


@router.get("")
def list_prompts():
    with session_scope() as session:
        return {"prompts": [_summary(session, p) for p in prompts.catalog().values()],
                "groups": list(dict.fromkeys(p.group for p in prompts.catalog().values()))}


def _detail(session, p: prompts.Prompt) -> dict:
    body, _, _ = prompts.current(session, p.id)
    return {**_summary(session, p), "body": body, "default": p.default, "default_tier": p.tier, "tools": list(p.tools),
            "placeholders": [{"name": n, "meaning": prompts.PLACEHOLDERS.get(n, "")} for n in p.placeholders],
            "warnings": [f"{{{n}}} isn't filled in for this prompt" for n in prompts.unknown_placeholders(p.id, body)],
            "revisions": [{"id": r.id, "saved_at": r.saved_at.isoformat(), "saved_by": r.saved_by, "note": r.note,
                           "chars": len(r.body), "tier": r.tier} for r in prompts.revisions(session, p.id)]}


@router.get("/{pid:path}/preview")
def preview(pid: str):
    """The full system prompt as the model receives it (shared instructions + household notes + this task)."""
    p = _get(pid)
    if not (p.id.startswith("tasks/") or p.id == "jobs/weekly_plan"):
        raise HTTPException(400, "Only chat tasks and the weekly plan have a combined preview.")
    name = "plan_week" if p.id == "jobs/weekly_plan" else p.id.removeprefix("tasks/")
    from shopping.agent.loop import _system  # noqa: PLC0415 - avoids importing the agent at startup

    with session_scope() as session:
        jobs = appsettings.get(session, "jobs")
    text = _system(playbooks.load(name), "(the person chatting)", jobs)
    return {"text": text, "chars": len(text), "approx_tokens": len(text) // 4}


@router.get("/{pid:path}/revisions/{rev_id}")
def revision(pid: str, rev_id: int):
    _get(pid)
    with session_scope() as session:
        r = session.get(PromptRevision, rev_id)
        if r is None or r.prompt_id != pid:
            raise HTTPException(404, "No such revision.")
        return {"id": r.id, "body": r.body, "tier": r.tier, "saved_at": r.saved_at.isoformat(), "saved_by": r.saved_by}


@router.get("/{pid:path}")
def detail(pid: str):
    p = _get(pid)
    with session_scope() as session:
        return _detail(session, p)


class SaveBody(BaseModel):
    body: str = Field(max_length=prompts.MAX_CHARS + 1000)
    tier: str | None = Field(default=None, pattern="^(fast|smart)$")


@router.put("/{pid:path}")
def save(pid: str, body: SaveBody, caller: Caller = Depends(require_user)):
    p = _get(pid)
    with session_scope() as session:
        try:
            prompts.save(session, p.id, body=body.body, tier=body.tier, by=caller.name)
        except prompts.PromptError as exc:
            raise HTTPException(400, str(exc)) from exc
        return _detail(session, p)


@router.delete("/{pid:path}")
def reset(pid: str, caller: Caller = Depends(require_user)):
    p = _get(pid)
    with session_scope() as session:
        prompts.reset(session, p.id, by=caller.name)
        return _detail(session, p)

