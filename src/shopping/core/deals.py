"""Top deals: standout prices the agent flags for the Deals page, independent of any run.

The user can add a deal straight to the cart or dismiss it (with a reason, recorded as feedback so
the agent learns which deals are worth flagging). Deals whose sale has ended retire themselves.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from shopping.core import service
from shopping.core.feedback import clean_tags
from shopping.core.service import ShoppingError, product_label
from shopping.models import (
    DEAL_ACTIVE, DEAL_ADDED, DEAL_DISMISSED, DEAL_RETIRED, FEEDBACK_TAG_LABELS, Feedback, Product, TopDeal, utcnow,
)
from shopping.retailer.normalize import NormalizedProduct, round2

RATINGS = {1: "Good", 2: "Great", 3: "Stock up"}
STATUS_LABELS = {DEAL_ACTIVE: "Active", DEAL_ADDED: "Added to cart", DEAL_DISMISSED: "Dismissed", DEAL_RETIRED: "Ended"}


def _expiry_day(text: str | None) -> str | None:
    """Retailer expiry like '2026-11-25T00:00:00Z' -> '2026-11-25', the last day of the sale. The date is taken
    as written: the time part is a placeholder (sales end on Wednesdays), so converting zones would lose a day."""
    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:10]).date().isoformat()
    except ValueError:
        return None


def today() -> str:
    return utcnow().astimezone(service.LOCAL_TZ).date().isoformat()


def get_deal(session: Session, deal_id: int) -> TopDeal:
    deal = session.get(TopDeal, deal_id)
    if deal is None:
        raise ShoppingError(f"Unknown deal {deal_id}.")
    return deal


def flag_deal(session: Session, np: NormalizedProduct, *, headline: str | None, why: str | None, relevant_to: str | None = None,
              rating: int = 2, suggested_quantity: float = 1, added_by: str = "agent", store_id: str | None = None) -> TopDeal:
    """Add a product to the Deals page at its current price. Re-flagging an active deal refreshes it."""
    if rating not in RATINGS:
        raise ShoppingError("rating must be 1 (good), 2 (great) or 3 (stock up).")
    if np.availability and "OUT" in np.availability.upper():
        raise ShoppingError(f"{np.name} is out of stock; not worth flagging.")
    suggested_quantity = service.whole_quantity(suggested_quantity, np.sold_by)
    obs = service.record_observation(session, np, source="product", store_id=store_id)
    if not headline:
        headline = f"{np.discount_pct:g}% off" if np.discount_pct else (np.deal_text or "Good price")
    deal = session.scalar(select(TopDeal).where(TopDeal.product_id == obs.product_id, TopDeal.status == DEAL_ACTIVE))
    if deal is None:
        deal = TopDeal(product_id=obs.product_id, added_by=added_by)
        session.add(deal)
    deal.price_observation_id = obs.id
    deal.headline = headline.strip()[:128]
    deal.why = (why or "").strip() or deal.why
    deal.relevant_to = (relevant_to or "").strip()[:128] or deal.relevant_to
    deal.rating, deal.suggested_quantity = rating, suggested_quantity
    session.flush()
    return deal


def retire_expired(session: Session) -> int:
    """Deals whose sale has ended leave the active list."""
    today_, n = today(), 0
    for deal in session.scalars(select(TopDeal).where(TopDeal.status == DEAL_ACTIVE)):
        end = _expiry_day(deal.observation.deal_expiry if deal.observation else None)
        if end and end < today_:
            deal.status, deal.acted_at, deal.response_note = DEAL_RETIRED, utcnow(), "Sale ended"
            n += 1
    session.flush()
    return n


def list_deals(session: Session, *, include_closed: bool = False, days: int = 30) -> list[TopDeal]:
    retire_expired(session)
    q = select(TopDeal)
    if include_closed:
        q = q.where((TopDeal.status == DEAL_ACTIVE) | (TopDeal.updated_at >= utcnow() - timedelta(days=days)))
    else:
        q = q.where(TopDeal.status == DEAL_ACTIVE)
    deals = list(session.scalars(q))
    deals.sort(key=lambda d: (d.status != DEAL_ACTIVE, -d.rating, -(d.created_at.timestamp())))
    return deals


def deal_dict(session: Session, deal: TopDeal, *, store_id: str | None = None) -> dict:
    p: Product = deal.product
    obs = deal.observation
    stats = service.price_stats(session, p, store_id=store_id)
    price = obs.effective_price if obs else None
    regular = obs.regular_price if obs else None
    pct = round(float((regular - price) / regular * 100), 1) if price is not None and regular and regular > price else None
    current = stats.get("current_price")
    return {
        "deal_id": deal.id,
        "status": deal.status,
        "status_label": STATUS_LABELS.get(deal.status, deal.status),
        "flagged_at": deal.created_at.isoformat(),
        "added_by": deal.added_by,
        "headline": deal.headline,
        "why": deal.why,
        "relevant_to": deal.relevant_to,
        "rating": deal.rating,
        "rating_label": RATINGS.get(deal.rating),
        "suggested_quantity": deal.suggested_quantity,
        "product_id": p.retailer_product_id,
        "product": product_label(p),
        "brand": p.brand, "name": p.name, "size": p.raw_size, "sold_by": p.sold_by, "aisle": p.aisle,
        "image_url": p.image_url,
        "link": f"https://www.nofrills.ca{p.link}" if p.link and p.link.startswith("/") else p.link,
        "price": round2(price),
        "regular_price": round2(regular),
        "discount_pct": pct,
        "unit_price": None if not obs or obs.unit_price is None else round(float(obs.unit_price), 4),
        "unit_price_basis": obs.unit_price_basis if obs else None,
        "deal_text": obs.deal_text if obs else None,
        "pc_optimum": obs.pc_optimum_text if obs else None,
        "ends": _expiry_day(obs.deal_expiry if obs else None),
        "latest_price": current,
        "price_changed_since_flagged": current is not None and price is not None and current != round2(price),
        "lowest_seen": stats.get("min_price"),
        "avg_90d": stats.get("avg_90d"),
        "days_observed": stats.get("days_observed"),
        "acted_at": deal.acted_at.isoformat() if deal.acted_at else None,
        "response_note": deal.response_note,
    }


async def add_to_cart(session: Session, client, deal_id: int, quantity: float) -> str:
    """User action: add the deal's product to the cart (on top of anything already there)."""
    deal = get_deal(session, deal_id)
    if deal.status != DEAL_ACTIVE:
        raise ShoppingError("This deal is no longer active.")
    quantity = service.whole_quantity(quantity, deal.product.sold_by)
    cart = await client.get_cart()
    pid = deal.product.retailer_product_id
    before = cart.quantity_of(pid)
    await service.user_set_cart_quantity(session, client, pid, before + quantity, source="deals")
    deal.status, deal.acted_at = DEAL_ADDED, utcnow()
    session.flush()
    return f"Added {quantity:g} × {product_label(deal.product)} to the cart" + (f" (now {before + quantity:g})." if before else ".")


