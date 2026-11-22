"""The household's No Frills account: PC ID sign-in (once, in a browser) and its status."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select

from shopping import context
from shopping.api.deps import Caller, require_user
from shopping.core.cart_queue import queue as cart_queue
from shopping.database import session_scope
from shopping.models import RUN_APPROVED, RUN_PARTIAL, RUN_REVIEW, SEL_APPROVED, SEL_FAILED, Selection, ShoppingRun
from shopping.pcid.oauth import AuthError

router = APIRouter(prefix="/api/retailer")


class FinishBody(BaseModel):
    redirect: str = Field(min_length=4, max_length=4096)


def _status() -> dict:
    st = context.auth.status()
    return {"state": st.state, "label": st.label, "customer_id": st.customer_id,
            "signed_in_at": st.signed_in_at.isoformat() if st.signed_in_at else None,
            "signed_in_by": st.signed_in_by,
            "last_renewed_at": st.last_updated.isoformat() if st.last_updated else None}


@router.get("/session")
def session_status(_: Caller = Depends(require_user)):
    return _status()


@router.post("/signin/start")
def signin_start(_: Caller = Depends(require_user)):
    """The PC ID sign-in URL to open in a new tab. Valid for 30 minutes."""
    try:
        return {"url": context.pcid.start_login()}
    except AuthError as exc:
        return {"url": None, "error": str(exc)}


@router.post("/signin/finish")
async def signin_finish(body: FinishBody, caller: Caller = Depends(require_user)):
    """Takes the address the PC ID tab ended on (…/login/success?redirectURL=com.loblaw.pcx://…)."""
    try:
        await context.pcid.finish_login(body.redirect, by=caller.name)
        await context.auth.ensure_authenticated()
    except AuthError as exc:
        return {"ok": False, "error": str(exc), **_status()}
    await requeue_waiting()  # items approved while signed out go to the cart now
    return {"ok": True, **_status()}


async def requeue_waiting() -> int:
    n = 0
    with session_scope() as session:
        rows = session.execute(select(Selection.id, Selection.shopping_run_id).join(ShoppingRun).where(
            Selection.status.in_([SEL_APPROVED, SEL_FAILED]), Selection.approved_at.is_not(None),
            ShoppingRun.status.in_([RUN_REVIEW, RUN_PARTIAL, RUN_APPROVED]))).all()
    by_run: dict[str, list[str]] = {}
    for sid, rid in rows:
        by_run.setdefault(rid, []).append(sid)
    for rid, ids in by_run.items():
        n += cart_queue.enqueue(rid, ids)
    return n


@router.post("/signout")
async def sign_out(_: Caller = Depends(require_user)):
    await context.auth.clear()
    return _status()
