"""Past orders: import what was actually bought, match it to run items, and show the gaps.

The cart only says what was *approved*; the order history says what was *purchased* (people often
edits the cart on nofrills.ca before checking out). Importing an order records each line, the price
actually paid (as a price observation), and which run item suggested it.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from shopping.core import service
from shopping.models import SEL_APPLIED, PastOrder, PastOrderLine, Selection
from shopping.retailer.normalize import PastOrderData, PastOrderSummary, observation_from_order_line, round2

MATCH_WINDOW = timedelta(days=14)  # a run item counts towards an order placed within two weeks of adding it


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def import_order(session: Session, data: PastOrderData, summary: PastOrderSummary | None, *, store_id: str | None) -> PastOrder:
    """Create or refresh an imported order. Price observations are recorded on first import only."""
    order = session.get(PastOrder, data.order_id)
    first_import = order is None
    if order is None:
        order = PastOrder(id=data.order_id)
        session.add(order)
    if summary is not None:
        order.placed_at = _parse_time(summary.placed_at) or order.placed_at
        order.store, order.fulfillment = summary.store, summary.fulfillment
    order.subtotal, order.tax, order.total, order.points_earned = data.subtotal, data.tax, data.total, data.points_earned
    from_this_store = store_id if (order.store or "").upper().find("NOFRILLS") >= 0 else None
    order.lines.clear()
    session.flush()
    for i, line in enumerate(data.lines):
        product = None
        obs = observation_from_order_line(line)
        if obs is not None:
            if first_import:
                o = service.record_observation(session, obs, source="order", store_id=from_this_store, dedupe=False)
                if order.placed_at:
                    o.observed_at = order.placed_at
                product = o.product
            else:
                product = service.upsert_product(session, obs)
        order.lines.append(PastOrderLine(
            position=i, product_id=product.id if product else None, retailer_product_id=line.product_id, name=line.name,
            brand=line.brand, size=line.size, quantity=line.quantity, weight_kg=line.weight_kg, unit_price=line.unit_price,
            total=line.total, fulfilled=line.fulfilled, is_fee=line.is_fee,
            substitution_allowed=line.substitution_allowed, substituted_with=line.substituted_with,
        ))
    session.flush()
    _match_selections(session, order)
    return order


def _window_selections(session: Session, order: PastOrder) -> list[Selection]:
    if order.placed_at is None:
        return []
    return list(session.scalars(
        select(Selection).where(Selection.status == SEL_APPLIED, Selection.applied_to_cart_at <= order.placed_at,
                                Selection.applied_to_cart_at >= order.placed_at - MATCH_WINDOW)
        .order_by(Selection.applied_to_cart_at.desc())
    ))


def _match_selections(session: Session, order: PastOrder) -> None:
    by_product: dict[int, Selection] = {}
    for sel in _window_selections(session, order):
        by_product.setdefault(sel.product_id, sel)  # most recent wins
    for line in order.lines:
        sel = by_product.get(line.product_id) if line.product_id else None
        line.selection_id = sel.id if sel else None
    session.flush()


def _qty_text(line: PastOrderLine) -> str:
    if line.weight_kg:
        return f"{line.weight_kg:g} kg"
    if line.quantity:
        return f"{line.quantity:g}"
    return "—"


def order_view(session: Session, order: PastOrder) -> dict:
    lines = [l for l in order.lines if not l.is_fee]
    fees = [l for l in order.lines if l.is_fee]
    bought_ids = {l.product_id for l in lines if l.fulfilled and l.product_id}
    approved_not_bought = [
        {"selection_id": s.id, "run_id": s.shopping_run_id, "run": s.run.summary, "requirement": s.requirement,
         "product_id": s.product.retailer_product_id, "product": service.product_label(s.product), "quantity": s.quantity}
        for s in _window_selections(session, order) if s.product_id not in bought_ids
    ]
    return {
        "order_id": order.id,
        "placed_at": order.placed_at.isoformat() if order.placed_at else None,
        "store": order.store,
        "fulfillment": order.fulfillment,
        "subtotal": round2(order.subtotal), "tax": round2(order.tax), "total": round2(order.total),
        "points_earned": order.points_earned,
        "item_count": len(lines),
        "items": [
            {"product_id": l.retailer_product_id, "name": l.name, "brand": l.brand, "size": l.size, "quantity": _qty_text(l),
             "unit_price": round2(l.unit_price), "per": "kg" if l.weight_kg else "ea", "total": round2(l.total), "fulfilled": l.fulfilled,
             "substitution_allowed": l.substitution_allowed, "substituted_with": l.substituted_with,
             "from_run": None if not l.selection else {"run_id": l.selection.shopping_run_id, "requirement": l.selection.requirement}}
            for l in lines
        ],
        "not_fulfilled": [l.name for l in lines if not l.fulfilled],
        "substituted": [{"name": l.name, "with": l.substituted_with} for l in lines if l.substituted_with],
        "from_runs": sum(1 for l in lines if l.selection_id),
        "added_outside_app": [l.name for l in lines if l.fulfilled and not l.selection_id],
        "approved_in_app_but_not_bought": approved_not_bought,
        "fees_and_tips": [{"name": l.name, "total": round2(l.total)} for l in fees],
    }


def summary_dict(s: PastOrderSummary, imported: PastOrder | None) -> dict:
    return {"order_id": s.order_id, "placed_at": s.placed_at, "total": s.total, "store": s.store, "fulfillment": s.fulfillment,
            "imported": imported is not None}


def purchases_for_products(session: Session, product_ids: set[int]) -> list[tuple[datetime, PastOrderLine]]:
    """(placed_at, line) for fulfilled purchases of these products, oldest first."""
    if not product_ids:
        return []
    rows = session.execute(
        select(PastOrder.placed_at, PastOrderLine)
        .join(PastOrderLine, PastOrderLine.order_id == PastOrder.id)
        .where(PastOrderLine.product_id.in_(product_ids), PastOrderLine.fulfilled.is_(True), PastOrder.placed_at.is_not(None))
        .order_by(PastOrder.placed_at)
    ).all()
    return [(t, l) for t, l in rows]

