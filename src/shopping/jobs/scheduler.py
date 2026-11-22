"""A minute-tick scheduler for household-time jobs. Each job records when it last ran (app_settings), so
restarts don't double-run and a missed hour still runs once the server is back."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from shopping import appsettings, context
from shopping.auth.session import AuthRequired, SessionUnavailable
from shopping.config import settings
from shopping.core import inbox
from shopping.core import orders as ords
from shopping.database import session_scope
from shopping.jobs import deals as deals_job
from shopping.jobs import weekly
from shopping.meals import planner
from shopping.models import AppSetting, PastOrder
from shopping.notion.kitchen import open_kitchen

log = logging.getLogger(__name__)


def _marker(session, name: str) -> str | None:
    row = session.get(AppSetting, f"job:{name}")
    return row.value if row else None


def _set_marker(session, name: str, value: str) -> None:
    row = session.get(AppSetting, f"job:{name}") or AppSetting(key=f"job:{name}")
    row.value = value
    session.add(row)


async def checkins() -> int:
    """Planned meals at or past Cook By → an inbox nudge. Suggested/Planned meals 3+ days past → Skipped."""
    today = date.today()
    n = 0
    async with open_kitchen() as k:
        meals = await k.meals(statuses=["Planned", "Suggested"], limit=200)
        for m in meals:
            cook_by = (m.get("Cook By") or "")[:10]
            if not cook_by:
                continue
            if cook_by < (today - timedelta(days=3)).isoformat():
                await k.meal_update(m["id"], {"Status": "Skipped"})
                with session_scope() as session:
                    inbox.dismiss_key(session, f"checkin:{m['id']}")
                    inbox.post(session, kind="meal_skipped", title=f"Didn't get to {m.get('Name')}?",
                               body="Marked Skipped. If its fresh ingredients were frozen, used elsewhere or tossed, tell the assistant "
                                    "so the pantry stays right.", link="#/meals", dedupe_key=f"skipped:{m['id']}")
                n += 1
            elif m.get("Status") == "Planned" and cook_by <= today.isoformat():
                with session_scope() as session:
                    if inbox.post(session, kind="meal_checkin", title=f"Cooked {m.get('Name')}?",
                                  body="Mark it cooked (with a rating), or skipped.", link="#/meals", dedupe_key=f"checkin:{m['id']}"):
                        n += 1
    return n


async def import_orders() -> int:
    if context.auth.status().state != "connected":
        return 0
    client = context.client()
    summaries = await client.list_past_orders()
    n = 0
    with session_scope() as session:
        known = set(session.scalars(select(PastOrder.id)))
    for s in summaries[:5]:
        if s.order_id in known:
            continue
        data = await client.get_past_order(s.order_id)
        with session_scope() as session:
            ords.import_order(session, data, s, store_id=settings.store_id)
            inbox.post(session, kind="order_imported", title=f"Order {s.order_id} imported (${s.total or 0:.2f})",
                       body="Ask the assistant to mark what you bought on the grocery list.", link=f"#/orders/{s.order_id}",
                       dedupe_key=f"order:{s.order_id}")
        n += 1
    return n


async def keep_session_alive() -> str:
    """Renew the No Frills (PC id) session once a day, so an idle week doesn't let it lapse. If PC id has
    ended it, tell the household instead of failing quietly at the next cart action."""
    if context.auth.status().state != "connected":
        return "not signed in"
    try:
        await context.auth.ensure_authenticated()
        return "renewed"
    except AuthRequired as exc:
        with session_scope() as session:
            inbox.post(session, kind="needs_session", title="No Frills sign-in ended", body=str(exc), link="#/settings",
                       dedupe_key="pcid_signed_out")
        return f"signed out: {exc}"
    except SessionUnavailable as exc:
        return f"PC id unavailable, will retry tomorrow: {exc}"


def backup_database(keep: int = 7) -> str:
    """SQLite online backup into DATA_DIR/backups (kept 7 days). Copy that folder off the server for safety."""
    import sqlite3

    out_dir = settings.data_dir / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"app-{date.today():%Y%m%d}.db"
    src = sqlite3.connect(settings.db_path)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    for old in sorted(out_dir.glob("app-*.db"))[:-keep]:
        old.unlink()
    return str(target)


async def tick(now: datetime) -> None:
    with session_scope() as session:
        jobs = appsettings.get(session, "jobs")
        week = planner.week_start(now.date(), weekday=int(jobs["weekly_plan_weekday"])).isoformat()
        today = now.date().isoformat()
        days_in = (now.date() - date.fromisoformat(week)).days
        # On flyer day after the set hour, or the next day as a catch-up (e.g. the server was down) — never later,
        # so switching the plan on mid-week doesn't start one immediately.
        due_plan = (jobs["weekly_plan_enabled"] and _marker(session, "weekly_plan") != week and not weekly.running()
                    and ((days_in == 0 and now.hour >= int(jobs["weekly_plan_hour"])) or days_in == 1))
        due_checkin = jobs["checkins_enabled"] and now.hour >= int(jobs["checkin_hour"]) and _marker(session, "checkins") != today
        due_orders = jobs["order_import_enabled"] and now.hour >= 20 and _marker(session, "orders") != today
        due_deals = (settings.deal_scan_hours > 0 and now.hour >= int(jobs["deals_hour"]) and _marker(session, "deals") != today
                     and not deals_job.running())
        if due_deals:
            _set_marker(session, "deals", today)
        due_backup = now.hour >= 3 and _marker(session, "backup") != today
        if due_backup:
            _set_marker(session, "backup", today)
        due_keepalive = now.hour >= 4 and _marker(session, "pcid_keepalive") != today
        if due_keepalive:
            _set_marker(session, "pcid_keepalive", today)
        if due_plan:
            _set_marker(session, "weekly_plan", week)
        if due_checkin:
            _set_marker(session, "checkins", today)
        if due_orders and context.auth.status().state == "connected":
            _set_marker(session, "orders", today)
        else:
            due_orders = False
    if due_deals:
        log.info("daily deals: scanning and assessing")
        deals_job.start(scan=True)
    if due_backup:
        try:
            log.info("backup written: %s", await asyncio.to_thread(backup_database))
        except Exception as exc:  # noqa: BLE001
            log.warning("backup failed: %s", exc)
    if due_keepalive:
        log.info("No Frills session: %s", await keep_session_alive())
    if due_plan:
        log.info("weekly plan: starting for week of %s", week)
        weekly.start_in_background()
    if due_checkin:
        try:
            log.info("meal check-ins: %s", await checkins())
        except Exception as exc:  # noqa: BLE001 - Notion may not be configured yet
            log.info("meal check-ins skipped: %s", exc)
    if due_orders:
        try:
            log.info("order import: %s new", await import_orders())
        except Exception as exc:  # noqa: BLE001
            log.warning("order import failed: %s", exc)


async def run_forever() -> None:
    await asyncio.sleep(30)
    tz = ZoneInfo(settings.timezone)
    while True:
        try:
            await tick(datetime.now(tz))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("scheduler tick failed")
        await asyncio.sleep(60)
