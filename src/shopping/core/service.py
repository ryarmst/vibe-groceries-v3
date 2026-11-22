"""Shopping domain logic shared by the MCP server and the web UI."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shopping.models import (
    PREFERENCE_KINDS,
    REASON_CODES,
    RUN_APPLIED,
    RUN_APPROVED,
    RUN_BUILDING,
    RUN_CANCELLED,
    RUN_PARTIAL,
    RUN_REVIEW,
    SEL_APPLIED,
    SEL_APPROVED,
    SEL_FAILED,
    SEL_PENDING,
    Category,
    SEL_REJECTED,
    CartAction,
    ChoiceGroup,
    Preference,
    PriceObservation,
    Product,
    Selection,
    ShoppingRun,
    utcnow,
)
from shopping.auth.session import AuthRequired
from shopping.retailer.client import PCExpressClient, RetailerError
from shopping.retailer.normalize import NormalizedProduct, SubstitutionSpec, round2

log = logging.getLogger(__name__)
LOCAL_TZ = ZoneInfo("America/Toronto")
ACTIVE_RUN_STATES = (RUN_BUILDING, RUN_REVIEW, RUN_PARTIAL, RUN_APPROVED)


class ShoppingError(Exception):
    pass


def whole_quantity(quantity: float, sold_by: str | None, *, allow_zero: bool = False) -> float:
    """Packaged items (everything not sold by weight) take whole-number quantities."""
    q = float(quantity)
    if q < 0 or (q == 0 and not allow_zero):
        raise ShoppingError("Quantity must be above zero.")
    if sold_by != "weight" and abs(q - round(q)) > 1e-9:
        raise ShoppingError(f"Quantity {q:g} isn't a whole number; this item is sold by the package.")
    return q if sold_by == "weight" else float(round(q))


def _local_day(dt: datetime) -> str:
    return dt.astimezone(LOCAL_TZ).date().isoformat()


# =====================================================================================
# Products and price observations
# =====================================================================================

def upsert_product(session: Session, np: NormalizedProduct) -> Product:
    product = session.scalar(select(Product).where(Product.retailer_product_id == np.retailer_product_id))
    if product is None:
        product = Product(retailer_product_id=np.retailer_product_id, name=np.name)
        session.add(product)
    for attr in ("article_number", "uom", "brand", "name", "raw_size", "raw_package_sizing", "size_value", "size_unit",
                 "size_estimated", "comparison_family", "sold_by", "image_url", "link"):
        value = getattr(np, attr)
        if value is not None and value != "":
            setattr(product, attr, value)
    if np.gtin:
        product.gtin = np.gtin
    if np.categories:
        set_category(session, product, np.categories)
    product.last_seen_at = utcnow()
    session.flush()
    return product


def aisle_of(path: list[tuple[str, str]]) -> str | None:
    """The shopper's "aisle": second level under Food (Meat, Dairy & Eggs…), first level elsewhere."""
    if not path:
        return None
    if path[0][1] == "Food" and len(path) > 1:
        return path[1][1]
    return path[0][1]


def remember_categories(session: Session, path: list[tuple[str, str]]) -> None:
    """Store each level of an aisle path (root first) in the category tree."""
    parent = None
    names: list[str] = []
    for level, (code, name) in enumerate(path, start=1):
        names.append(name)
        cat = session.get(Category, code)
        if cat is None:
            cat = Category(code=code, name=name)
            session.add(cat)
        cat.name, cat.parent_code, cat.level, cat.path = name, parent, level, " > ".join(names)
        parent = code


CROSS_CUTTING_AISLES = {"Natural and Organic", "International Foods"}  # also list products that live in a real aisle


def set_category(session: Session, product: Product, path: list[tuple[str, str]], *, authoritative: bool = True) -> None:
    """Give a product its category. The product page's breadcrumb is authoritative; a deal scan (which sees a
    product under every aisle it's listed in) only fills in a missing or cross-cutting category."""
    if not path:
        return
    remember_categories(session, path)
    if not authoritative and product.category_code and product.aisle not in CROSS_CUTTING_AISLES:
        return
    if not authoritative and product.category_code and aisle_of(path) in CROSS_CUTTING_AISLES:
        return
    product.category_code = path[-1][0]
    product.category_path = " > ".join(n for _, n in path)
    product.aisle = aisle_of(path)


def _is_on_sale(obs: PriceObservation) -> bool:
    return bool(obs.deal_text or obs.pc_optimum_text or (obs.sale_price is not None and obs.regular_price is not None
                                                        and obs.sale_price < obs.regular_price))


def _set_current(product: Product, obs: PriceObservation) -> None:
    """Keep the product's current_* fields in step with its newest shelf observation."""
    if obs.source == "order":
        return  # a price paid in the past, not today's shelf price
    if product.current_observed_at and obs.observed_at and obs.observed_at < product.current_observed_at:
        return
    product.current_price, product.current_regular_price = obs.effective_price, obs.regular_price
    product.current_unit_price, product.current_unit_basis = obs.unit_price, obs.unit_price_basis
    product.current_deal_text = obs.deal_text or obs.pc_optimum_text
    product.current_deal_expiry = (obs.deal_expiry or "")[:10] or None
    product.current_on_sale = _is_on_sale(obs)
    product.current_availability = obs.availability
    product.current_observed_at = obs.observed_at or utcnow()


