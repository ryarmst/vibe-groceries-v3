"""The weekly plan: refresh deals if stale, then run the plan_week playbook unattended (proactive mode:
it can suggest meals and stage a run, but can't change the pantry, grocery list or Cooking)."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from shopping import appsettings, context
from shopping.agent import loop
from shopping.agent.setup import ensure_tools
from shopping.config import settings
from shopping.core import catalog, inbox
from shopping.database import session_scope
from shopping.meals import planner
from shopping.models import ChatThread, utcnow

log = logging.getLogger(__name__)
_running: asyncio.Task | None = None


async def run_weekly_plan(*, requested_by: str | None = None, thread_id: str | None = None) -> str:
    ensure_tools()
    with session_scope() as session:
        jobs = appsettings.get(session, "jobs")
        last = catalog.last_scan(session)
        week = planner.week_start(weekday=int(jobs["weekly_plan_weekday"]))
        if thread_id is None:
            t = ChatThread(title=f"Weekly plan — week of {week:%b %-d}", mode="auto", system=True, created_by=requested_by)
            session.add(t)
            session.flush()
            thread_id = t.id
    if (last is None or last.started_at < utcnow() - timedelta(hours=20)) and not catalog.scan_running():
        try:
            await catalog.scan_deals(context.client(), session_scope, store_id=settings.store_id)
        except Exception as exc:  # noqa: BLE001 - plan with what we have
            log.warning("weekly plan: deal scan failed: %s", exc)
    final, errors = "", []
    async for ev in loop.run(thread_id, user_text=f"Plan the week starting {week.isoformat()} ({jobs['meals_per_week']} meals) "
                                                  "and stage the shopping run.", proactive=True, playbook="plan_week",
                             device_name="the weekly schedule"):
        if ev["type"] == "text":
            final += ev["text"]
        elif ev["type"] == "mode":
            final = ""
        elif ev["type"] == "error":
            errors.append(ev["message"])
        if ev["type"] == "tool" and ev.get("status") == "running":
            final = ""  # keep only the text after the last tool call (the report)
    with session_scope() as session:
        if errors and not final:
            inbox.post(session, kind="job_failed", title="The weekly plan didn't finish", body="; ".join(errors)[:1000],
                       link=f"#/chat/{thread_id}")
        else:
            inbox.post(session, kind="plan_ready", title=f"Week of {week:%b %-d}: meals and shopping run ready",
                       body=final.strip()[:1500], link=f"#/chat/{thread_id}", dedupe_key=f"plan:{week.isoformat()}")
    return thread_id


def start_in_background(*, requested_by: str | None = None) -> str | None:
    global _running
    if _running is not None and not _running.done():
        return None
    with session_scope() as session:
        jobs = appsettings.get(session, "jobs")
        week = planner.week_start(weekday=int(jobs["weekly_plan_weekday"]))
        t = ChatThread(title=f"Weekly plan — week of {week:%b %-d}", mode="auto", system=True, created_by=requested_by)
        session.add(t)
        session.flush()
        thread_id = t.id
    _running = asyncio.create_task(run_weekly_plan(requested_by=requested_by, thread_id=thread_id), name="weekly-plan")
    return thread_id


def running() -> bool:
    return _running is not None and not _running.done()
