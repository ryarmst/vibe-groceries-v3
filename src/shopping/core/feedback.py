"""Triage decisions with reasons, pattern detection over them, and per-requirement buying history.

Every remove / choose-alternative the user makes during review is stored as a `Feedback` row with
quick reason tags and free text. Those rows are reviewed later (web /feedback, MCP
shopping_get_feedback) to spot patterns worth turning into preferences, app features, or skill
changes. Requirement history ("when did we last buy milk?") supports regular purchases.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shopping.core import service
from shopping.core.service import ShoppingError
from shopping.models import (
    FEEDBACK_TAG_LABELS,
    FEEDBACK_TAGS,
    PREFERENCE_KINDS,
    SEL_APPLIED,
    SEL_PENDING,
    Feedback,
    Preference,
    Product,
    Selection,
    utcnow,
)

RULE_KINDS = ("prefer_product", "prefer_brand", "prefer_for_type", "blacklist_product", "blacklist_brand")

# Review page sections, in display order.
SECTIONS = [
    ("list", "From the grocery list"),
    ("regular", "Regular purchases"),
    ("deals", "Deals & extras"),
    ("other", "Other"),
]


def section_of(sel: Selection) -> str:
    reasons = set(sel.reason_codes or [])
    if reasons & {"grocery_list", "user_requested", "pantry_low"}:
        return "list"
    if "regular_staple" in reasons:
        return "regular"
    if "good_deal" in reasons:
        return "deals"
    return "other"


def _product_label(p: Product | None) -> str | None:
    if p is None:
        return None
    return " ".join(x for x in [p.brand, p.name, p.raw_size] if x)


def snapshot(sel: Selection) -> dict:
    obs = sel.observation
    return {
        "product_id": sel.product.retailer_product_id,
        "label": _product_label(sel.product),
        "brand": sel.product.brand,
        "size": sel.product.raw_size,
        "size_value": None if sel.product.size_value is None else float(sel.product.size_value),
        "family": sel.product.comparison_family,
        "price": None if not obs or obs.effective_price is None else float(obs.effective_price),
        "unit_price": None if not obs or obs.unit_price is None else float(obs.unit_price),
        "unit_price_basis": obs.unit_price_basis if obs else None,
        "deal": obs.deal_text if obs else None,
        "quantity": sel.quantity,
        "agent_reasons": sel.reason_codes,
        "agent_rationale": sel.rationale,
        "substitution": {"allowed": bool(sel.substitution_allowed), "note": sel.substitution_note,
                         "substitute_product_id": sel.substitute_product_id},
        "aisle": sel.product.aisle,
        "category": sel.product.category_path,
    }


def allowed_tags(action: str) -> set[str]:
    return set(FEEDBACK_TAGS.get(action, {})) | {"other"}


def clean_tags(action: str, tags: list[str] | None) -> list[str]:
    allowed = allowed_tags(action)
    tags = [t for t in dict.fromkeys(tags or []) if t]
    bad = [t for t in tags if t not in allowed]
    if bad:
        raise ShoppingError(f"Unknown reason tags for {action}: {bad}")
    return tags


def make_rule(session: Session, rule: str, sel: Selection | None, product: Product) -> Preference:
    if rule not in RULE_KINDS:
        raise ShoppingError(f"Unknown rule {rule!r}")
    if rule in ("prefer_brand", "blacklist_brand"):
        if not product.brand:
            raise ShoppingError("This product has no brand recorded, so a brand rule can't be made.")
        target, label = product.brand, product.brand
    else:
        target, label = product.retailer_product_id, _product_label(product)
    scope = None if rule.startswith("blacklist") else (sel.requirement if sel else product.name)
    return service.add_preference(session, kind=rule, scope=scope, target=target, target_label=label,
                                  source_selection_id=sel.id if sel else None)


def triage(session: Session, selection_id: str, *, decision: str, chosen_selection_id: str | None = None,
           tags: list[str] | None = None, note: str | None = None, rule: str | None = None,
           quantity: float | None = None) -> Feedback | None:
    """Apply one review decision and record why.

    decision: "keep" (approve), "remove" (reject the requirement), or "choose" (switch to another
    option in the choice group and approve it). `rule` optionally saves a preference in the same step.
    """
    sel = session.get(Selection, selection_id)
    if sel is None:
        raise ShoppingError(f"No selection {selection_id!r}")
    note = (note or "").strip() or None
    rule = rule or None
    before = snapshot(sel)
    fb: Feedback | None = None

    if decision == "choose":
        if not chosen_selection_id or chosen_selection_id == sel.id:
            decision = "keep"  # choosing the current option is just keeping it
        else:
            chosen = session.get(Selection, chosen_selection_id)
            if chosen is None or chosen.choice_group_id != sel.choice_group_id or sel.choice_group_id is None:
                raise ShoppingError("That option isn't part of this item's choices.")
            if quantity is not None and quantity != chosen.quantity:
                service.user_set_selection_quantity(session, chosen.id, quantity)
            service.choose_alternative(session, sel.choice_group_id, chosen.id)
            service.decide_selection(session, chosen.id, approve=True)
            fb = Feedback(action="chose_alternative", tags=clean_tags("chose_alternative", tags), note=note,
                          product_id=sel.product_id, chosen_product_id=chosen.product_id,
                          context=json.dumps({"from": before, "to": snapshot(chosen),
                                              "recommended_selection_id": sel.choice_group.recommended_selection_id}))
            if rule:
                # Prefer-rules point at the chosen product; blacklist-rules at the one passed over.
                target_sel = sel if rule.startswith("blacklist") else chosen
                fb.preference_id = make_rule(session, rule, target_sel, target_sel.product).id

    if decision == "keep":
        changed_qty = quantity is not None and quantity != sel.quantity
        if changed_qty:
            service.user_set_selection_quantity(session, sel.id, quantity)
        service.decide_selection(session, sel.id, approve=True)
        clean = clean_tags("approved", tags)
        if clean or note or rule or changed_qty:
            fb = Feedback(action="quantity_changed" if changed_qty and not (clean or note) else "approved", tags=clean, note=note,
                          product_id=sel.product_id,
                          context=json.dumps({"from": before, "quantity_to": quantity if changed_qty else None}))
            if rule:
                fb.preference_id = make_rule(session, rule, sel, sel.product).id

    elif decision == "remove":
        service.decide_selection(session, sel.id, approve=False)
        fb = Feedback(action="removed", tags=clean_tags("removed", tags), note=note, product_id=sel.product_id,
                      context=json.dumps({"from": before}))
        if rule:
            fb.preference_id = make_rule(session, rule, sel, sel.product).id

    elif decision != "choose":
        raise ShoppingError(f"Unknown decision {decision!r}")

    if fb is not None:
        fb.shopping_run_id = sel.shopping_run_id
        fb.selection_id = sel.id
        fb.choice_group_id = sel.choice_group_id
        fb.requirement = sel.requirement
        session.add(fb)
        session.flush()
    return fb


def add_note(session: Session, *, note: str, requirement: str | None = None, run_id: str | None = None,
             product_retailer_id: str | None = None, tags: list[str] | None = None) -> Feedback:
    """Free-standing note (e.g. 'buy bigger eggs when baking season starts')."""
    if not (note or "").strip():
        raise ShoppingError("Note is empty.")
    product = None
    if product_retailer_id:
        product = session.scalar(select(Product).where(Product.retailer_product_id == product_retailer_id))
    fb = Feedback(action="note", note=note.strip(), requirement=requirement or None, shopping_run_id=run_id,
                  product_id=product.id if product else None, tags=[t for t in (tags or []) if t in FEEDBACK_TAG_LABELS])
    session.add(fb)
    session.flush()
    return fb


def next_pending(run) -> Selection | None:
    order = {key: i for i, (key, _) in enumerate(SECTIONS)}
    pending = [s for s in service.chosen_selections(run) if s.status == SEL_PENDING]
    pending.sort(key=lambda s: (order[section_of(s)], s.created_at))
    return pending[0] if pending else None


# -------------------------------------------------------------------------------------
# Reading feedback
# -------------------------------------------------------------------------------------

def feedback_dict(fb: Feedback) -> dict:
    ctx = json.loads(fb.context) if fb.context else {}
    return {
        "id": fb.id,
        "date": fb.created_at.isoformat(),
        "run_id": fb.shopping_run_id,
        "requirement": fb.requirement,
        "action": fb.action,
        "tags": fb.tags,
        "tag_labels": [FEEDBACK_TAG_LABELS.get(t, t) for t in fb.tags],
        "note": fb.note,
        "product": _product_label(fb.product),
        "product_id": fb.product.retailer_product_id if fb.product else None,
        "chosen_product": _product_label(fb.chosen_product),
        "chosen_product_id": fb.chosen_product.retailer_product_id if fb.chosen_product else None,
        "rule_created": fb.preference_id,
        "agent_rationale": (ctx.get("from") or {}).get("agent_rationale"),
        "price": (ctx.get("from") or {}).get("price"),
        "chosen_price": (ctx.get("to") or {}).get("price"),
        "reviewed": fb.reviewed_at is not None,
        "resolution": fb.resolution,
    }


def list_feedback(session: Session, *, days: int | None = None, action: str | None = None, tag: str | None = None,
                  requirement: str | None = None, product_retailer_id: str | None = None,
                  unreviewed_only: bool = False, limit: int = 200) -> list[Feedback]:
    q = select(Feedback).order_by(Feedback.created_at.desc())
    if days:
        q = q.where(Feedback.created_at >= utcnow() - timedelta(days=days))
    if action:
        q = q.where(Feedback.action == action)
    if requirement:
        q = q.where(func.lower(Feedback.requirement) == requirement.lower())
    if product_retailer_id:
        q = q.join(Product, Feedback.product_id == Product.id).where(Product.retailer_product_id == product_retailer_id)
    if unreviewed_only:
        q = q.where(Feedback.reviewed_at.is_(None))
    rows = list(session.scalars(q.limit(limit * 3 if tag else limit)))
    if tag:
        rows = [r for r in rows if tag in (r.tags or [])][:limit]
    return rows


def _has_active(session: Session, kind: str, target: str | None, scope: str | None = None) -> bool:
    q = select(Preference.id).where(Preference.active.is_(True), Preference.kind == kind, Preference.target == target)
    if scope is not None:
        q = q.where(Preference.scope == scope)
    return session.scalar(q.limit(1)) is not None


def detect_patterns(session: Session, rows: list[Feedback]) -> list[dict]:
    """Heuristic suggestions from repeated feedback. Each can be turned into a rule with one click,
    or noted as something for the skill/app to handle."""
    out: list[dict] = []

    def add(kind, scope, target, label, ids, why, actionable=True):
        if actionable and kind in PREFERENCE_KINDS and _has_active(session, kind, target, scope if kind in ("prefer_for_type",) else None):
            return
        out.append({"kind": kind, "description": PREFERENCE_KINDS.get(kind, kind), "scope": scope, "target": target,
                    "target_label": label, "evidence": len(ids), "feedback_ids": ids, "why": why, "actionable": actionable})

    removed = [r for r in rows if r.action == "removed" and r.product]
    by_product: dict[int, list[Feedback]] = defaultdict(list)
    by_brand: dict[str, list[Feedback]] = defaultdict(list)
    for r in removed:
        if set(r.tags) & {"dislike_product", "quality", "wrong_item"}:
            by_product[r.product_id].append(r)
        if "dislike_brand" in r.tags and r.product.brand:
            by_brand[r.product.brand].append(r)
    for pid, items in by_product.items():
        if len(items) >= 2:
            p = items[0].product
            add("blacklist_product", None, p.retailer_product_id, _product_label(p), [i.id for i in items],
                f"Removed {len(items)}× as disliked / wrong / poor quality")
    for brand, items in by_brand.items():
        if len(items) >= 2:
            add("blacklist_brand", None, brand, brand, [i.id for i in items], f"Removed {len(items)}× for the brand")

    chose = [r for r in rows if r.action == "chose_alternative" and r.chosen_product]
    by_req_choice: dict[tuple[str, int], list[Feedback]] = defaultdict(list)
    by_chosen_brand: dict[str, list[Feedback]] = defaultdict(list)
    for r in chose:
        if r.requirement:
            by_req_choice[(r.requirement.lower(), r.chosen_product_id)].append(r)
        if "prefer_brand" in r.tags and r.chosen_product.brand:
            by_chosen_brand[r.chosen_product.brand].append(r)
    for (req, _), items in by_req_choice.items():
        if len(items) >= 2:
            p = items[0].chosen_product
            add("prefer_for_type", items[0].requirement, p.retailer_product_id, _product_label(p), [i.id for i in items],
                f"Picked this over the recommendation {len(items)}× for {items[0].requirement}")
    for brand, items in by_chosen_brand.items():
        if len(items) >= 2:
            add("prefer_brand", None, brand, brand, [i.id for i in items], f"Switched to {brand} {len(items)}× for the brand")

    price_driven = [r for r in chose if set(r.tags) & {"cheaper", "better_unit_price"}]
    if len(price_driven) >= 3 and len({r.requirement for r in price_driven}) >= 2:
        add("prefer_cheaper_equivalent", None, None, None, [r.id for r in price_driven],
            f"Chose the cheaper option {len(price_driven)}× across {len({r.requirement for r in price_driven})} items")

    larger = smaller = 0
    size_ids = []
    for r in chose:
        ctx = json.loads(r.context or "{}")
        a, b = (ctx.get("from") or {}), (ctx.get("to") or {})
        if a.get("family") == b.get("family") and a.get("size_value") and b.get("size_value") and a["size_value"] != b["size_value"]:
            size_ids.append(r.id)
            larger += b["size_value"] > a["size_value"]
            smaller += b["size_value"] < a["size_value"]
    if len(size_ids) >= 3:
        if larger / len(size_ids) >= 0.75:
            add("prefer_larger_packages", None, None, None, size_ids, f"Chose a larger package {larger} of {len(size_ids)} times")
        elif smaller / len(size_ids) >= 0.75:
            add("prefer_smaller_packages", None, None, None, size_ids, f"Chose a smaller package {smaller} of {len(size_ids)} times")

    enough: dict[str, list[Feedback]] = defaultdict(list)
    for r in removed:
        if "have_enough" in r.tags and r.requirement:
            enough[r.requirement.lower()].append(r)
    for _, items in enough.items():
        if len(items) >= 2:
            add("cadence", items[0].requirement, None, None, [i.id for i in items],
                f"“{items[0].requirement}” removed {len(items)}× because you had enough — suggested too often",
                actionable=False)

    subs: dict[tuple[str, bool], list[Feedback]] = defaultdict(list)
    for r in rows:
        if r.action == "substitution_changed" and r.requirement:
            to = (json.loads(r.context or "{}").get("to") or {})
            subs[(r.requirement.lower(), bool(to.get("allowed")))].append(r)
    for (_, allowed), items in subs.items():
        if len(items) >= 2:
            line = "Subs: OK" + (f" — {items[-1].note}" if items[-1].note else "") if allowed else "Subs: never"
            add("substitution", items[0].requirement, None, None, [i.id for i in items],
                f"You set substitutions {'on' if allowed else 'off'} for “{items[0].requirement}” {len(items)}× — "
                f"add “{line}” to its Shopping Preferences in Notion", actionable=False)

    out.sort(key=lambda p: -p["evidence"])
    return out


def feedback_summary(session: Session, *, days: int = 180) -> dict:
    rows = list_feedback(session, days=days, limit=1000)
    by_action = Counter(r.action for r in rows)
    by_tag = Counter(t for r in rows for t in (r.tags or []))
    by_requirement = Counter(r.requirement for r in rows if r.requirement)
    removed_brands = Counter(r.product.brand for r in rows if r.action == "removed" and r.product and r.product.brand)
    return {
        "days": days,
        "total": len(rows),
        "unreviewed": sum(1 for r in rows if r.reviewed_at is None),
        "by_action": dict(by_action),
        "by_tag": {FEEDBACK_TAG_LABELS.get(k, k): v for k, v in by_tag.most_common()},
        "by_tag_code": dict(by_tag.most_common()),
        "top_requirements": by_requirement.most_common(15),
        "removed_brands": removed_brands.most_common(10),
        "with_notes": sum(1 for r in rows if r.note),
        "patterns": detect_patterns(session, rows),
    }


def feedback_by_product(session: Session, retailer_ids: list[str]) -> dict[str, dict]:
    """Compact feedback history per product, for annotating search results."""
    if not retailer_ids:
        return {}
    rows = session.execute(
        select(Product.retailer_product_id, Feedback)
        .join(Feedback, Feedback.product_id == Product.id)
        .where(Product.retailer_product_id.in_(retailer_ids), Feedback.action.in_(["removed", "chose_alternative"]))
        .order_by(Feedback.created_at)
    ).all()
    out: dict[str, dict] = {}
    for rid, fb in rows:
        d = out.setdefault(rid, {"removed": 0, "passed_over": 0, "tags": Counter(), "last_note": None})
        d["removed" if fb.action == "removed" else "passed_over"] += 1
        d["tags"].update(fb.tags or [])
        if fb.note:
            d["last_note"] = fb.note
    for d in out.values():
        d["tags"] = dict(d["tags"])
    return out


# -------------------------------------------------------------------------------------
# Requirement history (regular purchases)
# -------------------------------------------------------------------------------------

def _name_matches(requirement_key: str, line_name: str) -> bool:
    """'broccoli' matches 'Broccoli Crown'; 'avocados' matches 'Avocado Bag'; whole words only."""
    words = re.findall(r"[a-z]+", line_name.lower())
    text = " " + " ".join(words) + " "
    key_words = re.findall(r"[a-z]+", requirement_key)
    if not key_words:
        return False
    variants = {" ".join(key_words)}
    last = key_words[-1]
    for cut in ("es", "s"):
        if last.endswith(cut) and len(last) > len(cut) + 2:
            variants.add(" ".join(key_words[:-1] + [last[: -len(cut)]]))
    return any(f" {v} " in text or f" {v}s " in text for v in variants)


def requirement_history(session: Session, names: list[str], *, default_cadence_days: int = 7) -> list[dict]:
    """Buying history per requirement name (e.g. pantry item names flagged as regular purchases).

    Uses imported orders (what was actually bought) when available, and cart additions from runs
    otherwise."""
    from shopping.core.orders import purchases_for_products  # local import: orders builds on service
    from shopping.models import PastOrder, PastOrderLine

    out = []
    now = utcnow()
    # Product -> requirement it was suggested for, so a product staged for "Almond Milk" never counts as "Milk".
    claimed = {pid: (req or "").lower() for pid, req in session.execute(select(Selection.product_id, Selection.requirement))}
    all_lines = session.execute(
        select(PastOrder.placed_at, PastOrderLine).join(PastOrderLine, PastOrderLine.order_id == PastOrder.id)
        .where(PastOrderLine.fulfilled.is_(True), PastOrderLine.is_fee.is_(False), PastOrder.placed_at.is_not(None))
    ).all()
    for name in names:
        key = name.strip().lower()
        if not key:
            continue
        sels = list(session.scalars(
            select(Selection).where(func.lower(Selection.requirement) == key).order_by(Selection.created_at)
        ))
        added = [s for s in sels if s.status == SEL_APPLIED and s.applied_to_cart_at]
        own_products = {s.product_id for s in sels}
        purchases = purchases_for_products(session, own_products)
        seen_lines = {l.id for _, l in purchases}
        purchases += [(t, l) for t, l in all_lines if l.id not in seen_lines
                      and claimed.get(l.product_id, key) == key and _name_matches(key, l.name)]
        purchases.sort(key=lambda tl: tl[0])
        buy_dates = sorted({t.date() for t, _ in purchases})
        add_dates = sorted({s.applied_to_cart_at.date() for s in added})
        dates = buy_dates if len(buy_dates) >= 2 else sorted(set(buy_dates) | set(add_dates))
        intervals = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0]
        typical = round(sum(intervals) / len(intervals)) if intervals else None
        last = added[-1] if added else None
        last_buy = purchases[-1] if purchases else None
        last_time = max([t for t in [last.applied_to_cart_at if last else None, last_buy[0] if last_buy else None] if t], default=None)
        days_since = (now - last_time).days if last_time else None
        skips = [f for f in session.scalars(
            select(Feedback).where(func.lower(Feedback.requirement) == key, Feedback.action == "removed").order_by(Feedback.created_at)
        )]
        last_skip = skips[-1] if skips else None
        skipped_enough_recently = bool(last_skip and "have_enough" in (last_skip.tags or []) and (now - last_skip.created_at).days < (typical or default_cadence_days))
        pref = session.scalar(select(Preference).where(Preference.active.is_(True), Preference.kind == "prefer_for_type",
                                                      func.lower(Preference.scope) == key).order_by(Preference.created_at.desc()))
        cadence = typical or default_cadence_days
        verb = "bought" if last_buy and last_buy[0] == last_time else "added to cart"
        if skipped_enough_recently:
            status, why = "not_due", f"skipped {last_skip.created_at.date()} — had enough"
        elif last_time is None:
            status, why = "unknown", "no purchases or cart additions recorded yet"
        elif days_since >= cadence * 0.8:
            status, why = "due", f"last {verb} {days_since} days ago (typical {cadence})"
        else:
            status, why = "not_due", f"last {verb} {days_since} days ago (typical {cadence})"
        out.append({
            "requirement": name,
            "status": status,
            "why": why,
            "times_suggested": len(sels),
            "times_added_to_cart": len(added),
            "times_purchased": len(buy_dates),
            "times_removed": len(skips),
            "last_added": None if not last else {
                "date": last.applied_to_cart_at.date().isoformat(), "days_ago": (now - last.applied_to_cart_at).days,
                "product_id": last.product.retailer_product_id, "product": _product_label(last.product), "quantity": last.quantity,
                "price": None if not last.observation or last.observation.effective_price is None else float(last.observation.effective_price),
            },
            "last_purchased": None if not last_buy else {
                "date": last_buy[0].date().isoformat(), "days_ago": (now - last_buy[0]).days, "order_id": last_buy[1].order_id,
                "product_id": last_buy[1].retailer_product_id, "product": " ".join(x for x in [last_buy[1].brand, last_buy[1].name, last_buy[1].size] if x),
                "quantity": f"{last_buy[1].weight_kg:g} kg" if last_buy[1].weight_kg else last_buy[1].quantity,
                "price": None if last_buy[1].total is None else float(last_buy[1].total),
            },
            "typical_interval_days": typical,
            "typical_quantity": sorted(s.quantity for s in added)[len(added) // 2] if added else None,
            "last_removed": None if not last_skip else {
                "date": last_skip.created_at.date().isoformat(), "tags": last_skip.tags, "note": last_skip.note,
            },
            "preferred_product": None if not pref else {"product_id": pref.target, "product": pref.target_label},
            "last_substitution": next(({"allowed": s.substitution_allowed, "note": s.substitution_note}
                                       for s in reversed(sels) if s.status in ("approved", "applied")), None),
        })
    return out


def mark_reviewed(session: Session, feedback_ids: list[int], resolution: str | None) -> int:
    n = 0
    for fid in feedback_ids:
        fb = session.get(Feedback, fid)
        if fb and fb.reviewed_at is None:
            fb.reviewed_at, fb.resolution = utcnow(), (resolution or "").strip() or None
            n += 1
    return n