def record_observation(session: Session, np: NormalizedProduct, *, source: str, store_id: str | None,
                       run_id: str | None = None, dedupe: bool = True) -> PriceObservation:
    """Store a price observation. Bulk sources (search/deals) are de-duplicated to one row per
    product/store/day unless the price, size or deal changed."""
    product = upsert_product(session, np)
    if dedupe:
        last = session.scalar(
            select(PriceObservation)
            .where(PriceObservation.product_id == product.id, PriceObservation.store_id == store_id)
            .order_by(PriceObservation.observed_at.desc())
            .limit(1)
        )
        if (
            last is not None
            and _local_day(last.observed_at) == _local_day(utcnow())
            and last.effective_price == np.effective_price
            and last.normalized_quantity == np.size_value
            and last.deal_text == np.deal_text
        ):
            _set_current(product, last)
            if source != "order":
                product.current_observed_at = utcnow()  # same price, confirmed again now
                product.current_availability = np.availability
            return last
    obs = PriceObservation(
        product_id=product.id,
        shopping_run_id=run_id,
        store_id=store_id,
        source=source,
        regular_price=np.regular_price,
        sale_price=np.sale_price,
        effective_price=np.effective_price,
        member_price=np.member_price,
        raw_package_size=np.raw_size,
        raw_package_sizing=np.raw_package_sizing,
        normalized_quantity=np.size_value,
        normalized_unit=np.size_unit,
        size_estimated=np.size_estimated,
        unit_price=np.unit_price,
        unit_price_basis=np.unit_price_basis,
        retailer_unit_price=np.retailer_unit_price,
        deal_type=np.deal_type,
        deal_text=np.deal_text,
        deal_expiry=np.deal_expiry,
        pc_optimum_text=np.pc_optimum_text,
        promotion_text=np.promotion_text,
        availability=np.availability,
    )
    session.add(obs)
    session.flush()
    _set_current(product, obs)
    return obs


def price_stats(session: Session, product: Product, *, store_id: str | None = None) -> dict:
    """Current vs. historical prices. One value per day is used for averages so repeated
    searches on the same day don't skew them."""
    q = select(PriceObservation).where(PriceObservation.product_id == product.id)
    if store_id:
        q = q.where(PriceObservation.store_id == store_id)
    observations = list(session.scalars(q.order_by(PriceObservation.observed_at)))
    if not observations:
        return {"observations": 0}
    daily: dict[str, PriceObservation] = {}
    for obs in observations:
        daily[_local_day(obs.observed_at)] = obs  # last observation of each day
    days = sorted(daily)
    current = daily[days[-1]]
    previous = next((daily[d] for d in reversed(days[:-1]) if daily[d].effective_price != current.effective_price), None)
    now = utcnow()

    def window(n_days: int) -> list[PriceObservation]:
        return [o for o in daily.values() if o.observed_at >= now - timedelta(days=n_days) and o.effective_price is not None]

    def avg(obs_list: list[PriceObservation], attr: str) -> Decimal | None:
        values = [getattr(o, attr) for o in obs_list if getattr(o, attr) is not None]
        return sum(values) / len(values) if values else None

    prices = [o.effective_price for o in daily.values() if o.effective_price is not None]
    unit_prices = [o.unit_price for o in daily.values() if o.unit_price is not None]
    sizes = {o.normalized_quantity for o in observations if o.normalized_quantity is not None and not o.size_estimated}
    avg90 = avg(window(90), "effective_price")
    stats = {
        "observations": len(observations),
        "days_observed": len(days),
        "first_observed": days[0],
        "current_price": round2(current.effective_price),
        "current_unit_price": None if current.unit_price is None else round(float(current.unit_price), 4),
        "unit_price_basis": current.unit_price_basis,
        "previous_price": round2(previous.effective_price) if previous else None,
        "previous_price_date": _local_day(previous.observed_at) if previous else None,
        "avg_30d": round2(avg(window(30), "effective_price")),
        "avg_90d": round2(avg90),
        "min_price": round2(min(prices)) if prices else None,
        "max_price": round2(max(prices)) if prices else None,
        "min_unit_price": round(float(min(unit_prices)), 4) if unit_prices else None,
        "max_unit_price": round(float(max(unit_prices)), 4) if unit_prices else None,
        "package_size_changed": len(sizes) > 1,
    }
    if avg90 and current.effective_price is not None and len(days) >= 2:
        stats["vs_avg_90d_pct"] = round(float((current.effective_price - avg90) / avg90 * 100), 1)
        stats["is_lowest_observed"] = current.effective_price <= min(prices)
    return stats


def history_brief(session: Session, retailer_ids: list[str], store_id: str | None) -> dict[str, dict]:
    """Small per-product history summary for annotating search results (one query)."""
    if not retailer_ids:
        return {}
    rows = session.execute(
        select(
            Product.retailer_product_id,
            func.count(func.distinct(func.date(PriceObservation.observed_at))),
            func.min(PriceObservation.observed_at),
        )
        .join(PriceObservation, PriceObservation.product_id == Product.id)
        .where(Product.retailer_product_id.in_(retailer_ids), PriceObservation.store_id == store_id)
        .group_by(Product.retailer_product_id)
    ).all()
    out: dict[str, dict] = {}
    for rid, n_days, first in rows:
        if n_days < 2:
            continue  # nothing to compare against yet
        product = session.scalar(select(Product).where(Product.retailer_product_id == rid))
        s = price_stats(session, product, store_id=store_id)
        out[rid] = {k: s.get(k) for k in ("days_observed", "min_price", "avg_90d", "vs_avg_90d_pct", "is_lowest_observed", "previous_price", "package_size_changed")}
    return out


