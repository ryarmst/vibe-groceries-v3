"""LLM deal review: find great deals the rules can't (things the household hasn't bought yet).

After the rule-based assessment, send the model ~60 strong candidate deals from across the store plus a short
household profile (what's kept in the pantry, Shopping/Store Preferences, recent notes, deals turned down) and
ask for up to 6 discoveries with reasons. It can only pick from the candidate list (ids are validated), so it
can't invent products or prices. Picks are TopDeal rows with added_by="review"; earlier picks it doesn't repeat
are retired. One call per assessment; skipped quietly if no LLM is configured.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from shopping import appsettings, context, prompts
from shopping.config import settings
from shopping.agent import budget, playbooks
from shopping.agent.providers import registry as providers
from shopping.agent.providers.base import ProviderError, TextDelta, Usage
from shopping.core import catalog, service
from shopping.database import session_scope
from shopping.models import DEAL_ACTIVE, DEAL_DISMISSED, DEAL_RETIRED, PriceObservation, Product, TopDeal, utcnow

log = logging.getLogger(__name__)

MAX_CANDIDATES, MAX_PICKS, PER_CATEGORY = 60, 6, 3
SKIP_AISLES = {"Baby"}

def system_prompt() -> tuple[str, str]:
    """The "Daily deal picks" prompt (editable on the Prompts page) plus the household's own notes, and its tier."""
    with session_scope() as session:
        body, tier, _ = prompts.current(session, "jobs/deal_review")
    fill = {"max_picks": MAX_PICKS, "household": settings.household}
    text = "\n\n".join(x for x in (prompts.render(body, fill), prompts.render(playbooks.about(), fill)) if x)
    return text, tier or "fast"


def candidates(session: Session) -> list[dict]:
    ranked, bought = catalog._ranked_deals(session, aisles=None, food_only=False)  # noqa: SLF001
    # Deals already listed by the rules, the assistant in chat or a person aren't offered again; the review's own
    # current picks are (marked), so it can keep them instead of churning them daily.
    shown = set(session.scalars(select(TopDeal.product_id).where(TopDeal.status == DEAL_ACTIVE, TopDeal.added_by != "review")))
    own = set(session.scalars(select(TopDeal.product_id).where(TopDeal.status == DEAL_ACTIVE, TopDeal.added_by == "review")))
    since = utcnow() - timedelta(days=60)
    dismissed = set(session.scalars(select(TopDeal.product_id).where(TopDeal.status == DEAL_DISMISSED, TopDeal.updated_at >= since)))
    per_cat: Counter = Counter()
    out = []
    for p in ranked:
        if p.id in shown or p.id in dismissed or (p.aisle or "") in SKIP_AISLES:
            continue
        pct = catalog._discount_pct(p) or 0  # noqa: SLF001
        b = catalog.brief(p, bought.get(p.id), with_history=session)
        if pct < 20 and not b.get("is_lowest_seen"):
            continue
        key = p.category_code or p.aisle
        if per_cat[key] >= PER_CATEGORY:
            continue
        per_cat[key] += 1
        item = {k: b.get(k) for k in ("product_id", "brand", "name", "size", "category", "price", "regular_price", "discount_pct",
                                      "unit_price", "unit_price_basis", "deal", "deal_ends", "is_lowest_seen", "bought_before") if b.get(k) is not None}
        if p.id in own:
            item["already_your_pick"] = True
        out.append(item)
        if len(out) >= MAX_CANDIDATES:
            break
    return out


async def household_profile(session: Session) -> dict:
    profile: dict = {}
    try:
        from shopping.notion.kitchen import open_kitchen

        async with open_kitchen() as k:
            rows = await k.pantry()
        profile["pantry"] = [
            {k2: v for k2, v in {"item": r["Name"], "type": r.get("Type"), "store": r.get("Store Preference"),
                                 "preferences": (r.get("Shopping Preferences") or "")[:200] or None}.items() if v}
            for r in rows if r.get("Type") not in ("Equipment", "Utility")][:250]
    except Exception as exc:  # noqa: BLE001 - Notion is optional for this
        profile["pantry"] = f"unavailable ({exc})"
    ctx = catalog.shopping_context(session, per_aisle=1)
    profile["recent_notes"] = ctx.get("recent_review_notes", [])[:12]
    since = utcnow() - timedelta(days=60)
    profile["turned_down"] = [
        {"item": service.product_label(d.product), "note": d.response_note}
        for d in session.scalars(select(TopDeal).where(TopDeal.status == DEAL_DISMISSED, TopDeal.updated_at >= since).limit(20))]
    return profile


