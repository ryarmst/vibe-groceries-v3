"""Settings: write-only secrets, LLM tiers and budget, Notion databases, jobs."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from shopping import appsettings, context
from shopping.api.deps import Caller, require_user
from shopping.database import session_scope

router = APIRouter(prefix="/api/settings")


class SecretBody(BaseModel):
    value: str = Field(min_length=1, max_length=8192)


@router.get("/secrets")
def secrets_status(_: Caller = Depends(require_user)):
    with session_scope() as session:
        return {"secrets": context.vault.status(session)}


@router.put("/secrets/{name}")
def set_secret(name: str, body: SecretBody, caller: Caller = Depends(require_user)):
    with session_scope() as session:
        context.vault.set(session, name, body.value, by=caller.device_id)
        return {"ok": True, "secrets": context.vault.status(session)}  # status only; the value never comes back


@router.delete("/secrets/{name}")
def delete_secret(name: str, _: Caller = Depends(require_user)):
    with session_scope() as session:
        return {"deleted": context.vault.delete(session, name), "secrets": context.vault.status(session)}


@router.post("/notify/test")
async def notify_test(_: Caller = Depends(require_user)):
    from shopping.notify import push

    try:
        sent = await push.send("vibe-groceries test", "Deal alerts will arrive like this.")
    except push.NotifyError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": sent, "error": None if sent else "No ntfy topic set (Settings → API keys)."}


@router.get("")
def all_settings(_: Caller = Depends(require_user)):
    with session_scope() as session:
        return {s: appsettings.get(session, s) for s in appsettings.DEFAULTS} | {"providers": sorted(appsettings.PROVIDERS)}


@router.put("/{section}")
def update_section(section: str, changes: dict, _: Caller = Depends(require_user)):
    with session_scope() as session:
        return {section: appsettings.update(session, section, changes)}