# =====================================================================================
# Preferences
# =====================================================================================

def active_preferences(session: Session) -> list[Preference]:
    return list(session.scalars(select(Preference).where(Preference.active.is_(True)).order_by(Preference.created_at)))


def preference_dict(p: Preference) -> dict:
    return {
        "id": p.id, "kind": p.kind, "description": PREFERENCE_KINDS.get(p.kind, p.kind), "scope": p.scope,
        "target": p.target, "target_label": p.target_label, "category": p.category, "weight": p.weight,
        "note": p.note, "created_at": p.created_at.isoformat(), "absolute": p.kind.startswith("blacklist"),
    }


def add_preference(session: Session, *, kind: str, scope: str | None = None, target: str | None = None,
                   target_label: str | None = None, note: str | None = None, category: str | None = None,
                   weight: float = 1.0, source_selection_id: str | None = None) -> Preference:
    if kind not in PREFERENCE_KINDS:
        raise ShoppingError(f"Unknown preference kind {kind!r}")
    # Avoid duplicates of the same active rule.
    existing = session.scalar(
        select(Preference).where(Preference.active.is_(True), Preference.kind == kind, Preference.scope.is_(scope) if scope is None else Preference.scope == scope,
                                 Preference.target.is_(target) if target is None else Preference.target == target)
    )
    if existing and kind != "preference_note":
        return existing
    pref = Preference(kind=kind, scope=scope, target=target, target_label=target_label, note=note, category=category,
                      weight=weight, source_selection_id=source_selection_id)
    session.add(pref)
    session.flush()
    return pref


def preference_flags(prefs: list[Preference], np_or_product) -> list[str]:
    """Which active preferences mention this product (by id or brand)."""
    rid = getattr(np_or_product, "retailer_product_id", None)
    brand = (getattr(np_or_product, "brand", None) or "").lower()
    flags = []
    for p in prefs:
        if p.kind in ("prefer_product", "blacklist_product", "prefer_for_type") and p.target == rid:
            flags.append(p.kind + (f" ({p.scope})" if p.scope else ""))
        elif p.kind in ("prefer_brand", "blacklist_brand") and brand and (p.target or "").lower() == brand:
            flags.append(p.kind + (f" ({p.scope})" if p.scope else ""))
    return flags


def blacklist_hit(prefs: list[Preference], np: NormalizedProduct) -> Preference | None:
    brand = (np.brand or "").lower()
    for p in prefs:
        if p.kind == "blacklist_product" and p.target == np.retailer_product_id:
            return p
        if p.kind == "blacklist_brand" and brand and (p.target or "").lower() == brand:
            return p
    return None


# =====================================================================================
# Shopping runs
# =====================================================================================

def get_run(session: Session, run_id: str) -> ShoppingRun:
    run = session.get(ShoppingRun, run_id)
    if run is None:
        raise ShoppingError(f"No shopping run {run_id!r}")
    return run


def create_run(session: Session, *, summary: str, store_id: str, notes: str | None = None) -> ShoppingRun:
    run = ShoppingRun(summary=summary or "Shopping run", store_id=store_id, notes=notes, status=RUN_BUILDING)
    session.add(run)
    session.flush()
    return run


def _require_editable(run: ShoppingRun) -> None:
    if run.status == RUN_CANCELLED:
        raise ShoppingError(f"Run {run.id} is cancelled; create a new run instead.")


def _clean_reasons(reasons: list[str] | None) -> list[str]:
    reasons = [r.strip().lower() for r in (reasons or []) if r and r.strip()]
    unknown = [r for r in reasons if r not in REASON_CODES]
    if unknown:
        raise ShoppingError(f"Unknown reason codes {unknown}; allowed: {sorted(REASON_CODES)}")
    return list(dict.fromkeys(reasons))


async def _stage_selection(session: Session, client: PCExpressClient, run: ShoppingRun, *, product_id: str, quantity: float,
                           reasons: list[str] | None, rationale: str | None, requirement: str | None,
                           group: ChoiceGroup | None, position: int, added_by: str = "agent",
                           substitution_allowed: bool = False, substitution_note: str | None = None,
                           substitute_product_id: str | None = None) -> Selection:
    if quantity <= 0:
        raise ShoppingError("Quantity must be positive.")
    reasons = _clean_reasons(reasons)
    np = await client.get_product(product_id)
    if not np.retailer_product_id:
        raise ShoppingError(f"Product {product_id!r} not found.")
    hit = blacklist_hit(active_preferences(session), np)
    if hit and "user_requested" not in reasons:
        raise ShoppingError(
            f"{np.brand or ''} {np.name} is blacklisted by preference #{hit.id} ({hit.kind}). "
            "Choose another product (or include reason 'user_requested' if the user explicitly asked for it)."
        )
    if (np.availability or "").upper() in ("OUT", "OUT_OF_STOCK"):
        # Search results don't always flag this (weighed meat especially); the product page does.
        raise ShoppingError(f"{np.brand or ''} {np.name} is out of stock at this store; choose another product.".strip())
    quantity = whole_quantity(quantity, np.sold_by)
    if np.max_quantity and quantity > np.max_quantity:
        raise ShoppingError(f"Quantity {quantity} exceeds the retailer maximum of {np.max_quantity}.")
    obs = record_observation(session, np, source="selection", store_id=run.store_id, run_id=run.id, dedupe=False)
    sel = Selection(
        product_id=obs.product_id, price_observation_id=obs.id, quantity=quantity, suggested_quantity=quantity,
        added_by=added_by, status=SEL_PENDING,
        substitution_allowed=bool(substitution_allowed or substitute_product_id),
        substitution_note=(substitution_note or "").strip()[:250] or None, substitute_product_id=substitute_product_id or None,
        requirement=requirement or (group.label if group else None), rationale=rationale, reason_codes=reasons, position=position,
    )
    run.selections.append(sel)  # attach via relationships so in-session collections stay current
    if group is not None:
        group.selections.append(sel)
    session.flush()
    return sel


