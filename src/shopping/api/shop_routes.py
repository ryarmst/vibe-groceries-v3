"""Runs and triage, the cart, modifying an item, deals, orders and history."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from shopping import context
from shopping.api.deps import require_user
from shopping.auth.session import AuthRequired
from shopping.config import settings
from shopping.core import activity as act
from shopping.core import catalog, views
from shopping.core import deals as dls
from shopping.core import feedback as fbk
from shopping.core import modify as mod
from shopping.core import orders as ords
from shopping.core import service
from shopping.core.cart_queue import queue as cart_queue
from shopping.database import session_scope
from shopping.models import (
    SEL_APPROVED, SEL_FAILED, PastOrder, PriceObservation, Product, Selection, utcnow,
)
from shopping.retailer.client import RetailerError

router = APIRouter(prefix="/api", dependencies=[Depends(require_user)])


def _connected() -> bool:
    return context.auth.status().state == "connected"


def _to_add(session, run_id: str, sel_ids: list[str] | None) -> list[str]:
    run = service.get_run(session, run_id)
    return [s.id for s in service.chosen_selections(run)
            if s.status in (SEL_APPROVED, SEL_FAILED) and s.approved_at and (sel_ids is None or s.id in sel_ids)]


def _queue_cart(run_id: str, ids: list[str]) -> dict:
    """Hand approved items to the background cart worker (after the decision is committed)."""
    if not ids:
        return {"queued": 0}
    if not _connected():
        return {"queued": 0, "needs_session": True,
                "message": "Approved. No Frills isn't signed in: sign in under Settings → No Frills account and the items go into the cart."}
    n = cart_queue.enqueue(run_id, ids)
    return {"queued": n, "message": f"Adding {n} item(s) to the cart…" if n else None}


# -- runs ----------------------------------------------------------------------------------

@router.get("/runs")
def runs(limit: int = 30):
    with session_scope() as session:
        return {"runs": views.runs_list(session, limit=min(limit, 100))}


@router.get("/runs/{run_id}")
def run(run_id: str):
    with session_scope() as session:
        return views.run_view(session, service.get_run(session, run_id), pending_ids=cart_queue.pending(run_id))


class TriageBody(BaseModel):
    decision: str = Field(pattern="^(keep|remove|choose)$")
    option: str | None = None
    note: str | None = Field(default=None, max_length=2000)
    quantity: float | None = Field(default=None, gt=0, le=99)
    sub_allowed: bool = False
    sub_note: str | None = Field(default=None, max_length=250)


@router.post("/runs/{run_id}/selections/{sel_id}/triage")
async def triage(run_id: str, sel_id: str, body: TriageBody):
    """One review decision. The note is free text; the assistant learns from it."""
    decision = body.decision
    if decision == "keep" and body.option and body.option != sel_id:
        decision = "choose"
    ids: list[str] = []
    with session_scope() as session:
        sel = session.get(Selection, sel_id)
        if sel is None or sel.shopping_run_id != run_id:
            raise service.ShoppingError("That item isn't part of this run.")
        if decision in ("keep", "choose"):
            target = session.get(Selection, body.option if decision == "choose" else sel_id)
            if target is None:
                raise service.ShoppingError("Unknown option.")
            await service.set_substitution(session, context.client(), selection_id=target.id,
                                           allowed=views.subs_allowed(body.sub_allowed, body.sub_note or "", target.substitution_note),
                                           note=body.sub_note, in_cart=False)
        fbk.triage(session, sel_id, decision=decision, chosen_selection_id=body.option or None, note=body.note, quantity=body.quantity)
        if decision in ("keep", "choose"):
            ids = _to_add(session, run_id, [body.option if decision == "choose" else sel_id])
    return {"ok": True, **_queue_cart(run_id, ids)}


class SnapshotBody(BaseModel):
    snapshot: list[str] = Field(default_factory=list, max_length=500)


@router.post("/runs/{run_id}/approve-all")
async def approve_all(run_id: str, body: SnapshotBody):
    with session_scope() as session:
        n = service.approve_all_current(session, run_id, body.snapshot)
        ids = _to_add(session, run_id, None)
    return {"approved": n, **_queue_cart(run_id, ids)}


@router.post("/runs/{run_id}/reject-remaining")
def reject_remaining(run_id: str, body: SnapshotBody):
    with session_scope() as session:
        return {"removed": service.reject_remaining(session, run_id, body.snapshot)}


@router.post("/runs/{run_id}/apply")
async def apply(run_id: str):
    with session_scope() as session:
        ids = _to_add(session, run_id, None)
    return _queue_cart(run_id, ids) | ({} if ids else {"message": "Nothing waiting to be added."})


# -- cart -------------------------------------------------------------------------------------

@router.get("/cart")
async def cart():
    c = await context.client().get_cart()
    with session_scope() as session:
        lines = []
        for line in c.lines:
            sel = mod.linked_selection(session, line.product_id)
            p = session.scalar(select(Product).where(Product.retailer_product_id == line.product_id))
            lines.append({
                "product_id": line.product_id, "name": line.name, "brand": line.brand, "quantity": line.quantity,
                "unit_price": line.unit_price, "line_total": line.line_total, "regular_line_total": line.regular_line_total,
                "selling_type": line.selling_type, "substitution_allowed": line.substitution_allowed,
                "substitution_comment": line.substitution_comment, "selected_substitution": line.selected_substitution,
                "image_url": p.image_url if p else None,
                "from_run": {"run_id": sel.shopping_run_id, "summary": sel.run.summary, "requirement": sel.requirement} if sel else None,
            })
    return {"cart_id": c.cart_id, "store_id": c.store_id, "store_name": c.store_name, "subtotal": c.subtotal,
            "estimated_total": c.estimated_total, "total_discounts": c.total_discounts, "lines": lines,
            "store_mismatch": bool(c.store_id and c.store_id != settings.store_id)}


class QtyBody(BaseModel):
    quantity: float = Field(ge=0, le=99)


@router.post("/cart/{product_id}")
async def cart_set(product_id: str, body: QtyBody):
    with session_scope() as session:
        await service.user_set_cart_quantity(session, context.client(), product_id, body.quantity, source="cart_page",
                                             selection=mod.linked_selection(session, product_id))
    return {"ok": True}


@router.post("/cart/{product_id}/add")
async def cart_add(product_id: str, body: QtyBody):
    """Add to the cart (on top of what's there) from anywhere: the Deals page, product pages."""
    if not body.quantity:
        raise service.ShoppingError("Quantity must be above zero.")
    client = context.client()
    c = await client.get_cart()
    before = c.quantity_of(product_id)
    with session_scope() as session:
        await service.user_set_cart_quantity(session, client, product_id, before + body.quantity, source="deals")
    return {"ok": True, "quantity": before + body.quantity,
            "message": f"Added to the cart{f' (now {before + body.quantity:g})' if before else ''}."}


class SubsBody(BaseModel):
    allowed: bool
    note: str | None = Field(default=None, max_length=250)


@router.post("/cart/{product_id}/substitution")
async def cart_subs(product_id: str, body: SubsBody):
    with session_scope() as session:
        sel = mod.linked_selection(session, product_id)
        msg = await service.set_substitution(session, context.client(), selection_id=sel.id if sel else None, product_id=product_id,
                                             allowed=body.allowed, note=body.note if body.note is not None else (sel.substitution_note if sel else None),
                                             in_cart=True)
    return {"ok": True, "message": msg}


# -- modify an item (from the cart or a run) ------------------------------------------------------

@router.get("/products/{product_id}/modify")
async def modify_context(product_id: str, selection_id: str | None = None, q: str | None = None):
    client = context.client()
    with session_scope() as session:
        sel = session.get(Selection, selection_id) if selection_id else mod.linked_selection(session, product_id)
        cart_line, cart_error = None, None
        if _connected():
            try:
                c = await client.get_cart()
                cart_line = next((l for l in c.lines if l.product_id == product_id), None)
            except (AuthRequired, RetailerError) as exc:
                cart_error = str(exc)
        current = None
        try:
            current = await client.get_product(product_id)
            service.record_observation(session, current, source="product", store_id=settings.store_id)
        except RetailerError as exc:
            cart_error = cart_error or str(exc)
        product = session.scalar(select(Product).where(Product.retailer_product_id == product_id))
        if product is None and cart_line is not None:
            product = service._product_from_cart_line(session, cart_line)  # noqa: SLF001
        if product is None:
            raise service.ShoppingError("Unknown product.")
        session.commit()  # don't hold the database while searching
        query = (q if q is not None else (sel.requirement if sel and sel.requirement else product.name)) or ""
        alternatives, search_error = [], None
        if query.strip():
            try:
                found = [p for p in (await client.search_products(query))["products"] if p.retailer_product_id != product_id]
                fam = product.comparison_family
                found.sort(key=lambda p: (p.comparison_family != fam, p.unit_price is None, p.unit_price or 0))
                fb_hist = fbk.feedback_by_product(session, [p.retailer_product_id for p in found[:24]])
                for p in found[:24]:
                    service.record_observation(session, p, source="search", store_id=settings.store_id)
                    alternatives.append({**p.summary(), "image_url": p.image_url,
                                         "past_feedback": fb_hist.get(p.retailer_product_id)})
            except RetailerError as exc:
                search_error = str(exc)
        options = [views.selection_card(session, s, fb_hist=None, req=None, pending_ids=set())
                   for s in (sel.choice_group.selections if sel and sel.choice_group else []) if s.id != sel.id]
        return {
            "product": views.product_brief(product),
            "current": ({**current.summary(), "image_url": current.image_url} if current else None),
            "selection": views.selection_card(session, sel, fb_hist=None, req=None,
                                              pending_ids=cart_queue.pending()) if sel else None,
            "run": {"run_id": sel.shopping_run_id, "summary": sel.run.summary} if sel else None,
            "cart_line": {"quantity": cart_line.quantity, "substitution_allowed": cart_line.substitution_allowed,
                          "substitution_comment": cart_line.substitution_comment} if cart_line else None,
            "cart_error": cart_error, "query": query, "alternatives": alternatives, "search_error": search_error,
            "options": options, "stats": service.price_stats(session, product, store_id=settings.store_id),
            "events": act.activity(session, days=365, product_id=product_id, limit=15),
        }


class ModifyBody(BaseModel):
    action: str = Field(pattern="^(swap|quantity|remove|note|restore|substitution|substitute_with)$")
    product_id: str
    selection_id: str | None = None
    new_product_id: str | None = None
    quantity: float | None = Field(default=None, ge=0, le=99)
    note: str | None = Field(default=None, max_length=2000)
    in_cart: bool = False
    sub_allowed: bool = False
    sub_note: str | None = Field(default=None, max_length=250)


@router.post("/modify")
async def modify(body: ModifyBody):
    with session_scope() as session:
        if body.action in ("substitution", "substitute_with"):
            if body.action == "substitute_with" and not body.new_product_id:
                raise service.ShoppingError("Pick a product to use as the substitute.")
            prev = session.get(Selection, body.selection_id) if body.selection_id else None
            msg = await service.set_substitution(
                session, context.client(), selection_id=body.selection_id, product_id=body.product_id,
                allowed=views.subs_allowed(body.sub_allowed, body.sub_note or "", prev.substitution_note if prev else None)
                or body.action == "substitute_with",
                note=body.sub_note, substitute_product_id=body.new_product_id if body.action == "substitute_with" else None,
                in_cart=body.in_cart)
            return {"ok": True, "message": msg}
        msg = await mod.modify(session, context.client(), action=body.action, product_id=body.product_id,
                               selection_id=body.selection_id, new_product_id=body.new_product_id, quantity=body.quantity,
                               note=body.note, in_cart=body.in_cart)
        return {"ok": True, "message": msg}


# -- deals and catalogue ----------------------------------------------------------------------------

@router.get("/deals/context")
def deals_context(focus: str = "overview", per_aisle: int = 6):
    with session_scope() as session:
        return catalog.shopping_context(session, focus=focus if focus in ("overview", "meals") else "overview", per_aisle=min(per_aisle, 20))


@router.post("/deals/assess")
async def deals_assess():
    """Refresh Today's top deals now (scanning first if the last scan is more than 20 hours old)."""
    from shopping.jobs import deals as deals_job

    with session_scope() as session:
        last = catalog.last_scan(session)
    fresh = last is not None and last.started_at > utcnow() - timedelta(hours=20)
    if deals_job.running():
        return {"started": False, "message": "Already assessing deals."}
    if fresh:
        deals_job.start(scan=False)
        return {"started": True, "message": "Ranking this morning's prices and picking the best deals…"}
    deals_job.start(scan=True)
    return {"started": True, "message": "Scanning every aisle, ranking deals, then asking the assistant: about 3–4 minutes."}


@router.get("/deals/assess")
def deals_assess_status():
    from shopping.jobs import deals as deals_job

    with session_scope() as session:
        return {"running": deals_job.running(), "last_scan": catalog.scan_dict(catalog.last_scan(session))}


@router.get("/deals/top")
def top_deals(include_closed: bool = True):
    with session_scope() as session:
        return {"deals": [dls.deal_dict(session, d, store_id=settings.store_id) for d in dls.list_deals(session, include_closed=include_closed, days=30)]}


class AddDealBody(BaseModel):
    product: str = Field(min_length=6, max_length=300)  # link or code
    headline: str | None = Field(default=None, max_length=128)
    why: str | None = Field(default=None, max_length=1000)
    relevant_to: str | None = Field(default=None, max_length=128)
    rating: int = Field(default=2, ge=1, le=3)


@router.post("/deals/top")
async def add_top_deal(body: AddDealBody):
    import re

    m = re.search(r"(\d{6,}_[A-Z]{2,4})", body.product)
    if not m:
        raise service.ShoppingError("Paste a nofrills.ca product link or a product code like 20163708001_EA.")
    np = await context.client().get_product(m.group(1))
    with session_scope() as session:
        d = dls.flag_deal(session, np, headline=body.headline, why=body.why, relevant_to=body.relevant_to, rating=body.rating,
                          added_by="user", store_id=settings.store_id)
        return dls.deal_dict(session, d, store_id=settings.store_id)


class NoteBody(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


@router.post("/deals/top/{deal_id}/dismiss")
def top_deal_dismiss(deal_id: int, body: NoteBody):
    with session_scope() as session:
        dls.dismiss(session, deal_id, note=body.note)
    return {"ok": True}


# -- orders -----------------------------------------------------------------------------------------

@router.get("/orders")
async def orders():
    summaries, error = [], None
    if _connected():
        try:
            summaries = await context.client().list_past_orders()
        except (AuthRequired, RetailerError) as exc:
            error = str(exc)
    else:
        error = "Connect the No Frills session to load new orders."
    with session_scope() as session:
        imported = {o.id: o for o in session.scalars(select(PastOrder).order_by(PastOrder.placed_at.desc()))}
        rows = [ords.summary_dict(s, imported.get(s.order_id)) for s in summaries]
        known = {r["order_id"] for r in rows}
        rows += [{"order_id": o.id, "placed_at": o.placed_at.isoformat() if o.placed_at else None,
                  "total": float(o.total) if o.total is not None else None, "store": o.store, "fulfillment": o.fulfillment,
                  "imported": True} for o in imported.values() if o.id not in known]
    return {"orders": rows, "error": error}


@router.get("/orders/{order_id}")
async def order(order_id: str, refresh: bool = True):
    with session_scope() as session:
        o = session.get(PastOrder, order_id)
        if (o is None or refresh) and _connected():
            client = context.client()
            summaries = await client.list_past_orders()
            data = await client.get_past_order(order_id)
            o = ords.import_order(session, data, next((s for s in summaries if s.order_id == order_id), None), store_id=settings.store_id)
        if o is None:
            raise service.ShoppingError("Order not imported yet. Sign in to No Frills (Settings) to load it.")
        return ords.order_view(session, o)


# -- history ----------------------------------------------------------------------------------------

@router.get("/history/activity")
def activity(kind: str = "", q: str = "", days: int = 30, run_id: str = "", product_id: str = ""):
    with session_scope() as session:
        events = act.activity(session, days=min(days, 730), kind=kind or None, q=q or None, run_id=run_id or None,
                              product_id=product_id or None)
    for e in events:
        e["time"] = e["time"].isoformat() if e.get("time") else None
    return {"events": events, "kinds": act.KINDS}


@router.get("/history/feedback")
def feedback(action: str = "", requirement: str = "", unreviewed: bool = False):
    with session_scope() as session:
        rows = fbk.list_feedback(session, action=action or None, requirement=requirement or None, unreviewed_only=unreviewed)
        summary = fbk.feedback_summary(session)
        return {"records": [fbk.feedback_dict(r) | {"device_id": r.device_id} for r in rows],
                "summary": {k: v for k, v in summary.items() if k != "patterns"}}


class ReviewBody(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)
    resolution: str | None = Field(default=None, max_length=500)


class GeneralNoteBody(BaseModel):
    note: str = Field(min_length=1, max_length=2000)
    requirement: str | None = Field(default=None, max_length=128)


@router.post("/history/notes")
def add_note(body: GeneralNoteBody):
    with session_scope() as session:
        fb = fbk.add_note(session, note=body.note, requirement=body.requirement)
        return {"id": fb.id}


@router.get("/history/prices")
def price_changes(q: str = ""):
    with session_scope() as session:
        query = (select(Product, func.count(PriceObservation.id), func.max(PriceObservation.observed_at))
                 .join(PriceObservation, PriceObservation.product_id == Product.id).group_by(Product.id)
                 .order_by(func.max(PriceObservation.observed_at).desc()).limit(100))
        if q:
            like = f"%{q.lower()}%"
            query = query.where(func.lower(Product.name).like(like) | func.lower(Product.brand).like(like))
        rows = [{**views.product_brief(p), "observations": n, "last_seen": last.isoformat() if last else None,
                 "current_price": float(p.current_price) if p.current_price is not None else None}
                for p, n, last in session.execute(query).all()]
        return {"products": rows, "recent_changes": service.recent_price_changes(session, days=90, limit=20, store_id=settings.store_id)}

