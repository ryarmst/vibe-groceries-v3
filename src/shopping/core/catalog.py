"""The product catalogue: everything we've seen at the store, organised by No Frills' aisle tree, with each
product's current price and deal. A background deal scan keeps "what's on sale now" fresh, and the
context functions here give the agent one compact view of current deals ranked against the household's
own buying history and feedback.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter, defaultdict
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from shopping.core import service
from shopping.models import (
    DEAL_DISMISSED, SEL_APPLIED, Category, DealScan, Feedback, PastOrder, PastOrderLine, Preference, Product, Selection,
    TopDeal, utcnow,
)
from shopping.retailer.client import DEAL_FILTERS, RetailerError
from shopping.retailer.normalize import round2

log = logging.getLogger(__name__)

PAGE_SIZE = 48
MAX_PAGES_PER_CATEGORY = 12
FRESH_DAYS = 3  # a deal observation older than this (with no newer scan) is not trusted as "current"
MEAL_GROUPS = {  # aisle -> group, for meal planning
    "Meat": "Protein", "Fish & Seafood": "Protein", "Fruits & Vegetables": "Produce", "Dairy & Eggs": "Dairy & eggs",
    "Pantry": "Pantry", "Frozen Food": "Frozen", "Bakery": "Bakery", "Deli": "Deli",
}
MEAL_SUBAISLE_GROUPS = {"Frozen Meat & Seafood": "Protein", "Frozen Fruit": "Produce", "Frozen Vegetables": "Produce",
                        "Eggs": "Protein", "Tofu & Plant Based": "Protein"}
SKIP_FOR_MEALS = {"Ice Cream & Frozen Desserts"}  # desserts aren't meal ingredients
PER_SUBAISLE = 2


def meal_group(p: Product) -> str | None:
    leaf = (p.category_path or "").split(" > ")[-1]
    if leaf in SKIP_FOR_MEALS:
        return None
    if leaf in MEAL_SUBAISLE_GROUPS:
        return MEAL_SUBAISLE_GROUPS[leaf]
    return MEAL_GROUPS.get(p.aisle or "")
EXTRA_SWEEPS = {"flyer": DEAL_FILTERS["flyer"], "pc_points": {"promotions": ["PC Points"]}}


# =====================================================================================
# Deal scan
# =====================================================================================

_scan_lock = asyncio.Lock()


def last_scan(session: Session, *, done_only: bool = True) -> DealScan | None:
    q = select(DealScan).order_by(DealScan.started_at.desc()).limit(1)
    if done_only:
        q = q.where(DealScan.status == "done")
    return session.scalar(q)


def scan_running() -> bool:
    return _scan_lock.locked()


async def scan_deals(client, session_scope, *, store_id: str) -> int:
    """Sweep current deals aisle by aisle, recording prices and categories. Returns the scan id.
    Food is walked to sub-aisle level (Meat > Beef), other departments to aisle level."""
    async with _scan_lock:
        with session_scope() as session:
            scan = DealScan()
            session.add(scan)
            session.flush()
            scan_id = scan.id
        stats = {"pages": 0, "seen": 0}

        def record(products, path):
            with session_scope() as session:
                for np in products:
                    if np.sponsored:
                        continue
                    obs = service.record_observation(session, np, source="deal_scan", store_id=store_id)
                    if path:
                        service.set_category(session, obs.product, list(path), authoritative=False)
                    stats["seen"] += 1
                sc = session.get(DealScan, scan_id)
                sc.pages, sc.products_seen = stats["pages"], stats["seen"]

        async def fetch(filters, path, page):
            f = dict(filters)
            if path:
                f["category"] = [path[-1][0]]
            products, meta = await client._search("", f, page, PAGE_SIZE)  # noqa: SLF001 - same allowlisted search call
            stats["pages"] += 1
            children = []
            for g in meta.get("filter_groups") or []:
                if g.get("code") == "category":
                    children = [(str(v["code"]), v["name"], v.get("count") or 0) for v in g.get("values") or []
                                if not v.get("selected") and (v.get("count") or 0) > 0]
            return products, meta.get("total_results") or 0, children

        async def sweep(filters, path, first_page=None):
            """Record every page of one category (page 1 may already be in hand)."""
            products, total = first_page if first_page else (await fetch(filters, path, 1))[:2]
            record(products, path)
            pages = min(MAX_PAGES_PER_CATEGORY, -(-total // PAGE_SIZE))
            for page in range(2, pages + 1):
                more, _, _ = await fetch(filters, path, page)
                if not more:
                    break
                record(more, path)

        async def walk(filters, path, depth_limit):
            products, total, children = await fetch(filters, path, 1)
            # Only descend into real children of this node (the facet lists siblings at the root).
            if path and len(path) < depth_limit and children:
                with session_scope() as session:
                    service.remember_categories(session, path)
                # Real aisles first, so products also listed under a catch-all aisle keep their real one.
                for code, name, _count in sorted(children, key=lambda c: c[1] in service.CROSS_CUTTING_AISLES):
                    await walk(filters, path + [(code, name)], depth_limit)
            elif path:
                await sweep(filters, path, (products, total))
            else:  # root: children are departments
                for code, name, _count in children:
                    await walk(filters, [(code, name)], 3 if name == "Food" else 2)

        try:
            await walk(DEAL_FILTERS["price_reduction"], [], 0)
            for filters in EXTRA_SWEEPS.values():
                await sweep(filters, [])
            with session_scope() as session:
                scan = session.get(DealScan, scan_id)
                # Deals not seen in this sweep have ended (or never were deals); the scan is the authority.
                for p in session.scalars(select(Product).where(Product.current_on_sale.is_(True),
                                                               Product.current_observed_at < scan.started_at)):
                    p.current_on_sale = False
                scan.status, scan.finished_at = "done", utcnow()
                scan.pages, scan.products_seen = stats["pages"], stats["seen"]
            log.info("deal scan %s done: %s pages, %s products", scan_id, stats["pages"], stats["seen"])
        except Exception as exc:
            with session_scope() as session:
                scan = session.get(DealScan, scan_id)
                scan.status, scan.finished_at, scan.error = "failed", utcnow(), str(exc)[:500]
            log.warning("deal scan %s failed: %s", scan_id, exc)
            raise
        await backfill_categories(client, session_scope, store_id=store_id)
        return scan_id


async def backfill_categories(client, session_scope, *, store_id: str, limit: int = 40) -> int:
    """Products the household has bought but we haven't categorised: look them up once."""
    with session_scope() as session:
        bought = set(bought_stats(session))
        ids = list(session.scalars(select(Product.retailer_product_id)
                                   .where(Product.id.in_(bought), Product.category_code.is_(None)).limit(limit)))
    done = 0
    for rid in ids:
        try:
            np = await client.get_product(rid)
        except RetailerError:
            continue
        with session_scope() as session:
            service.record_observation(session, np, source="product", store_id=store_id)
        done += 1
    return done