def _next_position(run: ShoppingRun) -> int:
    positions = [s.position for s in run.selections] + [g.position for g in run.choice_groups]
    return (max(positions) + 1) if positions else 0


async def add_selection(session: Session, client: PCExpressClient, run_id: str, *, product_id: str, quantity: float = 1,
                        reasons: list[str] | None = None, rationale: str | None = None, requirement: str | None = None,
                        substitution_allowed: bool = False, substitution_note: str | None = None,
                        substitute_product_id: str | None = None) -> Selection:
    run = get_run(session, run_id)
    _require_editable(run)
    sel = await _stage_selection(session, client, run, product_id=product_id, quantity=quantity, reasons=reasons,
                                 rationale=rationale, requirement=requirement, group=None, position=_next_position(run),
                                 substitution_allowed=substitution_allowed, substitution_note=substitution_note,
                                 substitute_product_id=substitute_product_id)
    _after_agent_change(run)
    return sel


async def add_choice_group(session: Session, client: PCExpressClient, run_id: str, *, label: str, options: list[dict],
                           recommended_index: int = 0) -> ChoiceGroup:
    run = get_run(session, run_id)
    _require_editable(run)
    if len(options) < 1:
        raise ShoppingError("A choice group needs at least one option.")
    if not 0 <= recommended_index < len(options):
        raise ShoppingError("recommended_index is out of range.")
    group = ChoiceGroup(label=label, position=_next_position(run))
    run.choice_groups.append(group)
    session.flush()
    for i, opt in enumerate(options):
        reasons = list(opt.get("reasons") or [])
        if i != recommended_index and "alternative" not in reasons:
            reasons.append("alternative")
        sel = await _stage_selection(session, client, run, product_id=opt["product_id"], quantity=float(opt.get("quantity", 1)),
                                     reasons=reasons, rationale=opt.get("rationale"), requirement=label, group=group, position=i,
                                     substitution_allowed=bool(opt.get("substitution_allowed")),
                                     substitution_note=opt.get("substitution_note"), substitute_product_id=opt.get("substitute_product_id"))
        if i == recommended_index:
            group.recommended_selection_id = sel.id
            group.selected_selection_id = sel.id
    session.flush()
    _after_agent_change(run)
    return group


def update_selection(session: Session, selection_id: str, *, quantity: float | None = None, rationale: str | None = None,
                     reasons: list[str] | None = None, withdraw: bool = False, substitution_allowed: bool | None = None,
                     substitution_note: str | None = None, substitute_product_id: str | None = None) -> Selection:
    """Agent-side edits. Only pending selections can be changed; the agent cannot approve."""
    sel = session.get(Selection, selection_id)
    if sel is None:
        raise ShoppingError(f"No selection {selection_id!r}")
    _require_editable(sel.run)
    if sel.status != SEL_PENDING:
        raise ShoppingError(f"Selection {selection_id} is already {sel.status}; only pending selections can be edited.")
    if quantity is not None:
        if quantity <= 0:
            raise ShoppingError("Quantity must be positive (use withdraw=true to remove a suggestion).")
        sel.quantity = whole_quantity(quantity, sel.product.sold_by)
    if rationale is not None:
        sel.rationale = rationale
    if reasons is not None:
        sel.reason_codes = _clean_reasons(reasons)
    if substitution_allowed is not None:
        sel.substitution_allowed = substitution_allowed
    if substitution_note is not None:
        sel.substitution_note = substitution_note.strip()[:250] or None
    if substitute_product_id is not None:
        sel.substitute_product_id = substitute_product_id or None
        if substitute_product_id:
            sel.substitution_allowed = True
    if withdraw:
        sel.status = SEL_REJECTED
        sel.rejected_at = utcnow()
        sel.last_error = "withdrawn by agent"
    session.flush()
    recompute_run_status(sel.run)
    return sel


def submit_run_for_review(session: Session, run_id: str) -> ShoppingRun:
    run = get_run(session, run_id)
    _require_editable(run)
    if not run.selections:
        raise ShoppingError("Add at least one selection before submitting for review.")
    if run.status == RUN_BUILDING:
        run.status = RUN_REVIEW
    recompute_run_status(run)
    return run


def _after_agent_change(run: ShoppingRun) -> None:
    # New selections on an already-reviewed run put it back into review.
    if run.status in (RUN_APPROVED, RUN_PARTIAL):
        run.status = RUN_REVIEW


