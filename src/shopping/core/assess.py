"""Daily deal assessment: keep "Today's top deals" current without an LLM or a No Frills session.

After the morning deal scan, rank what's on sale against the household's own buying history and keep a short
list on the Deals page (TopDeal rows with added_by="auto"):

  stock up (★★★)  something they've bought, ≥40% off; or ≥25% off and the lowest price seen over 2+ weeks of history
                  → also a push alert, once
  great    (★★)   something they've bought, ≥25% off (or ≥15% off and the lowest seen over 2+ weeks);
                  or ≥40% off and the lowest seen, in a category they buy
"Lowest seen" only counts with at least two weeks of price history: with a few days, everything is the lowest.
Dismissed deals stay dismissed (60 days); auto deals that no longer qualify (price back up, sale ended) retire.
Deals flagged by the assistant or added by a person are left alone.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from shopping.core import catalog, service
from shopping.models import DEAL_ACTIVE, DEAL_DISMISSED, DEAL_RETIRED, PriceObservation, Product, TopDeal, utcnow

MAX_DEALS = 12
STOCK_UP_PCT, GREAT_PCT, EXCEPTIONAL_PCT = 40.0, 25.0, 40.0
HISTORY_DAYS = 14  # minimum days of price history before "lowest seen" means anything


def _history_days(stats: dict) -> int:
    """How long we've been watching this product's price (first observation to today)."""
    from datetime import date

    first = stats.get("first_observed")
    return (date.today() - date.fromisoformat(first)).days if first else 0


def _latest_obs(session: Session, product: Product) -> PriceObservation | None:
    return session.scalar(select(PriceObservation).where(PriceObservation.product_id == product.id, PriceObservation.source != "order")
                          .order_by(PriceObservation.observed_at.desc()).limit(1))


def _why(p: Product, bought: dict | None, stats: dict, pct: float | None) -> str:
    bits = []
    if bought:
        bits.append(f"you've bought it {bought['times']}×")
    price = float(p.current_price) if p.current_price is not None else None
    if pct and p.current_regular_price:
        bits.append(f"${price:.2f}, {pct:g}% off ${float(p.current_regular_price):.2f}")
    avg = stats.get("avg_90d")
    if avg and price is not None and _history_days(stats) >= HISTORY_DAYS and avg > price * 1.02:
        bits.append(f"usually about ${avg:.2f}")
    if stats.get("is_lowest_observed") and _history_days(stats) >= HISTORY_DAYS:
        bits.append("lowest price we've seen")
    if p.current_deal_expiry:
        bits.append(f"ends {p.current_deal_expiry}")
    text = "; ".join(bits)
    return (text[:1].upper() + text[1:] + ".") if text else ""


def assess(session: Session, *, store_id: str | None = None) -> dict:
    """Refresh the auto top-deals list. Returns what changed, including new stock-up deals to alert on."""
    ranked, bought = catalog._ranked_deals(session, aisles=None, food_only=False)  # noqa: SLF001 - same ranking as the context
    since = utcnow() - timedelta(days=60)
    dismissed = set(session.scalars(select(TopDeal.product_id).where(TopDeal.status == DEAL_DISMISSED, TopDeal.updated_at >= since)))
    bought_cats = {p.category_code for p in session.scalars(select(Product).where(Product.id.in_(list(bought)))) if p.category_code}

    picks: list[tuple[int, Product, dict]] = []
    for p in ranked:
        if p.id in dismissed:
            continue
        pct = catalog._discount_pct(p) or 0.0  # noqa: SLF001
        stats = service.price_stats(session, p, store_id=store_id)
        lowest = bool(stats.get("is_lowest_observed")) and _history_days(stats) >= HISTORY_DAYS
        mine = p.id in bought
        if mine and (pct >= STOCK_UP_PCT or (lowest and pct >= GREAT_PCT)):
            rating = 3
        elif (mine and (pct >= GREAT_PCT or (lowest and pct >= 15))) or (p.category_code in bought_cats and pct >= EXCEPTIONAL_PCT and lowest):
            rating = 2
        else:
            continue
        picks.append((rating, p, stats))
        if len(picks) >= MAX_DEALS * 2:
            break
    picks.sort(key=lambda x: (-x[0], -(catalog._discount_pct(x[1]) or 0)))  # noqa: SLF001
    picks = picks[:MAX_DEALS]

    active_auto = {d.product_id: d for d in session.scalars(select(TopDeal).where(TopDeal.status == DEAL_ACTIVE, TopDeal.added_by == "auto"))}
    others = set(session.scalars(select(TopDeal.product_id).where(TopDeal.status == DEAL_ACTIVE, TopDeal.added_by != "auto")))
    keep, new, alerts = set(), 0, []
    for rating, p, stats in picks:
        if p.id in others:
            continue  # already on the page (assistant or person); don't duplicate
        pct = catalog._discount_pct(p)  # noqa: SLF001
        obs = _latest_obs(session, p)
        deal = active_auto.get(p.id)
        if deal is None:
            deal = TopDeal(product_id=p.id, added_by="auto", relevant_to=None)
            session.add(deal)
            new += 1
        was_rating = deal.rating if deal.id else 0
        deal.price_observation_id = obs.id if obs else None
        deal.rating = rating
        deal.headline = ("Lowest we've seen" if stats.get("is_lowest_observed") and _history_days(stats) >= HISTORY_DAYS
                         else f"{pct:g}% off" if pct else (p.current_deal_text or "Good price"))[:128]
        deal.why = _why(p, bought.get(p.id), stats, pct)
        deal.suggested_quantity = deal.suggested_quantity or 1
        keep.add(p.id)
        session.flush()
        if rating == 3 and was_rating < 3:
            alerts.append(deal)
    retired = 0
    for pid, deal in active_auto.items():
        if pid not in keep:
            deal.status, deal.acted_at, deal.response_note = DEAL_RETIRED, utcnow(), "No longer a top deal (price or sale changed)"
            retired += 1
    session.flush()
    return {"top_deals": len(keep), "new": new, "retired": retired, "stock_up_alerts": alerts}
