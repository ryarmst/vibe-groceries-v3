"""Modify one item after the fact — from the cart or from a run: swap it for another product, change
its quantity, remove it, or just leave a note. Every change records cart history and (when there is
a reason or note) feedback, and can save a rule at the same time."""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from shopping.core import feedback as fbk
from shopping.core import service
from shopping.core.service import ShoppingError
from shopping.models import (
    SEL_APPLIED,
    SEL_APPROVED,
    SEL_REJECTED,
    Feedback,
    Product,
    Selection,
    ShoppingRun,
    utcnow,
)
from shopping.retailer.client import PCExpressClient

ACTIONS = ("swap", "quantity", "remove", "note", "restore")


def linked_selection(session: Session, product_retailer_id: str) -> Selection | None:
    """The most recent run selection that put this product in the cart (if any)."""
    return session.scalar(
        select(Selection)
        .join(Product, Selection.product_id == Product.id)
        .join(ShoppingRun, Selection.shopping_run_id == ShoppingRun.id)
        .where(Product.retailer_product_id == product_retailer_id, Selection.status == SEL_APPLIED, ShoppingRun.status != "cancelled")
        .order_by(Selection.applied_to_cart_at.desc())
        .limit(1)
    )


def _feedback(session: Session, *, action: str, sel: Selection | None, product: Product | None, chosen: Product | None,
              tags: list[str], note: str | None, rule: str | None, context: dict) -> Feedback | None:
    tag_action = {"swap": "chose_alternative", "remove": "removed"}.get(action, "approved")
    tags = fbk.clean_tags(tag_action, [t for t in tags if t in fbk.allowed_tags(tag_action)])
    if action in ("quantity", "note", "restore") and not (tags or note or rule):
        return None
    fb = Feedback(
        action={"swap": "chose_alternative", "remove": "removed", "quantity": "quantity_changed", "note": "note",
                "restore": "approved"}[action],
        tags=tags, note=note, product_id=product.id if product else None, chosen_product_id=chosen.id if chosen else None,
        shopping_run_id=sel.shopping_run_id if sel else None, selection_id=sel.id if sel else None,
        choice_group_id=sel.choice_group_id if sel else None,
        requirement=(sel.requirement if sel else None) or (product.name if product else None),
        context=json.dumps(context, default=str),
    )
    if rule:
        target = chosen if (chosen is not None and not rule.startswith("blacklist")) else product
        if target is not None:
            fb.preference_id = fbk.make_rule(session, rule, sel, target).id
    session.add(fb)
    session.flush()
    return fb