def chosen_selections(run: ShoppingRun) -> list[Selection]:
    """Standalone selections plus the currently selected option of each choice group."""
    out = []
    for sel in run.selections:
        if sel.choice_group_id is None:
            out.append(sel)
        elif sel.choice_group and sel.choice_group.selected_selection_id == sel.id:
            out.append(sel)
    return sorted(out, key=lambda s: (s.choice_group.position if s.choice_group else s.position))


def recompute_run_status(run: ShoppingRun) -> None:
    if run.status in (RUN_BUILDING, RUN_CANCELLED):
        return
    chosen = chosen_selections(run)
    statuses = {s.status for s in chosen}
    if not chosen or SEL_PENDING in statuses:
        run.status = RUN_REVIEW
    elif statuses <= {SEL_APPLIED, SEL_REJECTED} and SEL_APPLIED in statuses:
        run.status = RUN_APPLIED
    elif statuses <= {SEL_APPROVED, SEL_APPLIED}:
        run.status = RUN_APPROVED
    else:
        run.status = RUN_PARTIAL


def estimated_total(run: ShoppingRun) -> Decimal:
    total = Decimal(0)
    for sel in chosen_selections(run):
        if sel.status == SEL_REJECTED or sel.observation is None or sel.observation.effective_price is None:
            continue
        total += sel.observation.effective_price * Decimal(str(sel.quantity))
    return total


# -- user decisions (web UI) -----------------------------------------------------------

def _decide(sel: Selection, approve: bool) -> None:
    if sel.status not in (SEL_PENDING, SEL_APPROVED, SEL_REJECTED, SEL_FAILED):
        return  # applied selections are final
    now = utcnow()
    if approve:
        sel.status, sel.approved_at, sel.rejected_at = SEL_APPROVED, now, None
    else:
        sel.status, sel.rejected_at, sel.approved_at = SEL_REJECTED, now, None


def decide_selection(session: Session, selection_id: str, approve: bool) -> Selection:
    sel = session.get(Selection, selection_id)
    if sel is None:
        raise ShoppingError(f"No selection {selection_id!r}")
    _require_editable(sel.run)
    if sel.run.status == RUN_BUILDING:
        sel.run.status = RUN_REVIEW  # the user is reviewing, whether or not the agent submitted
    group = sel.choice_group
    if group is not None and approve:
        # Approving an option selects it; sibling options are not being bought.
        group.selected_selection_id = sel.id
        for sib in group.selections:
            if sib.id != sel.id and sib.status in (SEL_PENDING, SEL_APPROVED):
                _decide(sib, approve=False)
    elif group is not None and not approve:
        # Rejecting the chosen option rejects the requirement (all pending siblings).
        for sib in group.selections:
            if sib.status in (SEL_PENDING, SEL_APPROVED):
                _decide(sib, approve=False)
    _decide(sel, approve)
    session.flush()
    recompute_run_status(sel.run)
    return sel


def user_set_selection_quantity(session: Session, selection_id: str, quantity: float) -> Selection:
    sel = session.get(Selection, selection_id)
    if sel is None:
        raise ShoppingError(f"No selection {selection_id!r}")
    _require_editable(sel.run)
    if sel.status == SEL_APPLIED:
        raise ShoppingError("Already in the cart; change the quantity on the Cart page.")
    if quantity <= 0:
        raise ShoppingError("Quantity must be positive; reject the item instead.")
    sel.quantity = whole_quantity(quantity, sel.product.sold_by)
    session.flush()
    return sel


def choose_alternative(session: Session, group_id: str, selection_id: str) -> ChoiceGroup:
    group = session.get(ChoiceGroup, group_id)
    if group is None or selection_id not in {s.id for s in group.selections}:
        raise ShoppingError("That option does not belong to this group.")
    _require_editable(group.run)
    previous = session.get(Selection, group.selected_selection_id) if group.selected_selection_id else None
    group.selected_selection_id = selection_id
    # If the previous choice was already approved, carry the approval over to the new choice.
    if previous and previous.id != selection_id and previous.status == SEL_APPROVED:
        decide_selection(session, selection_id, approve=True)
    else:
        sel = session.get(Selection, selection_id)
        if sel.status == SEL_REJECTED:
            sel.status, sel.rejected_at = SEL_PENDING, None
    session.flush()
    recompute_run_status(group.run)
    return group


def approve_all_current(session: Session, run_id: str, snapshot_ids: list[str]) -> int:
    """Approve the chosen selections that were on screen when the user clicked. Selections the
    agent added after the page was rendered are not in the snapshot and stay pending."""
    run = get_run(session, run_id)
    _require_editable(run)
    snapshot = set(snapshot_ids)
    count = 0
    for sel in chosen_selections(run):
        if sel.id in snapshot and sel.status == SEL_PENDING:
            decide_selection(session, sel.id, approve=True)
            count += 1
    return count


def reject_remaining(session: Session, run_id: str, snapshot_ids: list[str]) -> int:
    run = get_run(session, run_id)
    _require_editable(run)
    snapshot = set(snapshot_ids)
    count = 0
    for sel in chosen_selections(run):
        if sel.id in snapshot and sel.status == SEL_PENDING:
            decide_selection(session, sel.id, approve=False)
            count += 1
    return count


