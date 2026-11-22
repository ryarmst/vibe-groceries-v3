"""Who is acting, for attribution ("Sam removed this: …") without threading a device id through
every core function. Set per request after device auth; new Feedback rows pick it up on flush."""

from __future__ import annotations

from contextvars import ContextVar

current_device: ContextVar[str | None] = ContextVar("current_device", default=None)


def install(session_factory) -> None:
    from sqlalchemy import event

    from shopping.models import Feedback

    @event.listens_for(session_factory, "before_flush")
    def _stamp(session, _ctx, _instances):
        device = current_device.get()
        if device is None:
            return
        for obj in session.new:
            if isinstance(obj, Feedback) and obj.device_id is None:
                obj.device_id = device