async def modify(session: Session, client: PCExpressClient, *, action: str, product_id: str, selection_id: str | None = None,
                 new_product_id: str | None = None, quantity: float | None = None, tags: list[str] | None = None,
                 note: str | None = None, rule: str | None = None, in_cart: bool = True) -> str:
    """Apply one modification. Returns a short human-readable result.

    `in_cart`: whether the item is (or should be) in the retailer cart. Cart changes need a connected session;
    without one, run-side changes still happen and AuthRequired is raised for the cart part."""
    if action not in ACTIONS:
        raise ShoppingError(f"Unknown action {action!r}")
    tags, note, rule = tags or [], (note or "").strip() or None, rule or None
    sel = session.get(Selection, selection_id) if selection_id else None
    if sel is None and in_cart:
        sel = linked_selection(session, product_id)
    product = sel.product if sel else session.scalar(select(Product).where(Product.retailer_product_id == product_id))
    before = fbk.snapshot(sel) if sel else {"product_id": product_id, "label": service.product_label(product)}
    in_cart = in_cart or (sel is not None and sel.status == SEL_APPLIED)

    if action == "note":
        _feedback(session, action="note", sel=sel, product=product, chosen=None, tags=tags, note=note, rule=rule, context={"from": before})
        return "Note saved."

    if action == "quantity":
        if quantity is None or quantity <= 0:
            raise ShoppingError("Enter a quantity above zero (or use Remove).")
        quantity = service.whole_quantity(quantity, product.sold_by if product else None)
        if sel:
            sel.quantity = quantity
        if in_cart:
            await service.user_set_cart_quantity(session, client, product_id, quantity, source="modify", selection=sel)
        _feedback(session, action="quantity", sel=sel, product=product, chosen=None, tags=tags, note=note, rule=rule,
                  context={"from": before, "quantity_to": quantity})
        return f"Quantity set to {quantity:g}."

    if action == "remove":
        if sel:
            sel.status, sel.rejected_at, sel.approved_at = SEL_REJECTED, utcnow(), None
            service.recompute_run_status(sel.run)
        _feedback(session, action="remove", sel=sel, product=product, chosen=None, tags=tags, note=note, rule=rule, context={"from": before})
        if in_cart:
            await service.user_set_cart_quantity(session, client, product_id, 0, source="modify", selection=sel)
        return "Removed" + (" from the cart." if in_cart else " from the run.")

    if action == "restore":
        if sel is None:
            raise ShoppingError("Nothing to restore.")
        sel.status, sel.rejected_at, sel.approved_at = SEL_APPROVED, None, utcnow()
        if sel.choice_group:
            sel.choice_group.selected_selection_id = sel.id
        service.recompute_run_status(sel.run)
        _feedback(session, action="restore", sel=sel, product=product, chosen=None, tags=tags, note=note, rule=rule, context={"from": before})
        await service.apply_approved(session, client, sel.shopping_run_id, only_ids=[sel.id], source="modify")
        return "Added back."

    # swap
    if not new_product_id or new_product_id == product_id:
        raise ShoppingError("Pick a different product to swap to.")
    qty = quantity if quantity and quantity > 0 else (sel.quantity if sel else 1)
    new_sel = None
    if sel is not None:
        run = sel.run
        group = sel.choice_group
        existing = next((s for s in (group.selections if group else []) if s.product.retailer_product_id == new_product_id), None)
        if existing is not None:
            new_sel = existing
            new_sel.quantity = qty
        else:
            new_sel = await service._stage_selection(  # noqa: SLF001 - same package
                session, client, run, product_id=new_product_id, quantity=qty, reasons=["user_requested"],
                rationale=f"You swapped this in for {service.product_label(sel.product)}.", requirement=sel.requirement,
                group=group, position=(max(s.position for s in group.selections) + 1) if group else sel.position, added_by="user")
        if group is not None:
            group.selected_selection_id = new_sel.id
        sel.status, sel.rejected_at, sel.approved_at = SEL_REJECTED, utcnow(), None
        new_sel.status, new_sel.approved_at, new_sel.rejected_at = SEL_APPROVED, utcnow(), None
        service.recompute_run_status(run)
        chosen = new_sel.product
        after = fbk.snapshot(new_sel)
    else:
        np = await client.get_product(new_product_id)
        service.record_observation(session, np, source="product", store_id=client.store_id)
        chosen = service.upsert_product(session, np)
        after = {"product_id": new_product_id, "label": service.product_label(chosen),
                 "price": None if np.effective_price is None else float(np.effective_price),
                 "unit_price": None if np.unit_price is None else float(np.unit_price), "unit_price_basis": np.unit_price_basis}
    _feedback(session, action="swap", sel=sel, product=product, chosen=chosen, tags=tags, note=note, rule=rule,
              context={"from": before, "to": after})
    if not in_cart:
        return f"Swapped to {service.product_label(chosen)}."
    # Cart: take the old product out, put the new one in.
    await service.user_set_cart_quantity(session, client, product_id, 0, source="modify", selection=sel)
    if new_sel is not None:
        result = await service.apply_approved(session, client, new_sel.shopping_run_id, only_ids=[new_sel.id], source="modify")
        if result.get("error"):
            raise ShoppingError(f"Removed the old item, but adding the new one failed: {result['error']}")
    else:
        await service.user_set_cart_quantity(session, client, new_product_id, qty, source="modify")
    return f"Swapped to {service.product_label(chosen)} in the cart."