def _parse(text: str) -> list[dict]:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    picks = data.get("picks") if isinstance(data, dict) else None
    return [p for p in picks or [] if isinstance(p, dict)]


async def _ask(system: str, user: str, tier: str = "fast") -> tuple[str, tuple[str, str], Usage]:
    """One call on the prompt's tier (fast by default: a separate free quota from the shopping/meals model),
    falling back in order."""
    import asyncio

    from shopping.agent import quota
    from shopping.agent.loop import _candidates

    with session_scope() as session:
        llm = appsettings.get(session, "llm")
        budget.check(session)
    last: Exception | None = None
    for provider_name, model in _candidates(llm, tier):
        with session_scope() as session:
            ok, reason, wait = quota.check(session, llm, provider_name, model)
        if not ok:
            last = ProviderError(reason or "quota")
            continue
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            with session_scope() as session:
                provider = providers.build(session, context.vault, provider_name)
            text, usage = "", Usage()
            quota.note_request(provider_name, model)
            async for ev in provider.stream(system=system, messages=[{"role": "user", "content": user}], tools=[],
                                            model=model, temperature=0.3, max_tokens=8000):  # thinking models spend tokens first
                if isinstance(ev, TextDelta):
                    text += ev.text
                elif isinstance(ev, Usage):
                    usage = ev
            return text, (provider_name, model), usage
        except ProviderError as exc:
            quota.classify_error(provider_name, model, exc)
            last = exc
    raise last or ProviderError("No LLM configured.")


async def review(*, store_id: str | None = None) -> dict:
    """Ask the model for discoveries; returns {"picks": n, "new": [...TopDeal ids], "skipped": reason?}."""
    with session_scope() as session:
        cands = candidates(session)
        profile = await household_profile(session)
    if not cands:
        return {"picks": 0, "new": [], "skipped": "no candidates"}
    user = json.dumps({"candidates": cands, "household": profile}, default=str)
    try:
        system, tier = system_prompt()
        text, used, usage = await _ask(system, user, tier)
    except (ProviderError, budget.BudgetExceeded) as exc:
        log.info("deal review skipped: %s", exc)
        return {"picks": 0, "new": [], "skipped": str(exc)}
    by_id = {c["product_id"]: c for c in cands}
    picks = [p for p in _parse(text) if p.get("product_id") in by_id][:MAX_PICKS]
    with session_scope() as session:
        budget.record(session, provider=used[0], model=used[1], tokens_in=usage.input_tokens, tokens_out=usage.output_tokens, thread_id=None)
        previous = {d.product_id: d for d in session.scalars(select(TopDeal).where(TopDeal.status == DEAL_ACTIVE, TopDeal.added_by == "review"))}
        keep, new_ids, alerts = set(), [], []
        for pick in picks:
            product = session.scalar(select(Product).where(Product.retailer_product_id == pick["product_id"]))
            if product is None:
                continue
            obs = session.scalar(select(PriceObservation).where(PriceObservation.product_id == product.id, PriceObservation.source != "order")
                                 .order_by(PriceObservation.observed_at.desc()).limit(1))
            deal = previous.get(product.id)
            is_new = deal is None
            if is_new:
                deal = TopDeal(product_id=product.id, added_by="review")
                session.add(deal)
            rating = 3 if pick.get("rating") == 3 else 2
            was = deal.rating if not is_new else 0
            deal.price_observation_id = obs.id if obs else None
            deal.rating = rating
            deal.headline = str(pick.get("headline") or "Worth a try")[:128]
            deal.why = str(pick.get("why") or "")[:600] or None
            deal.relevant_to = (str(pick.get("for") or "").strip()[:128]) or None
            deal.suggested_quantity = deal.suggested_quantity or 1
            session.flush()
            keep.add(product.id)
            if is_new:
                new_ids.append(deal.id)
            if rating == 3 and was < 3:
                alerts.append(deal.id)
        for pid, deal in previous.items():
            if pid not in keep:
                deal.status, deal.acted_at, deal.response_note = DEAL_RETIRED, utcnow(), "Not picked in the latest review"
    log.info("deal review: %s picks from %s candidates (%s/%s)", len(keep), len(cands), *used)
    return {"picks": len(keep), "new": new_ids, "alerts": alerts, "model": f"{used[0]}/{used[1]}"}
