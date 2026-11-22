"""The inbox: what needs a person (drives the app's badge).

Stored events (plan ready, run ready, meal check-ins, job failures) plus computed ones (the No Frills
session is needed because something is waiting to go into the cart).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from shopping.models import (
    RUN_APPROVED, RUN_PARTIAL, RUN_REVIEW, SEL_APPROVED, SEL_FAILED, InboxEvent, Selection, ShoppingRun, utcnow,
)


def post(session: Session, *, kind: str, title: str, body: str | None = None, link: str | None = None,
         dedupe_key: str | None = None) -> InboxEvent | None:
    """Add an event unless an undismissed one with the same dedupe key exists."""
    if dedupe_key and session.scalar(select(InboxEvent).where(InboxEvent.dedupe_key == dedupe_key, InboxEvent.dismissed_at.is_(None))):
        return None
    ev = InboxEvent(kind=kind, title=title[:256], body=body, link=link, dedupe_key=dedupe_key)
    session.add(ev)
    session.flush()
    return ev


def dismiss(session: Session, event_id: int) -> None:
    ev = session.get(InboxEvent, event_id)
    if ev is not None and ev.dismissed_at is None:
        ev.dismissed_at = utcnow()


def dismiss_key(session: Session, dedupe_key: str) -> None:
    for ev in session.scalars(select(InboxEvent).where(InboxEvent.dedupe_key == dedupe_key, InboxEvent.dismissed_at.is_(None))):
        ev.dismissed_at = utcnow()


def waiting_for_cart(session: Session) -> int:
    """Approved items not in the cart yet (e.g. approved while the session was expired)."""
    return len(list(session.scalars(
        select(Selection.id).join(ShoppingRun).where(Selection.status.in_([SEL_APPROVED, SEL_FAILED]),
                                                     ShoppingRun.status.in_([RUN_REVIEW, RUN_PARTIAL, RUN_APPROVED])))))


def items(session: Session, *, session_state: str, cart_pending: int = 0) -> list[dict]:
    out = []
    waiting = waiting_for_cart(session) + cart_pending
    if session_state != "connected" and waiting:
        out.append({"id": None, "kind": "needs_session", "title": f"{waiting} approved item(s) are waiting for the cart",
                    "body": "Sign in to No Frills (Settings → No Frills account); approved items go to the cart then.", "link": "#/settings"})
    for r in session.scalars(select(ShoppingRun).where(ShoppingRun.status == RUN_REVIEW).order_by(ShoppingRun.created_at.desc())):
        out.append({"id": None, "kind": "run_review", "title": f"Review: {r.summary}", "body": None, "link": f"#/runs/{r.id}"})
    for ev in session.scalars(select(InboxEvent).where(InboxEvent.dismissed_at.is_(None)).order_by(InboxEvent.created_at.desc()).limit(30)):
        out.append({"id": ev.id, "kind": ev.kind, "title": ev.title, "body": ev.body, "link": ev.link,
                    "created_at": ev.created_at.isoformat()})
    return out
