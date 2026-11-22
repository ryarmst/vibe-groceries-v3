"""JSON views for the app: a run as review cards, a product's modify context.

Per item: the product, its observed price and deal, our price history, past feedback on it, when the
requirement was last bought or removed.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from shopping.core import feedback as fbk
from shopping.core import service
from shopping.models import Feedback, PriceObservation, Product, Selection, ShoppingRun
from shopping.retailer.normalize import round2


def product_brief(p: Product) -> dict:
    return {"product_id": p.retailer_product_id, "name": p.name, "brand": p.brand, "size": p.raw_size,
            "image_url": p.image_url, "sold_by": p.sold_by, "aisle": p.aisle, "category": p.category_path,
            "link": f"https://www.nofrills.ca{p.link}" if p.link and p.link.startswith("/") else p.link}


def obs_brief(o: PriceObservation | None) -> dict | None:
    if o is None:
        return None
    return {"price": round2(o.effective_price), "regular_price": round2(o.regular_price),
            "unit_price": None if o.unit_price is None else round(float(o.unit_price), 4), "unit_price_basis": o.unit_price_basis,
            "deal": o.deal_text, "deal_ends": (o.deal_expiry or "")[:10] or None, "pc_optimum": o.pc_optimum_text,
            "observed_at": o.observed_at.isoformat() if o.observed_at else None}


def _stats_brief(st: dict) -> dict | None:
    if not st or st.get("observations", 0) == 0:
        return None
    keys = ("days_observed", "min_price", "max_price", "avg_90d", "previous_price", "previous_price_date", "vs_avg_90d_pct",
            "is_lowest_observed", "package_size_changed")
    return {k: st.get(k) for k in keys if st.get(k) is not None}


def selection_card(session: Session, sel: Selection, *, fb_hist: dict, req: dict | None, pending_ids: set[str]) -> dict:
    obs = sel.observation
    return {
        "selection_id": sel.id,
        "status": "adding" if sel.id in pending_ids else sel.status,
        "quantity": sel.quantity,
        "suggested_quantity": sel.suggested_quantity,
        "requirement": sel.requirement,
        "reasons": sel.reason_codes,
        "rationale": sel.rationale,
        "added_by": sel.added_by,
        "substitution_allowed": sel.substitution_allowed,
        "substitution_note": sel.substitution_note,
        "substitute_product_id": sel.substitute_product_id,
        "last_error": sel.last_error if sel.status == "failed" else None,
        "applied_to_cart_at": sel.applied_to_cart_at.isoformat() if sel.applied_to_cart_at else None,
        "product": product_brief(sel.product),
        "price": obs_brief(obs),
        "history": _stats_brief(service.price_stats(session, sel.product, store_id=obs.store_id if obs else None)),
        "past_feedback": fb_hist,
        "requirement_history": req,
    }


def run_view(session: Session, run: ShoppingRun, *, pending_ids: set[str] | None = None) -> dict:
    """The run as review cards: one per requirement (a single item, or a choice group with its options)."""
    pending_ids = pending_ids or set()
    fb_hist_all = fbk.feedback_by_product(session, [s.product.retailer_product_id for s in run.selections])
    run_feedback: dict[str, list[dict]] = {}
    for f in session.scalars(select(Feedback).where(Feedback.shopping_run_id == run.id).order_by(Feedback.created_at)):
        run_feedback.setdefault(f.selection_id, []).append(fbk.feedback_dict(f))
    reqs = {r["requirement"].lower(): r for r in fbk.requirement_history(session, sorted({s.requirement for s in run.selections if s.requirement}))}

    def card(sel: Selection) -> dict:
        hist = None if run_feedback.get(sel.id) else fb_hist_all.get(sel.product.retailer_product_id)  # earlier runs only
        req = reqs.get((sel.requirement or "").lower())
        if req and req.get("last_added") is None and req.get("last_removed") is None:
            req = None
        return selection_card(session, sel, fb_hist=hist, req=req, pending_ids=pending_ids)

    cards = []
    for sel in run.selections:
        if sel.choice_group_id is None:
            cards.append({"kind": "single", "id": sel.id, "label": sel.requirement or sel.product.name, "section": fbk.section_of(sel),
                          "main": card(sel), "options": [], "feedback": run_feedback.get(sel.id, []), "created": sel.created_at})
    for g in run.choice_groups:
        chosen = next((s for s in g.selections if s.id == g.selected_selection_id), g.selections[0])
        cards.append({"kind": "group", "id": g.id, "label": g.label, "section": fbk.section_of(chosen),
                      "recommended_selection_id": g.recommended_selection_id, "selected_selection_id": g.selected_selection_id,
                      "main": card(chosen), "options": [card(s) for s in g.selections],
                      "feedback": [f for s in g.selections for f in run_feedback.get(s.id, [])], "created": g.created_at})
    cards.sort(key=lambda c: c["created"])
    for c in cards:
        c.pop("created")
    counts: dict[str, int] = {}
    for s in service.chosen_selections(run):
        key = "adding" if s.id in pending_ids else s.status
        counts[key] = counts.get(key, 0) + 1
    return {
        "run_id": run.id, "summary": run.summary, "status": run.status, "notes": run.notes,
        "created_at": run.created_at.isoformat(), "estimated_total": round2(service.estimated_total(run)),
        "counts": counts, "sections": [{"key": k, "title": t} for k, t in fbk.SECTIONS],
        "pending_snapshot": [c["main"]["selection_id"] for c in cards if c["main"]["status"] == "pending"],
        "editable": run.status != "cancelled",
        "cards": cards,
    }


def runs_list(session: Session, *, limit: int = 30) -> list[dict]:
    out = []
    for r in session.scalars(select(ShoppingRun).order_by(ShoppingRun.created_at.desc()).limit(limit)):
        counts: dict[str, int] = {}
        for s in service.chosen_selections(r):
            counts[s.status] = counts.get(s.status, 0) + 1
        out.append({"run_id": r.id, "summary": r.summary, "status": r.status, "created_at": r.created_at.isoformat(),
                    "item_count": sum(counts.values()), "counts": counts, "estimated_total": round2(service.estimated_total(r))})
    return out


def subs_allowed(checkbox: bool, note: str, previous_note: str | None) -> bool:
    """Substitutes are OK when the box is ticked, or when the user typed (or changed) a description of an
    acceptable substitute. A note that was only pre-filled doesn't override unticking the box."""
    typed = (note or "").strip()
    return bool(checkbox) or bool(typed and typed != (previous_note or "").strip())