def cancel_run(session: Session, run_id: str) -> ShoppingRun:
    run = get_run(session, run_id)
    if run.status == RUN_APPLIED:
        raise ShoppingError("Run already applied to the cart.")
    run.status = RUN_CANCELLED
    return run


# -- cart application -----------------------------------------------------------------

def product_label(p: Product | None) -> str | None:
    return None if p is None else " ".join(x for x in [p.brand, p.name, p.raw_size] if x)


async def apply_approved(session: Session, client: PCExpressClient, run_id: str, *, only_ids: list[str] | None = None,
                         source: str = "apply") -> dict:
    """Push approved, not-yet-applied selections into the retailer cart (all of them, or `only_ids`).

    Idempotent per product: the cart quantity is raised to at least the total approved quantity
    for that product in this run, never lowered and never double-added on retry."""
    run = get_run(session, run_id)
    _require_editable(run)
    to_apply = [s for s in chosen_selections(run) if s.status in (SEL_APPROVED, SEL_FAILED) and s.approved_at]
    if only_ids is not None:
        to_apply = [s for s in to_apply if s.id in set(only_ids)]
    if not to_apply:
        return {"applied": 0, "failed": 0, "results": [], "message": "Nothing approved to add."}
    cart = await client.get_cart()
    by_product: dict[str, list[Selection]] = defaultdict(list)
    for sel in to_apply:
        by_product[sel.product.retailer_product_id].append(sel)
    results = []
    for rid, sels in by_product.items():
        before = cart.quantity_of(rid)
        # Count everything approved/in cart for this product in the run, not just this batch.
        wanted = sum(s.quantity for s in chosen_selections(run)
                     if s.product.retailer_product_id == rid and s.status in (SEL_APPROVED, SEL_FAILED, SEL_APPLIED))
        target = max(before, wanted)
        action = CartAction(shopping_run_id=run.id, selection_id=sels[0].id, product_id=sels[0].product_id, retailer_product_id=rid,
                            product_label=product_label(sels[0].product), action="set_quantity", source=source,
                            quantity_before=before, quantity_after=target,
                            price_at_action=sels[0].observation.effective_price if sels[0].observation else None,
                            upstream_reference=cart.cart_id)
        try:
            spec = await substitution_spec(client, sels[0])
            line = next((l for l in cart.lines if l.product_id == rid), None)
            sub_differs = line is None or line.substitution_allowed != spec.allowed or bool(spec.comment) or spec.substitute is not None
            if target != before or sub_differs:
                cart = await client.set_cart_quantity(rid, target, substitution=spec)
            action.success = True
            for s in sels:
                s.status, s.applied_to_cart_at, s.last_error = SEL_APPLIED, utcnow(), None
            results.append({"product_id": rid, "name": sels[0].product.name, "quantity_before": before, "quantity_after": target, "ok": True})
        except Exception as exc:  # noqa: BLE001 - record and continue with the rest
            action.success, action.error = False, str(exc)[:500]
            for s in sels:
                s.status, s.last_error = SEL_FAILED, str(exc)[:500]
            results.append({"product_id": rid, "name": sels[0].product.name, "ok": False, "error": str(exc)[:300]})
            if isinstance(exc, (RetailerError, AuthRequired, ShoppingError)):
                log.warning("cart apply failed run=%s product=%s: %s", run.id, rid, exc)
            else:  # a bug, not a retailer refusal: keep the traceback
                log.exception("cart apply crashed run=%s product=%s", run.id, rid)
            if "session" in str(exc).lower() or "unavailable" in str(exc).lower():
                session.add(action)
                break  # auth problem or retailer outage: stop rather than failing every item
        session.add(action)
        recompute_run_status(run)
        session.commit()  # save each item as it lands; never hold the database across the next (slow) retailer call
    recompute_run_status(run)
    ok = sum(1 for r in results if r["ok"])
    errors = [r["error"] for r in results if not r["ok"]]
    return {"applied": ok, "failed": len(results) - ok, "results": results, "error": errors[0] if errors else None}


async def substitution_spec(client: PCExpressClient, sel: Selection) -> SubstitutionSpec:
    substitute = None
    if sel.substitute_product_id:
        try:
            substitute = await client.get_product(sel.substitute_product_id)
        except RetailerError:
            substitute = None  # fall back to "substitutions OK" with the note
    return SubstitutionSpec(allowed=bool(sel.substitution_allowed), comment=sel.substitution_note or "", substitute=substitute)


