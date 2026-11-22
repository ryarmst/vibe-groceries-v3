"""One chronological feed of everything that happened: suggestions, approvals, removals, swaps,
cart changes, notes and rules. Built from the existing tables, newest first."""

from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from shopping.core.feedback import feedback_dict
from shopping.core.service import product_label
from shopping.models import PREFERENCE_KINDS, CartAction, Feedback, PastOrder, Preference, Product, Selection, TopDeal, utcnow

KINDS = {
    "suggested": "Suggested",
    "approved": "Approved",
    "removed": "Removed",
    "swapped": "Swapped",
    "cart": "Cart change",
    "note": "Note",
    "rule": "Rule saved",
    "purchased": "Bought",
    "deal": "Top deal",
}


def activity(session: Session, *, days: int = 30, kind: str | None = None, q: str | None = None,
             product_id: str | None = None, run_id: str | None = None, limit: int = 300) -> list[dict]:
    since = utcnow() - timedelta(days=days)
    events: list[dict] = []

    sel_q = select(Selection).where(Selection.created_at >= since)
    if run_id:
        sel_q = sel_q.where(Selection.shopping_run_id == run_id)
    removal_reasons: dict[str, Feedback] = {}
    fb_q = select(Feedback).where(Feedback.created_at >= since)
    if run_id:
        fb_q = fb_q.where(Feedback.shopping_run_id == run_id)
    feedback_rows = list(session.scalars(fb_q))
    for f in feedback_rows:
        if f.action == "removed" and f.selection_id:
            removal_reasons[f.selection_id] = f

    for s in session.scalars(sel_q):
        base = {"product": product_label(s.product), "product_id": s.product.retailer_product_id, "run_id": s.shopping_run_id,
                "run": s.run.summary, "requirement": s.requirement}
        price = s.observation.effective_price if s.observation else None
        # Alternatives offered alongside a recommendation are part of that suggestion, not separate ones.
        is_alternative = s.choice_group is not None and s.choice_group.recommended_selection_id != s.id and s.added_by != "user"
        if not is_alternative:
            n_alt = len(s.choice_group.selections) - 1 if s.choice_group is not None else 0
            events.append({**base, "time": s.created_at, "kind": "suggested",
                           "title": "You added" if s.added_by == "user" else "Agent suggested",
                           "detail": f"×{s.suggested_quantity or s.quantity:g}" + (f" at ${price:.2f}" if price is not None else "")
                                     + (f" · with {n_alt} alternative{'s' if n_alt != 1 else ''}" if n_alt else ""),
                           "reasons": s.reason_codes, "note": s.rationale})
        if s.approved_at:
            qty = f"×{s.quantity:g}" + (f" (suggested ×{s.suggested_quantity:g})" if s.suggested_quantity and s.suggested_quantity != s.quantity else "")
            events.append({**base, "time": s.approved_at, "kind": "approved", "title": "Approved", "detail": qty})
        # Unchosen options of a choice group are "not picked", not removals; skip them unless the user gave a reason.
        not_picked = s.choice_group is not None and s.choice_group.selected_selection_id != s.id and s.id not in removal_reasons
        if s.rejected_at and not not_picked:
            f = removal_reasons.get(s.id)
            events.append({**base, "time": s.rejected_at, "kind": "removed", "title": "Removed from run",
                           "detail": ", ".join(feedback_dict(f)["tag_labels"]) if f else "",
                           "note": f.note if f else (s.last_error if s.last_error == "withdrawn by agent" else None)})

    for f in feedback_rows:
        d = feedback_dict(f)
        base = {"time": f.created_at, "run_id": f.shopping_run_id, "requirement": f.requirement, "product": d["product"], "product_id": d["product_id"]}
        if f.action == "chose_alternative":
            events.append({**base, "kind": "swapped", "title": "Swapped", "detail": f"→ {d['chosen_product']}",
                           "chosen_product_id": d["chosen_product_id"], "reasons_text": ", ".join(d["tag_labels"]), "note": f.note})
        elif f.action == "substitution_changed":
            ctx = json.loads(f.context or "{}")
            to = ctx.get("to") or {}
            events.append({**base, "kind": "note", "title": "Substitution changed",
                           "detail": ("subs OK" if to.get("allowed") else "subs off") + (" · specific substitute" if to.get("substitute") else ""),
                           "note": f.note})
        elif f.action == "deal_dismissed":
            events.append({**base, "kind": "deal", "title": "Deal dismissed", "detail": ", ".join(d["tag_labels"]), "note": f.note})
        elif f.action in ("note", "approved", "quantity_changed") and (f.note or f.tags):
            events.append({**base, "kind": "note", "title": {"note": "Note", "approved": "Kept — note", "quantity_changed": "Quantity changed — note"}[f.action],
                           "detail": ", ".join(d["tag_labels"]), "note": f.note})

    ca_q = select(CartAction).where(CartAction.timestamp >= since)
    if run_id:
        ca_q = ca_q.where(CartAction.shopping_run_id == run_id)
    for a in session.scalars(ca_q):
        before, after = a.quantity_before or 0, a.quantity_after or 0
        verb = ("Substitution set" if a.action == "substitution" else
                "Added to cart" if before == 0 and after > 0 else "Removed from cart" if after == 0 else "Cart quantity changed")
        label = a.product_label or (product_label(session.get(Product, a.product_id)) if a.product_id else None) or a.retailer_product_id
        events.append({"time": a.timestamp, "kind": "cart", "title": verb if a.success else f"{verb} — failed", "ok": a.success,
                       "product": label, "product_id": a.retailer_product_id, "run_id": a.shopping_run_id,
                       "detail": f"{before:g} → {after:g}" + (f" at ${a.price_at_action:.2f}" if a.price_at_action is not None else "")
                                 + (f" · via {a.source.replace('_', ' ')}" if a.source else ""),
                       "note": a.error})

    for p in session.scalars(select(Preference).where(Preference.created_at >= since)):
        if run_id:
            continue
        events.append({"time": p.created_at, "kind": "rule", "title": "Rule saved", "product": p.target_label or p.target,
                       "product_id": p.target if p.target and "_" in p.target else None,
                       "detail": PREFERENCE_KINDS.get(p.kind, p.kind) + (f" (for {p.scope})" if p.scope else "") + ("" if p.active else " · inactive"),
                       "note": p.note})

    if not run_id:
        for deal in session.scalars(select(TopDeal).where(TopDeal.created_at >= since)):
            price = deal.observation.effective_price if deal.observation else None
            events.append({"time": deal.created_at, "kind": "deal", "title": "Deal flagged" + (" by you" if deal.added_by == "user" else ""),
                           "product": product_label(deal.product), "product_id": deal.product.retailer_product_id,
                           "requirement": deal.relevant_to,
                           "detail": deal.headline + (f" · ${price:.2f}" if price is not None else ""), "note": deal.why})
        for order in session.scalars(select(PastOrder).where(PastOrder.placed_at >= since)):
            lines = [l for l in order.lines if not l.is_fee and l.fulfilled]
            if product_id:
                for l in lines:
                    if l.retailer_product_id == product_id:
                        qty = f"{l.weight_kg:g} kg" if l.weight_kg else f"×{l.quantity:g}" if l.quantity else ""
                        events.append({"time": order.placed_at, "kind": "purchased", "title": "Bought", "product": l.name,
                                       "product_id": l.retailer_product_id, "order_id": order.id,
                                       "detail": f"{qty}" + (f" for ${l.total:.2f}" if l.total is not None else "") + f" · order {order.id}",
                                       "run_id": l.selection.shopping_run_id if l.selection else None})
            else:
                from_runs = sum(1 for l in lines if l.selection_id)
                events.append({"time": order.placed_at, "kind": "purchased", "title": "Order placed", "product": order.store,
                               "order_id": order.id,
                               "detail": (f"${order.total:.2f} · " if order.total is not None else "") + f"{len(lines)} items · {from_runs} from runs"})

    if kind:
        events = [e for e in events if e["kind"] == kind]
    if product_id:
        events = [e for e in events if product_id in (e.get("product_id"), e.get("chosen_product_id"))]
    if q:
        needle = q.lower()
        events = [e for e in events if needle in " ".join(str(e.get(k) or "") for k in ("product", "requirement", "note", "detail")).lower()]
    events.sort(key=lambda e: e["time"], reverse=True)
    return events[:limit]
