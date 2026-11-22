"""The deals job: scan (public price data, no No Frills session needed), assess, alert on stock-up deals."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from shopping import appsettings, context
from shopping.config import settings
from shopping.core import assess, catalog, deals, inbox
from shopping.database import session_scope
from shopping.models import utcnow
from shopping.notify import push

log = logging.getLogger(__name__)
_task: asyncio.Task | None = None


async def run(*, scan: bool = True) -> dict:
    """Scan (when asked and the last scan is older than 20 h), then refresh Today's top deals and alert."""
    with session_scope() as session:
        last = catalog.last_scan(session)
        jobs = appsettings.get(session, "jobs")
        alerts_on, review_on = jobs.get("deal_alerts", True), jobs.get("deal_review", True)
    if scan and (last is None or last.started_at < utcnow() - timedelta(hours=20)) and not catalog.scan_running():
        await catalog.scan_deals(context.client(), session_scope, store_id=settings.store_id)
    with session_scope() as session:
        result = assess.assess(session, store_id=settings.store_id)
        alert_ids = [d.id for d in result.pop("stock_up_alerts")]
    if review_on:
        from shopping.agent import deal_review

        try:
            reviewed = await deal_review.review(store_id=settings.store_id)
        except Exception as exc:  # noqa: BLE001 - the rule-based list stands on its own
            log.warning("deal review failed: %s", exc)
            reviewed = {"picks": 0, "alerts": [], "skipped": str(exc)}
        result["assistant_picks"] = reviewed.get("picks", 0)
        if reviewed.get("skipped"):
            result["review_skipped"] = reviewed["skipped"]
        alert_ids += reviewed.get("alerts", [])
    with session_scope() as session:
        alerts = [deals.deal_dict(session, deals.get_deal(session, i), store_id=settings.store_id) for i in alert_ids]
        for a in alerts:
            price = f" ${a['price']:.2f}" if a.get("price") is not None else ""
            inbox.post(session, kind="deal_alert", title=f"Stock-up price: {a['product']}{price}",
                       body=a["why"], link="#/deals", dedupe_key=f"dealalert:{a['deal_id']}")
    result["alerts"] = len(alerts)
    if alerts and alerts_on:
        lines = [f"• {a['product']}: ${a['price']:.2f}" + (f" (was ${a['regular_price']:.2f})" if a.get("regular_price") else "")
                 + (f", ends {a['ends']}" if a.get("ends") else "") for a in alerts[:8]]
        try:
            await push.send(f"{len(alerts)} stock-up deal{'s' if len(alerts) > 1 else ''} at No Frills", "\n".join(lines),
                            click=alerts[0].get("link"))
        except Exception as exc:  # noqa: BLE001 - an alert failing must not fail the job
            log.warning("deal alert push failed: %s", exc)
    log.info("deal assessment: %s", result)
    return result


def start(*, scan: bool = True) -> bool:
    global _task
    if running():
        return False
    _task = asyncio.create_task(run(scan=scan), name="deal-assessment")
    return True


def running() -> bool:
    return (_task is not None and not _task.done()) or catalog.scan_running()