async def set_substitution(session: Session, client: PCExpressClient, *, selection_id: str | None = None, product_id: str | None = None,
                           allowed: bool, note: str | None = None, substitute_product_id: str | None = None,
                           in_cart: bool = False, source: str = "user") -> str:
    """User change of an item's substitution preference: on the run item, and in the cart if it's there.
    A change from what the agent proposed is recorded as feedback."""
    from shopping.models import Feedback  # local import: Feedback is defined after Selection

    sel = session.get(Selection, selection_id) if selection_id else None
    before = None
    if sel is not None:
        before = {"allowed": sel.substitution_allowed, "note": sel.substitution_note, "substitute": sel.substitute_product_id}
        sel.substitution_allowed = bool(allowed or substitute_product_id)
        sel.substitution_note = (note or "").strip()[:250] or None
        sel.substitute_product_id = substitute_product_id or None
        product_id = sel.product.retailer_product_id
        in_cart = in_cart or sel.status == SEL_APPLIED
    after = {"allowed": bool(allowed or substitute_product_id), "note": (note or "").strip() or None, "substitute": substitute_product_id or None}
    if in_cart and product_id:
        spec = SubstitutionSpec(allowed=after["allowed"], comment=after["note"] or "",
                                substitute=(await client.get_product(substitute_product_id)) if substitute_product_id else None)
        cart = await client.get_cart()
        line = next((l for l in cart.lines if l.product_id == product_id), None)
        product = _product_from_cart_line(session, line) if line else None
        action = CartAction(retailer_product_id=product_id, product_id=product.id if product else None, product_label=product_label(product),
                            action="substitution", source=source, quantity_before=line.quantity if line else None,
                            quantity_after=line.quantity if line else None, upstream_reference=cart.cart_id,
                            shopping_run_id=sel.shopping_run_id if sel else None, selection_id=sel.id if sel else None)
        try:
            await client.set_cart_substitution(product_id, spec)
            action.success = True
        except Exception as exc:  # noqa: BLE001
            action.error = str(exc)[:500]
            raise
        finally:
            session.add(action)
            session.flush()
    if before is not None and before != after:
        product = sel.product
        session.add(Feedback(action="substitution_changed", shopping_run_id=sel.shopping_run_id, selection_id=sel.id,
                             product_id=product.id, requirement=sel.requirement, tags=[], note=after["note"],
                             context=json.dumps({"from": before, "to": after})))
        session.flush()
    state = "allowed" if after["allowed"] else "off"
    return f"Substitutions {state}" + (f" — “{after['note']}”" if after["note"] else "") + (" (in the cart too)." if in_cart else ".")


def _product_from_cart_line(session: Session, line) -> Product:
    """Products added on nofrills.ca may never have been searched here; record what the cart tells us."""
    product = session.scalar(select(Product).where(Product.retailer_product_id == line.product_id))
    if product is None:
        product = Product(retailer_product_id=line.product_id, name=line.name or line.product_id, brand=line.brand)
        session.add(product)
        session.flush()
    return product


async def user_set_cart_quantity(session: Session, client: PCExpressClient, product_id: str, quantity: float, *,
                                 source: str = "cart_page", selection: Selection | None = None):
    """A user-initiated cart change, recorded in cart history. Returns the updated cart."""
    cart = await client.get_cart()
    line = next((l for l in cart.lines if l.product_id == product_id), None)
    quantity = whole_quantity(quantity, "weight" if line and line.selling_type == "SOLD_BY_WEIGHT" else "each", allow_zero=True)
    before = line.quantity if line else 0.0
    product = _product_from_cart_line(session, line) if line else session.scalar(select(Product).where(Product.retailer_product_id == product_id))
    action = CartAction(retailer_product_id=product_id, product_id=product.id if product else None, product_label=product_label(product),
                        action="remove" if quantity == 0 else "set_quantity", source=source,
                        quantity_before=before, quantity_after=quantity, upstream_reference=cart.cart_id,
                        price_at_action=Decimal(str(line.unit_price)) if line and line.unit_price is not None else None,
                        shopping_run_id=selection.shopping_run_id if selection else None, selection_id=selection.id if selection else None)
    session.commit()  # don't hold a write lock during the retailer call
    try:
        cart = await client.set_cart_quantity(product_id, quantity)
        action.success = True
        return cart
    except Exception as exc:  # noqa: BLE001
        action.error = str(exc)[:500]
        raise
    finally:
        session.add(action)
        session.flush()


# =====================================================================================
# Serialization helpers (MCP + templates)
# =====================================================================================

def selection_dict(session: Session, sel: Selection, *, with_history: bool = False) -> dict:
    obs = sel.observation
    product = sel.product
    d = {
        "selection_id": sel.id,
        "choice_group_id": sel.choice_group_id,
        "product_id": product.retailer_product_id,
        "name": product.name,
        "brand": product.brand,
        "package_size": product.raw_size,
        "quantity": sel.quantity,
        "status": sel.status,
        "requirement": sel.requirement,
        "reasons": sel.reason_codes,
        "rationale": sel.rationale,
        "observed_price": round2(obs.effective_price) if obs else None,
        "regular_price": round2(obs.regular_price) if obs else None,
        "unit_price": round(float(obs.unit_price), 4) if obs and obs.unit_price is not None else None,
        "unit_price_basis": obs.unit_price_basis if obs else None,
        "deal": obs.deal_text if obs else None,
        "substitution_allowed": sel.substitution_allowed,
        "substitution_note": sel.substitution_note,
        "substitute_product_id": sel.substitute_product_id,
        "last_error": sel.last_error,
    }
    if with_history:
        d["history"] = price_stats(session, product, store_id=obs.store_id if obs else None)
    return d


def run_dict(session: Session, run: ShoppingRun, *, with_history: bool = False) -> dict:
    groups = []
    for g in run.choice_groups:
        groups.append({
            "choice_group_id": g.id, "label": g.label, "recommended_selection_id": g.recommended_selection_id,
            "selected_selection_id": g.selected_selection_id,
            "options": [selection_dict(session, s, with_history=with_history) for s in g.selections],
        })
    standalone = [selection_dict(session, s, with_history=with_history) for s in run.selections if s.choice_group_id is None]
    counts: dict[str, int] = defaultdict(int)
    for s in chosen_selections(run):
        counts[s.status] += 1
    return {
        "run_id": run.id, "summary": run.summary, "status": run.status, "store_id": run.store_id,
        "created_at": run.created_at.isoformat(), "notes": run.notes,
        "estimated_total": round2(estimated_total(run)),
        "status_counts": dict(counts),
        "selections": standalone,
        "choice_groups": groups,
        "user_feedback": _run_feedback(session, run.id),
    }