def dismiss(session: Session, deal_id: int, *, tags: list[str] | None = None, note: str | None = None) -> TopDeal:
    """User action: not interested. Recorded as feedback so the agent flags better deals next time."""
    deal = get_deal(session, deal_id)
    tags = clean_tags("deal_dismissed", tags)
    note = (note or "").strip() or None
    deal.status, deal.acted_at = DEAL_DISMISSED, utcnow()
    deal.response_note = "; ".join([FEEDBACK_TAG_LABELS.get(t, t) for t in tags] + ([note] if note else [])) or None
    price = deal.observation.effective_price if deal.observation else None
    session.add(Feedback(action="deal_dismissed", product_id=deal.product_id, requirement=deal.relevant_to, tags=tags, note=note,
                         context=json.dumps({"from": {"price": float(price) if price is not None else None, "agent_rationale": deal.why,
                                                      "headline": deal.headline, "deal_id": deal.id}})))
    session.flush()
    return deal


def retire(session: Session, deal_id: int, note: str | None = None) -> TopDeal:
    """Agent action: take a deal down (price went back up, out of stock, flagged by mistake)."""
    deal = get_deal(session, deal_id)
    if deal.status == DEAL_ACTIVE:
        deal.status, deal.acted_at, deal.response_note = DEAL_RETIRED, utcnow(), (note or "").strip() or "Retired by agent"
        session.flush()
    return deal


def reopen(session: Session, deal_id: int) -> TopDeal:
    deal = get_deal(session, deal_id)
    if deal.status in (DEAL_DISMISSED, DEAL_RETIRED):
        deal.status, deal.acted_at, deal.response_note = DEAL_ACTIVE, None, None
        session.flush()
    return deal