async def scan_loop(client_factory, session_scope, *, store_id: str, every_hours: float) -> None:
    """Keep deals fresh: scan when the last good scan is older than `every_hours` (checked hourly)."""
    await asyncio.sleep(60)  # let startup finish
    while True:
        try:
            with session_scope() as session:
                last = last_scan(session)
                due = last is None or last.started_at < utcnow() - timedelta(hours=every_hours)
            if due and not scan_running():
                await scan_deals(client_factory(), session_scope, store_id=store_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - try again next hour
            log.exception("scheduled deal scan failed")
        await asyncio.sleep(3600)


_background: set[asyncio.Task] = set()


def start_scan(client, session_scope, *, store_id: str) -> bool:
    """Start a scan in the background (no-op if one is running). Returns whether one was started."""
    if scan_running():
        return False
    task = asyncio.create_task(scan_deals(client, session_scope, store_id=store_id), name="deal-scan-now")
    _background.add(task)
    task.add_done_callback(_background.discard)
    return True


def scan_dict(scan: DealScan | None) -> dict | None:
    if scan is None:
        return None
    return {"id": scan.id, "status": scan.status, "started_at": scan.started_at.isoformat(),
            "finished_at": scan.finished_at.isoformat() if scan.finished_at else None, "pages": scan.pages,
            "products_seen": scan.products_seen, "error": scan.error}


# =====================================================================================
# Household signals
# =====================================================================================

def bought_stats(session: Session) -> dict[int, dict]:
    """product id -> {times, last} from imported orders (what was actually bought) and items put in the cart."""
    out: dict[int, dict] = {}
    rows = session.execute(
        select(PastOrderLine.product_id, PastOrder.placed_at).join(PastOrder, PastOrder.id == PastOrderLine.order_id)
        .where(PastOrderLine.product_id.is_not(None), PastOrderLine.fulfilled.is_(True), PastOrderLine.is_fee.is_(False))
    ).all()
    for pid, at in rows:
        d = out.setdefault(pid, {"times": 0, "last": None})
        d["times"] += 1
        if at and (d["last"] is None or at > d["last"]):
            d["last"] = at
    for pid, at in session.execute(select(Selection.product_id, Selection.applied_to_cart_at).where(Selection.status == SEL_APPLIED)).all():
        d = out.setdefault(pid, {"times": 0, "last": None})
        d["times"] = max(d["times"], 1)  # put in the cart through the app; the order may not be imported yet
        if at and (d["last"] is None or at > d["last"]):
            d["last"] = at
    return out


def _negative_products(session: Session) -> set[int]:
    """Products the household has turned down: blacklisted, removed twice, or a dismissed top deal."""
    neg: set[int] = set()
    removed = Counter(session.scalars(select(Feedback.product_id).where(Feedback.action == "removed", Feedback.product_id.is_not(None))))
    neg |= {pid for pid, n in removed.items() if n >= 2}
    neg |= set(session.scalars(select(TopDeal.product_id).where(TopDeal.status == DEAL_DISMISSED)))
    targets = {p.target for p in session.scalars(select(Preference).where(Preference.active.is_(True),
                                                                          Preference.kind == "blacklist_product"))}
    if targets:
        neg |= set(session.scalars(select(Product.id).where(Product.retailer_product_id.in_(targets))))
    return neg


def _blacklisted_brands(session: Session) -> set[str]:
    return {(p.target or "").lower() for p in session.scalars(select(Preference).where(Preference.active.is_(True),
                                                                                       Preference.kind == "blacklist_brand"))}


# =====================================================================================
# Queries
# =====================================================================================

def _discount_pct(p: Product) -> float | None:
    if p.current_price is not None and p.current_regular_price and p.current_regular_price > p.current_price:
        return round(float((p.current_regular_price - p.current_price) / p.current_regular_price * 100), 1)
    return None


def current_deals_filter():
    today = service._local_day(utcnow())  # noqa: SLF001
    return [
        Product.current_on_sale.is_(True),
        Product.current_observed_at >= utcnow() - timedelta(days=FRESH_DAYS),
        or_(Product.current_deal_expiry.is_(None), Product.current_deal_expiry >= today),
        or_(Product.current_availability.is_(None), ~Product.current_availability.in_(["OUT", "OUT_OF_STOCK"])),
    ]


def brief(p: Product, bought: dict | None = None, *, with_history: Session | None = None) -> dict:
    d = {
        "product_id": p.retailer_product_id,
        "name": p.name,
        "brand": p.brand,
        "size": p.raw_size,
        "aisle": p.aisle,
        "category": p.category_path,
        "price": round2(p.current_price),
        "regular_price": round2(p.current_regular_price) if p.current_regular_price != p.current_price else None,
        "discount_pct": _discount_pct(p),
        "unit_price": None if p.current_unit_price is None else round(float(p.current_unit_price), 4),
        "unit_price_basis": p.current_unit_basis,
        "deal": p.current_deal_text,
        "deal_ends": p.current_deal_expiry,
        "sold_by": p.sold_by,
        "price_seen": p.current_observed_at.date().isoformat() if p.current_observed_at else None,
    }
    if bought:
        d["bought_before"] = {"times": bought["times"], "last": bought["last"].date().isoformat() if bought["last"] else None}
    if with_history is not None:
        st = service.price_stats(with_history, p)
        if st.get("days_observed", 0) >= 2:
            d["lowest_seen"] = st.get("min_price")
            d["is_lowest_seen"] = st.get("is_lowest_observed")
    return {k: v for k, v in d.items() if v is not None}


def search_catalog(session: Session, *, query: str = "", aisle: str | None = None, on_sale_only: bool = False,
                   bought_only: bool = False, limit: int = 30) -> list[dict]:
    """Search everything we've seen at the store (no retailer call). Words must all match name, brand or category."""
    q = select(Product)
    for word in [w for w in query.lower().split() if w][:6]:
        like = f"%{word}%"
        q = q.where(or_(func.lower(Product.name).like(like), func.lower(Product.brand).like(like),
                        func.lower(Product.category_path).like(like)))
    if aisle:
        q = q.where(func.lower(Product.aisle) == aisle.lower())
    if on_sale_only:
        q = q.where(*current_deals_filter())
    bought = bought_stats(session)
    if bought_only:
        q = q.where(Product.id.in_(list(bought)))
    rows = list(session.scalars(q.order_by(Product.current_observed_at.desc().nulls_last()).limit(500)))
    # Things bought before first, then deals, then the rest.
    rows.sort(key=lambda p: (p.id not in bought, not p.current_on_sale, -(bought.get(p.id, {}).get("times") or 0)))
    return [brief(p, bought.get(p.id)) for p in rows[:limit]]


def aisle_tree(session: Session) -> list[dict]:
    """Departments > aisles > sub-aisles, with how many products we know and how many are on sale now."""
    known = Counter(session.scalars(select(Product.category_code).where(Product.category_code.is_not(None))))
    deals = Counter(session.scalars(select(Product.category_code).where(Product.category_code.is_not(None), *current_deals_filter())))
    cats = list(session.scalars(select(Category).order_by(Category.path)))
    children = defaultdict(list)
    for c in cats:
        children[c.parent_code].append(c)

    def node(c: Category) -> dict:
        kids = [node(k) for k in children.get(c.code, [])]
        n = {"code": c.code, "name": c.name, "products": known.get(c.code, 0) + sum(k["products"] for k in kids),
             "on_sale": deals.get(c.code, 0) + sum(k["on_sale"] for k in kids)}
        if kids:
            n["children"] = kids
        return n

    return [node(c) for c in children.get(None, [])]


def _ranked_deals(session: Session, *, aisles: list[str] | None, food_only: bool) -> tuple[list[Product], dict[int, dict]]:
    bought = bought_stats(session)
    neg = _negative_products(session)
    bad_brands = _blacklisted_brands(session)
    q = select(Product).where(*current_deals_filter())
    if aisles:
        q = q.where(func.lower(Product.aisle).in_([a.lower() for a in aisles]))
    if food_only:
        q = q.where(Product.category_path.like("Food > %"))
    bought_aisles = Counter(p.aisle for p in session.scalars(select(Product).where(Product.id.in_(list(bought)))) if p.aisle)
    bought_cats = {p.category_code for p in session.scalars(select(Product).where(Product.id.in_(list(bought)))) if p.category_code}
    out = []
    for p in session.scalars(q):
        if p.id in neg or (p.brand or "").lower() in bad_brands:
            continue
        score = _discount_pct(p) or (8.0 if p.current_deal_text else 0.0)
        if p.id in bought:
            score += 25  # their own product on sale
        elif p.category_code in bought_cats:
            score += 10  # the kind of thing they buy
        elif p.aisle in bought_aisles:
            score += 3
        out.append((score, p))
    out.sort(key=lambda x: -x[0])
    return [p for _, p in out], bought


def shopping_context(session: Session, *, focus: str = "overview", aisles: list[str] | None = None, per_aisle: int = 6) -> dict:
    """One compact view for the agent: deal freshness, the household's own items on sale, the best deals per
    aisle (ranked toward what they buy, excluding what they've turned down), and recent review notes."""
    scan = last_scan(session)
    running = scan_running()
    meals = focus == "meals"
    if meals and not aisles:
        aisles = list(MEAL_GROUPS)
    ranked, bought = _ranked_deals(session, aisles=aisles, food_only=meals)
    yours = [p for p in ranked if p.id in bought][:25]
    by_aisle: dict[str, list[Product]] = defaultdict(list)
    variants: dict[int, int] = {}
    lines: dict[tuple, Product] = {}  # (brand, sub-aisle, price) -> first product: flavours of one product line
    per_leaf: Counter = Counter()
    for p in ranked:
        if p.id in bought:
            continue
        key = meal_group(p) if meals else (p.aisle or "Uncategorised")
        if key is None:
            continue
        line = (p.brand or "", p.category_code, p.current_price)
        if p.brand and line in lines:
            first = lines[line]
            variants[first.id] = variants.get(first.id, 1) + 1
            continue
        cap = per_aisle * (2 if meals and key in ("Protein", "Produce") else 1)
        if len(by_aisle[key]) >= cap or per_leaf[(key, p.category_code)] >= PER_SUBAISLE:
            continue
        lines[line] = p
        per_leaf[(key, p.category_code)] += 1
        by_aisle[key].append(p)
    notes = [
        {"date": f.created_at.date().isoformat(), "item": f.requirement, "action": f.action, "note": f.note}
        for f in session.scalars(select(Feedback).where(Feedback.note.is_not(None), Feedback.note != "",
                                                        Feedback.created_at >= utcnow() - timedelta(days=90))
                                 .order_by(Feedback.created_at.desc()).limit(15))
    ]
    total_deals = session.scalar(select(func.count(Product.id)).where(*current_deals_filter())) or 0
    return {
        "deal_data": {
            "last_scan": scan_dict(scan), "scan_running": running, "deals_known_now": total_deals,
            "note": None if scan else "No deal scan yet; call shopping_refresh_deals (takes ~3 minutes) for full coverage.",
        },
        "your_items_on_sale": [brief(p, bought.get(p.id), with_history=session) for p in yours],
        ("meal_deals" if meals else "top_deals_by_aisle"): {
            k: [{**brief(p, with_history=session), **({"other_variants_same_price": variants[p.id] - 1} if p.id in variants else {})}
                for p in v]
            for k, v in sorted(by_aisle.items(), key=lambda kv: kv[0])
        },
        "recent_review_notes": notes,
        "active_top_deals": session.scalar(select(func.count(TopDeal.id)).where(TopDeal.status == "active")) or 0,
    }
