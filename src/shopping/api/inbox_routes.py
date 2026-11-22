"""Who am I, the inbox (badge), and a one-call status summary for the header."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from shopping import context
from shopping.api.deps import Caller, require_user
from shopping.core import catalog, inbox
from shopping.core.cart_queue import queue as cart_queue
from shopping.jobs import deals as deals_job
from shopping.database import session_scope

router = APIRouter(prefix="/api")


@router.get("/me")
def me(caller: Caller = Depends(require_user)):
    return {"id": caller.id, "name": caller.name, "email": caller.email}


@router.get("/inbox")
def get_inbox(after_event: int = 0, caller: Caller = Depends(require_user)):
    st = context.auth.status()
    with session_scope() as session:
        items = inbox.items(session, session_state=st.state, cart_pending=len(cart_queue.pending()))
        last_scan = catalog.scan_dict(catalog.last_scan(session))
    return {
        "items": items,
        "count": len(items),
        "retailer_session": {"state": st.state, "seconds_remaining": st.seconds_remaining},
        "cart": {"pending": len(cart_queue.pending()), "pending_ids": sorted(cart_queue.pending()),
                 "events": [{"id": e.id, "run_id": e.run_id, "ok": e.ok, "message": e.message} for e in cart_queue.events_since(after_event)],
                 "last_event_id": cart_queue.last_event_id},
        "deal_scan": {"last": last_scan, "running": catalog.scan_running() or deals_job.running()},
        "user": {"id": caller.id, "name": caller.name},
    }


@router.post("/inbox/{event_id}/dismiss")
def dismiss(event_id: int, _: Caller = Depends(require_user)):
    with session_scope() as session:
        inbox.dismiss(session, event_id)
    return {"ok": True}
