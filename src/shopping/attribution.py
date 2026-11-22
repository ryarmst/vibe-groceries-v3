"""Who is acting, for attribution ("Sam removed this: …") without passing a user id through every core
function. Set per request after sign-in; new Feedback rows pick it up when saved."""

from __future__ import annotations

from contextvars import ContextVar

current_user: ContextVar[str | None] = ContextVar("current_user", default=None)


def install(session_factory) -> None:
    from sqlalchemy import event

    from shopping.models import Feedback

    @event.listens_for(session_factory, "before_flush")
    def _stamp(session, _ctx, _instances):
        user = current_user.get()
        if user is None:
            return
        for obj in session.new:
            if isinstance(obj, Feedback) and obj.device_id is None:
                obj.device_id = user