def _run_feedback(session: Session, run_id: str) -> list[dict]:
    from shopping.core.feedback import feedback_dict  # local import: feedback builds on this module
    from shopping.models import Feedback

    rows = session.scalars(select(Feedback).where(Feedback.shopping_run_id == run_id).order_by(Feedback.created_at))
    return [feedback_dict(f) for f in rows]


def product_history(session: Session, retailer_product_id: str, *, limit: int = 50) -> dict:
    product = session.scalar(select(Product).where(Product.retailer_product_id == retailer_product_id))
    if product is None:
        raise ShoppingError(f"No history for product {retailer_product_id!r} yet.")
    observations = list(session.scalars(
        select(PriceObservation).where(PriceObservation.product_id == product.id).order_by(PriceObservation.observed_at.desc()).limit(limit)
    ))
    return {
        "product": {
            "product_id": product.retailer_product_id, "name": product.name, "brand": product.brand, "package_size": product.raw_size,
            "gtin": product.gtin, "comparison_family": product.comparison_family, "sold_by": product.sold_by,
            "first_seen": product.first_seen_at.isoformat(), "last_seen": product.last_seen_at.isoformat(),
        },
        "stats": price_stats(session, product),
        "observations": [
            {
                "observed_at": o.observed_at.isoformat(), "store_id": o.store_id, "source": o.source, "run_id": o.shopping_run_id,
                "package_size": o.raw_package_size, "price": round2(o.effective_price), "regular_price": round2(o.regular_price),
                "unit_price": None if o.unit_price is None else round(float(o.unit_price), 4), "unit_price_basis": o.unit_price_basis,
                "deal": o.deal_text, "pc_optimum": o.pc_optimum_text,
            }
            for o in observations
        ],
        "selections": selection_history(session, product_id=retailer_product_id)["selections"],
    }


def selection_history(session: Session, *, product_id: str | None = None, query: str | None = None, limit: int = 50) -> dict:
    q = select(Selection).join(Product, Selection.product_id == Product.id).order_by(Selection.created_at.desc())
    if product_id:
        q = q.where(Product.retailer_product_id == product_id)
    if query:
        like = f"%{query.lower()}%"
        q = q.where(func.lower(Product.name).like(like) | func.lower(Product.brand).like(like) | func.lower(Selection.requirement).like(like))
    rows = list(session.scalars(q.limit(limit)))
    outcome_counts: dict[str, int] = defaultdict(int)
    for s in rows:
        outcome_counts[s.status] += 1
    quantities = [s.quantity for s in rows if s.status in (SEL_APPROVED, SEL_APPLIED)]
    return {
        "count": len(rows),
        "outcomes": dict(outcome_counts),
        "typical_quantity": (sorted(quantities)[len(quantities) // 2] if quantities else None),
        "last_suggested": rows[0].created_at.isoformat() if rows else None,
        "selections": [
            {
                "date": s.created_at.isoformat(), "run_id": s.shopping_run_id, "product_id": s.product.retailer_product_id,
                "name": s.product.name, "brand": s.product.brand, "requirement": s.requirement, "quantity": s.quantity,
                "status": s.status, "reasons": s.reason_codes,
                "price": round2(s.observation.effective_price) if s.observation else None,
            }
            for s in rows
        ],
    }


def recent_price_changes(session: Session, *, days: int = 30, limit: int = 50, store_id: str | None = None) -> list[dict]:
    """Products whose effective price or package size changed within the window."""
    since = utcnow() - timedelta(days=days)
    q = select(PriceObservation).where(PriceObservation.observed_at >= since)
    if store_id:
        q = q.where(PriceObservation.store_id == store_id)
    by_product: dict[int, list[PriceObservation]] = defaultdict(list)
    for o in session.scalars(q.order_by(PriceObservation.observed_at)):
        by_product[o.product_id].append(o)
    out = []
    for pid, obs in by_product.items():
        first, last = obs[0], obs[-1]
        price_changed = first.effective_price != last.effective_price
        size_changed = first.normalized_quantity != last.normalized_quantity and not last.size_estimated
        if not (price_changed or size_changed):
            continue
        product = session.get(Product, pid)
        change = None
        if first.effective_price and last.effective_price is not None:
            change = round(float((last.effective_price - first.effective_price) / first.effective_price * 100), 1)
        out.append({
            "product_id": product.retailer_product_id, "name": product.name, "brand": product.brand,
            "from_price": round2(first.effective_price), "to_price": round2(last.effective_price), "change_pct": change,
            "from_size": first.raw_package_size, "to_size": last.raw_package_size, "size_changed": size_changed,
            "from_unit_price": None if first.unit_price is None else round(float(first.unit_price), 4),
            "to_unit_price": None if last.unit_price is None else round(float(last.unit_price), 4),
            "unit_price_basis": last.unit_price_basis, "since": _local_day(first.observed_at), "latest": _local_day(last.observed_at),
        })
    out.sort(key=lambda r: abs(r["change_pct"] or 0), reverse=True)
    return out[:limit]
