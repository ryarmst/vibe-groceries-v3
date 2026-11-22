"""MCP interface for the shopping agent.

A deliberately small, semantic tool surface. There are no tools for checkout, orders,
payment, timeslots or arbitrary HTTP. The only cart-changing tool applies items the
*user* has approved in the web UI.
"""

from __future__ import annotations

import functools
import logging
import re
import time
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field
from sqlalchemy import select

from shopping import context
from shopping.auth.session import AuthRequired
from shopping.config import settings
from shopping.core import catalog as cat
from shopping.core import deals as dls
from shopping.core import feedback as fbk
from shopping.core import orders as ords
from shopping.core import service
from shopping.database import session_scope
from shopping.models import REASON_CODES, PastOrder, ShoppingRun
from shopping.retailer.client import RetailerError

log = logging.getLogger("shopping.mcp")

INSTRUCTIONS = f"""\
Grocery shopping assistant for No Frills (store {settings.store_id}). Workflow:
1. Start with shopping_get_context: what's on sale now (from a daily scan of every aisle), the household's own
   items on sale, the best deals per aisle ranked toward what they buy, and recent review notes. For meal
   planning use focus="meals". shopping_search_catalog searches everything we've seen (fast, no retailer call);
   shopping_search_products gets live prices. Then shopping_get_preferences.
2. shopping_create_run, then stage items with shopping_add_selection (one product) or
   shopping_add_choice_group (several candidate products for one requirement, with a recommendation).
   Always give reason codes and a short rationale. Reason codes: {", ".join(REASON_CODES)}.
   Use the exact item name from the user's list/pantry as `requirement` so history lines up across runs.
   For regular purchases, check shopping_get_requirement_history first and only stage items that are due.
3. shopping_submit_run_for_review and give the user the review URL. The user triages items in the web UI,
   often recording why they removed or swapped something; read shopping_get_feedback to learn from it,
   and after distilling it elsewhere (e.g. into notes), call shopping_mark_feedback_reviewed.
4. Approving an item in the web UI adds it to the cart immediately. shopping_apply_approved only retries
   approved items that couldn't be added (e.g. the session had expired); never use it on unapproved items.
Top deals: when you spot a standout price (lowest we've seen, a big % off on something the user buys, a
stock-up price on a staple), flag it with shopping_add_top_deal. It appears on the Deals page where the user can
add it to the cart in one tap; it is not a run item. Check shopping_get_top_deals first: don't re-flag dismissed
deals, and learn from dismissal reasons.
Substitutions are per item and OFF by default. Allow them only where a swap is no big deal (commodity
produce, same-% milk, canned staples, a generic brand); never for items chosen because of a special deal,
a preferred brand/product/variety, or anything with an "Avoid" or "Subs: never" preference.
This service cannot check out or place orders, and never edits an order that has been submitted.
Blacklist preferences are absolute; other preferences are guidance.
"""

mcp = MCPServer(name="shopping-assistant", instructions=INSTRUCTIONS, version="0.1.0")


def logged(fn):
    """Log tool name, duration, outcome and key identifiers (never secrets)."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        started = time.monotonic()
        ids = {k: v for k, v in kwargs.items() if k in ("run_id", "product_id", "selection_id", "query") and v}
        try:
            result = await fn(*args, **kwargs)
            log.info("mcp tool=%s ok duration_ms=%.0f %s", fn.__name__, (time.monotonic() - started) * 1000, ids)
            return result
        except Exception as exc:
            log.warning("mcp tool=%s failed duration_ms=%.0f %s error=%s", fn.__name__, (time.monotonic() - started) * 1000, ids, exc)
            # MCP only shows the agent messages from ToolError; our own errors are written for it to read.
            if isinstance(exc, (service.ShoppingError, RetailerError, AuthRequired)):
                raise ToolError(str(exc)) from exc
            raise

    return wrapper


def _review_url(run_id: str) -> str:
    return f"#/runs/{run_id}"  # a link inside the app


def _annotate(session, products, source: str) -> list[dict]:
    """Record observations, then add preference flags and our own price history."""
    prefs = service.active_preferences(session)
    for p in products:
        service.record_observation(session, p, source=source, store_id=settings.store_id)
    ids = [p.retailer_product_id for p in products]
    history = service.history_brief(session, ids, settings.store_id)
    past_feedback = fbk.feedback_by_product(session, ids)
    out = []
    for p in products:
        d = p.summary()
        flags = service.preference_flags(prefs, p)
        if flags:
            d["preferences"] = flags
        if p.retailer_product_id in history:
            d["history"] = history[p.retailer_product_id]
        if p.retailer_product_id in past_feedback:
            d["user_feedback"] = past_feedback[p.retailer_product_id]
        out.append({k: v for k, v in d.items() if v is not None})
    return out


# -------------------------------------------------------------------------------------
# Catalogue
# -------------------------------------------------------------------------------------

@mcp.tool()
@logged
async def shopping_search_products(
    query: Annotated[str, Field(description="Search text, e.g. 'peanut butter'")],
    page: Annotated[int, Field(ge=1, le=20, description="Result page (48 per page)")] = 1,
    limit: Annotated[int, Field(ge=1, le=48, description="Max products to return")] = 24,
    brand: Annotated[str | None, Field(description="Only products whose brand contains this text")] = None,
    comparison_family: Annotated[str | None, Field(description="weight | volume | count")] = None,
    sort_by_unit_price: Annotated[bool, Field(description="Sort cheapest per 100 g / 100 ml / item first")] = False,
) -> dict:
    """Search current No Frills products. Returns normalized prices, unit prices, deals,
    preference flags and our own price history where we have it. Sponsored results are excluded."""
    res = await context.client().search_products(query, page=page)
    products = res["products"]
    if brand:
        products = [p for p in products if brand.lower() in (p.brand or "").lower()]
    if comparison_family:
        products = [p for p in products if p.comparison_family == comparison_family]
    if sort_by_unit_price:
        products.sort(key=lambda p: (p.comparison_family, p.unit_price is None, p.unit_price or 0))
    with session_scope() as session:
        results = _annotate(session, products[:limit], "search")
    return {"query": query, "page": page, "total_results": res["total_results"], "returned": len(results), "results": results}


@mcp.tool()
@logged
async def shopping_get_product(product_id: Annotated[str, Field(description="Retailer product id, e.g. 20039581001_EA")]) -> dict:
    """Current details for one product plus our observed price history statistics."""
    p = await context.client().get_product(product_id)
    with session_scope() as session:
        service.record_observation(session, p, source="product", store_id=settings.store_id)
        product = service.upsert_product(session, p)
        d = p.summary()
        d["gtin"] = p.gtin
        d["history"] = service.price_stats(session, product, store_id=settings.store_id)
        flags = service.preference_flags(service.active_preferences(session), p)
        if flags:
            d["preferences"] = flags
    return {k: v for k, v in d.items() if v is not None}


@mcp.tool()
@logged
async def shopping_get_deals(
    kind: Annotated[str, Field(description="'flyer' (weekly flyer items) or 'price_reduction' (all items with a reduced price)")] = "flyer",
    query: Annotated[str, Field(description="Optional search text to narrow deals")] = "",
    category_code: Annotated[str | None, Field(description="Optional aisle category code from search filter groups")] = None,
    min_discount_pct: Annotated[float | None, Field(description="Only items at least this % below regular price")] = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 30,
) -> dict:
    """Current deals. Includes retailer deal text (SALE / LIMIT / PC Optimum points) and, where we have
    history, how the price compares with what we've observed before."""
    products = await context.client().get_deals(query=query, kind=kind, category_code=category_code, min_discount_pct=min_discount_pct, limit=limit)
    with session_scope() as session:
        results = _annotate(session, products, "deals")
    return {"kind": kind, "query": query, "returned": len(results), "results": results}


# -------------------------------------------------------------------------------------
# Cart (read) and apply approved
# -------------------------------------------------------------------------------------

@mcp.tool()
@logged
async def shopping_get_cart() -> dict:
    """The current retailer cart (requires the user to have connected their session on /auth)."""
    cart = await context.client().get_cart()
    return cart.summary()


@mcp.tool()
@logged
async def shopping_apply_approved(run_id: str) -> dict:
    """Add the items the USER approved in this run to the retailer cart. Pending or rejected items are
    never added. Quantities are raised to the approved amount, never lowered. Does not check out."""
    with session_scope() as session:
        result = await service.apply_approved(session, context.client(), run_id)
        result["run_status"] = service.get_run(session, run_id).status
    return result


@mcp.tool()
@logged
async def shopping_auth_status() -> dict:
    """Whether the retailer session is connected (needed for cart operations). Contains no secrets."""
    st = context.auth.status()
    return {"state": st.state, "label": st.label, "seconds_remaining": st.seconds_remaining,
            "reconnect": "Someone signs in under Settings → No Frills account in the app."}


# -------------------------------------------------------------------------------------
# Runs and selections
# -------------------------------------------------------------------------------------

@mcp.tool()
@logged
async def shopping_create_run(summary: Annotated[str, Field(description="e.g. 'Weekly groceries'")], notes: str | None = None) -> dict:
    """Start a new shopping run to stage selections in."""
    with session_scope() as session:
        run = service.create_run(session, summary=summary, store_id=settings.store_id, notes=notes)
        return {"run_id": run.id, "status": run.status, "review_url": _review_url(run.id)}


@mcp.tool()
@logged
async def shopping_get_run(run_id: str, include_history: bool = False) -> dict:
    """A run with its selections, choice groups, statuses and estimated total."""
    with session_scope() as session:
        d = service.run_dict(session, service.get_run(session, run_id), with_history=include_history)
        d["review_url"] = _review_url(run_id)
        return d


@mcp.tool()
@logged
async def shopping_list_runs(limit: Annotated[int, Field(ge=1, le=50)] = 10, active_only: bool = False) -> dict:
    """Recent shopping runs, newest first."""
    with session_scope() as session:
        q = select(ShoppingRun).order_by(ShoppingRun.created_at.desc()).limit(limit)
        if active_only:
            q = q.where(ShoppingRun.status.in_(service.ACTIVE_RUN_STATES))
        runs = list(session.scalars(q))
        return {"runs": [{"run_id": r.id, "summary": r.summary, "status": r.status, "created_at": r.created_at.isoformat(),
                          "items": len(service.chosen_selections(r)), "estimated_total": float(round(service.estimated_total(r), 2))} for r in runs]}


@mcp.tool()
@logged
async def shopping_add_selection(
    run_id: str,
    product_id: Annotated[str, Field(description="Retailer product id from search results")],
    quantity: Annotated[float, Field(gt=0)] = 1,
    reasons: Annotated[list[str], Field(description=f"One or more of: {', '.join(REASON_CODES)}")] = [],  # noqa: B006
    rationale: Annotated[str | None, Field(description="One or two sentences explaining why")] = None,
    requirement: Annotated[str | None, Field(description="The need this fills, e.g. 'milk'")] = None,
    substitution_allowed: Annotated[bool, Field(description="Let the shopper substitute if it's unavailable. Only for items where a swap is no big deal (see instructions); never for items chosen as a special deal or a specific brand/variety.")] = False,
    substitution_note: Annotated[str | None, Field(description="Short instruction for the shopper, e.g. 'any brand, same size'")] = None,
    substitute_product_id: Annotated[str | None, Field(description="Optional specific replacement product id (implies substitutions allowed)")] = None,
) -> dict:
    """Stage one product in a run for the user to review. Fetches the current price and records it."""
    with session_scope() as session:
        sel = await service.add_selection(session, context.client(), run_id, product_id=product_id, quantity=quantity,
                                          reasons=reasons, rationale=rationale, requirement=requirement,
                                          substitution_allowed=substitution_allowed, substitution_note=substitution_note,
                                          substitute_product_id=substitute_product_id)
        return service.selection_dict(session, sel)


class ChoiceOption(BaseModel):
    product_id: str
    quantity: float = Field(1, gt=0)
    reasons: list[str] = []
    rationale: str | None = None
    substitution_allowed: bool = False
    substitution_note: str | None = None
    substitute_product_id: str | None = None


COMPACT_KEYS = ("product_id", "name", "brand", "package_size", "sold_by", "price", "regular_price", "discount_pct", "unit_price",
                "unit_price_basis", "deal", "deal_expiry", "pc_optimum", "availability", "max_quantity", "preferences", "history",
                "user_feedback")
COMPACT_HISTORY = ("min_price", "avg_90d", "is_lowest_observed", "vs_avg_90d_pct", "package_size_changed")


_SIZE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(ml|l|litres?|liters?|g|kg|lbs?|oz)\b", re.I)
_TO_BASE = {"ml": ("ml", 1), "l": ("ml", 1000), "litre": ("ml", 1000), "litres": ("ml", 1000), "liter": ("ml", 1000),
            "liters": ("ml", 1000), "g": ("g", 1), "kg": ("g", 1000), "lb": ("g", 453.6), "lbs": ("g", 453.6), "oz": ("g", 28.35)}


def parse_size(text: str) -> tuple[float, str] | None:
    """'2% milk 1 L' -> (1000.0, 'ml'); '1.36 kg' -> (1360.0, 'g'). Percentages and counts aren't sizes."""
    m = _SIZE.search(text or "")
    if not m:
        return None
    unit, factor = _TO_BASE[m.group(2).lower()]
    return float(m.group(1).replace(",", ".")) * factor, unit


def same_size(a: tuple[float, str] | None, b: tuple[float, str] | None) -> bool:
    return bool(a and b and a[1] == b[1] and abs(a[0] - b[0]) <= 0.03 * b[0])


@mcp.tool()
@logged
async def shopping_search_many(
    queries: Annotated[list[str], Field(min_length=1, max_length=15, description="Several searches at once, e.g. ['2% milk 4L', 'sourdough bread', 'gala apples']")],
    per_query: Annotated[int, Field(ge=1, le=10, description="Results per search")] = 5,
    sort_by_unit_price: Annotated[bool, Field(description="Cheapest per 100 g / 100 ml / item first (within the most common size family)")] = True,
) -> dict:
    """Search for many needs in ONE call (preferred over repeated shopping_search_products). Compact live results per
    query, with deals, unit prices, preference flags and our price history. Sponsored and out-of-stock items are left out."""
    out = {}
    client = context.client()
    for q in dict.fromkeys(x.strip() for x in queries if x.strip()):
        try:
            products = [p for p in (await client.search_products(q))["products"] if not (p.availability or "").upper().startswith("OUT")]
        except RetailerError as exc:
            out[q] = {"error": str(exc)}
            continue
        wanted = parse_size(q)
        if sort_by_unit_price and products:
            fams = {}
            for p in products[:12]:
                fams[p.comparison_family] = fams.get(p.comparison_family, 0) + 1
            main = max(fams, key=fams.get)
            products.sort(key=lambda p: (p.comparison_family != main, p.unit_price is None, p.unit_price or 0))
        if wanted:  # the search named a size ("milk 1 L"): that size first, cheapest first within it
            products.sort(key=lambda p: not same_size(parse_size(p.raw_size or ""), wanted))
        with session_scope() as session:
            rows = _annotate(session, products[:per_query], "search")
        compact = []
        for r in rows:
            c = {k: r[k] for k in COMPACT_KEYS if k in r}
            if isinstance(c.get("history"), dict):
                c["history"] = {k: v for k, v in c["history"].items() if k in COMPACT_HISTORY and v is not None}
            compact.append(c)
        out[q] = compact
    return {"results": out}


class StageItem(BaseModel):
    """One requirement: either a single product (product_id) or a choice between products (options)."""
    requirement: str = Field(description="Exact Notion item name, e.g. 'Milk'")
    product_id: str | None = Field(default=None, description="For a single product")
    quantity: float = Field(1, gt=0)
    reasons: list[str] = Field(default_factory=list)
    rationale: str | None = None
    substitution_allowed: bool = False
    substitution_note: str | None = None
    options: list[ChoiceOption] | None = Field(default=None, description="For a real trade-off: 2–4 candidates (instead of product_id)")
    recommended_index: int = 0


@mcp.tool()
@logged
async def shopping_stage_items(
    items: Annotated[list[StageItem], Field(min_length=1, max_length=40)],
    run_id: Annotated[str | None, Field(description="Existing run; leave empty to create one with `summary`")] = None,
    summary: Annotated[str | None, Field(description="Title for a new run, e.g. 'Weekly groceries — Nov 26'")] = None,
    notes: str | None = None,
    submit: Annotated[bool, Field(description="Also submit the run for review (do this on your last batch)")] = False,
) -> dict:
    """Stage MANY items in one call (preferred over repeated shopping_add_selection / shopping_add_choice_group):
    creates the run if needed, adds each single product or choice group, and optionally submits for review.
    Failures (e.g. out of stock) are reported per item; the rest still go in."""
    client = context.client()
    with session_scope() as session:
        if not run_id:
            run_id = service.create_run(session, summary=summary or "Shopping run", store_id=settings.store_id, notes=notes).id
    results = []
    for it in items:
        try:
            with session_scope() as session:
                if it.options:
                    opts = [o.model_dump() for o in it.options]
                    group = await service.add_choice_group(session, client, run_id, label=it.requirement, options=opts,
                                                           recommended_index=min(it.recommended_index, len(opts) - 1))
                    for sel in group.selections:
                        sel.requirement = it.requirement
                    results.append({"requirement": it.requirement, "ok": True, "choice_group_id": group.id, "options": len(opts)})
                elif it.product_id:
                    sel = await service.add_selection(session, client, run_id, product_id=it.product_id, quantity=it.quantity,
                                                      reasons=it.reasons, rationale=it.rationale, requirement=it.requirement,
                                                      substitution_allowed=it.substitution_allowed, substitution_note=it.substitution_note)
                    results.append({"requirement": it.requirement, "ok": True, "selection_id": sel.id,
                                    "price": service.selection_dict(session, sel).get("observed_price")})
                else:
                    results.append({"requirement": it.requirement, "ok": False, "error": "Give product_id or options."})
        except (service.ShoppingError, RetailerError) as exc:
            results.append({"requirement": it.requirement, "ok": False, "error": str(exc)})
    with session_scope() as session:
        run = service.get_run(session, run_id)
        if submit:
            service.submit_run_for_review(session, run_id)
        return {"run_id": run_id, "review_url": _review_url(run_id), "status": run.status, "staged": sum(r["ok"] for r in results),
                "failed": [r for r in results if not r["ok"]], "estimated_total": float(service.estimated_total(run))}


class QuickItem(BaseModel):
    """One product to put straight into the cart."""
    product_id: str = Field(description="retailer product id from search results")
    requirement: str = Field("", description="What was asked for, e.g. 'Milk' (defaults to the product name)")
    quantity: float = Field(1, gt=0, le=12, description="Whole packages (or kg for by-weight items)")
    rationale: str | None = Field(default=None, description="One line: why this product")
    substitution_allowed: bool = False
    substitution_note: str | None = None


QUICK_MAX_ITEMS = 25


@mcp.tool()
@logged
async def shopping_quick_add(
    items: Annotated[list[QuickItem], Field(min_length=1, max_length=QUICK_MAX_ITEMS)],
    summary: Annotated[str, Field(description="Short title, e.g. 'Taco night'")] = "Quick Shop",
) -> dict:
    """QUICK SHOP ONLY: add these products straight to the No Frills cart (no review step). It records a run
    (approved on the person's behalf) so the cart change shows in history and can be undone from the run or
    cart page. Raises the cart quantity to at least the requested amount; never removes anything."""
    client = context.client()
    await client.get_cart()  # fail fast (and clearly) if the No Frills session isn't signed in
    with session_scope() as session:
        run_id = service.create_run(session, summary=f"Quick Shop: {summary}"[:200], store_id=settings.store_id).id
    staged, failed = [], []
    for it in items:
        try:
            with session_scope() as session:
                sel = await service.add_selection(session, client, run_id, product_id=it.product_id, quantity=it.quantity,
                                                  reasons=["user_requested"], rationale=it.rationale,
                                                  requirement=it.requirement or None,
                                                  substitution_allowed=it.substitution_allowed,
                                                  substitution_note=it.substitution_note)
                staged.append(sel.id)
        except (service.ShoppingError, RetailerError) as exc:
            failed.append({"requirement": it.requirement or it.product_id, "error": str(exc)})
    if not staged:
        with session_scope() as session:
            service.cancel_run(session, run_id)
        return {"added": [], "failed": failed, "message": "Nothing could be added."}
    with session_scope() as session:
        service.submit_run_for_review(session, run_id)
        service.approve_all_current(session, run_id, staged)
        applied = await service.apply_approved(session, client, run_id, source="quick_shop")
    failed += [{"requirement": r["name"], "error": r["error"]} for r in applied["results"] if not r["ok"]]
    cart = await client.get_cart()
    return {"run_id": run_id, "run_url": _review_url(run_id),
            "added": [{"name": r["name"], "quantity_in_cart": r["quantity_after"], "was": r["quantity_before"]}
                      for r in applied["results"] if r["ok"]],
            "failed": failed, "cart_items": len(cart.lines), "cart_estimated_total": cart.estimated_total}


@mcp.tool()
@logged
async def shopping_add_choice_group(
    run_id: str,
    label: Annotated[str, Field(description="The requirement, e.g. 'Peanut butter'")],
    options: Annotated[list[ChoiceOption], Field(min_length=1, max_length=8)],
    recommended_index: Annotated[int, Field(ge=0, description="Index in options of your recommendation")] = 0,
) -> dict:
    """Stage several candidate products for one requirement. The user picks one; yours is preselected.
    Non-recommended options automatically get the 'alternative' reason."""
    with session_scope() as session:
        group = await service.add_choice_group(session, context.client(), run_id, label=label,
                                               options=[o.model_dump() for o in options], recommended_index=recommended_index)
        return {"choice_group_id": group.id, "label": group.label, "recommended_selection_id": group.recommended_selection_id,
                "options": [service.selection_dict(session, s) for s in group.selections]}


@mcp.tool()
@logged
async def shopping_update_selection(
    selection_id: str,
    quantity: Annotated[float | None, Field(gt=0)] = None,
    rationale: str | None = None,
    reasons: list[str] | None = None,
    withdraw: Annotated[bool, Field(description="Withdraw this suggestion")] = False,
    substitution_allowed: bool | None = None,
    substitution_note: str | None = None,
    substitute_product_id: str | None = None,
) -> dict:
    """Change a pending selection's quantity, rationale, reasons or substitution preference, or withdraw it. Cannot approve."""
    with session_scope() as session:
        sel = service.update_selection(session, selection_id, quantity=quantity, rationale=rationale, reasons=reasons, withdraw=withdraw,
                                       substitution_allowed=substitution_allowed, substitution_note=substitution_note,
                                       substitute_product_id=substitute_product_id)
        return service.selection_dict(session, sel)


@mcp.tool()
@logged
async def shopping_submit_run_for_review(run_id: str) -> dict:
    """Mark the run ready for the user's review. Returns the URL to give the user."""
    with session_scope() as session:
        run = service.submit_run_for_review(session, run_id)
        return {"run_id": run.id, "status": run.status, "estimated_total": float(round(service.estimated_total(run), 2)),
                "review_url": _review_url(run.id)}


# -------------------------------------------------------------------------------------
# Preferences and history
# -------------------------------------------------------------------------------------

@mcp.tool()
@logged
async def shopping_get_preferences() -> dict:
    """Active shopping preferences. Blacklists (absolute=true) must be respected; others are guidance."""
    with session_scope() as session:
        return {"preferences": [service.preference_dict(p) for p in service.active_preferences(session)]}


@mcp.tool()
@logged
async def shopping_add_preference_note(
    note: Annotated[str, Field(description="The user's stated preference, in their words")],
    scope: Annotated[str | None, Field(description="Item type it applies to, e.g. 'coffee'")] = None,
) -> dict:
    """Record a free-form preference the user stated in conversation (e.g. 'whole bean coffee unless a big sale')."""
    with session_scope() as session:
        pref = service.add_preference(session, kind="preference_note", scope=scope, note=note)
        return service.preference_dict(pref)


@mcp.tool()
@logged
async def shopping_get_product_history(product_id: str, limit: Annotated[int, Field(ge=1, le=200)] = 30) -> dict:
    """Observed prices, package sizes and unit prices for a product over time, plus past selections."""
    with session_scope() as session:
        return service.product_history(session, product_id, limit=limit)


@mcp.tool()
@logged
async def shopping_get_selection_history(
    product_id: str | None = None,
    query: Annotated[str | None, Field(description="Match product name, brand or requirement, e.g. 'milk'")] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 30,
) -> dict:
    """Past suggestions: when, how often, typical quantity, approved vs rejected, price each time."""
    with session_scope() as session:
        return service.selection_history(session, product_id=product_id, query=query, limit=limit)


@mcp.tool()
@logged
async def shopping_get_recent_price_history(days: Annotated[int, Field(ge=1, le=365)] = 30, limit: Annotated[int, Field(ge=1, le=200)] = 30) -> dict:
    """Products whose price or package size changed recently (largest changes first)."""
    with session_scope() as session:
        return {"days": days, "changes": service.recent_price_changes(session, days=days, limit=limit, store_id=settings.store_id)}


# -------------------------------------------------------------------------------------
# Feedback and regular purchases
# -------------------------------------------------------------------------------------

@mcp.tool()
@logged
async def shopping_get_feedback(
    days: Annotated[int, Field(ge=1, le=730)] = 90,
    requirement: Annotated[str | None, Field(description="Only feedback about this item, e.g. 'milk'")] = None,
    product_id: str | None = None,
    action: Annotated[str | None, Field(description="removed | chose_alternative | approved | quantity_changed | note")] = None,
    unreviewed_only: bool = False,
    limit: Annotated[int, Field(ge=1, le=500)] = 100,
) -> dict:
    """What the user removed or swapped during review and why (reason tags + their own words), plus
    summary counts and detected patterns. Use it to avoid repeating suggestions the user rejected."""
    with session_scope() as session:
        rows = fbk.list_feedback(session, days=days, action=action, requirement=requirement,
                                 product_retailer_id=product_id, unreviewed_only=unreviewed_only, limit=limit)
        summary = fbk.feedback_summary(session, days=days)
        summary.pop("by_tag_code", None)
        return {"summary": summary, "records": [fbk.feedback_dict(r) for r in rows]}


@mcp.tool()
@logged
async def shopping_get_requirement_history(
    requirements: Annotated[list[str], Field(min_length=1, max_length=100,
                                             description="Item names, e.g. the pantry items flagged Regular Purchase")],
    default_cadence_days: Annotated[int, Field(ge=1, le=60, description="Assumed interval when there's no history")] = 7,
) -> dict:
    """For each item: when it was last added to the cart (product, quantity, price), the typical interval
    between purchases, whether the user recently removed it because they had enough, the preferred product
    for it, and a due / not_due / unknown verdict. Use this to decide which regular purchases to stage."""
    with session_scope() as session:
        return {"requirements": fbk.requirement_history(session, requirements, default_cadence_days=default_cadence_days)}


@mcp.tool()
@logged
async def shopping_mark_feedback_reviewed(
    feedback_ids: Annotated[list[int], Field(min_length=1, max_length=500)],
    resolution: Annotated[str, Field(description="What was done, e.g. 'Notion: Bread → Shopping Preferences updated'")],
) -> dict:
    """Mark feedback records as handled (e.g. after distilling them into Notion's Shopping Preferences),
    so later syncs with unreviewed_only=true don't process them twice."""
    with session_scope() as session:
        return {"marked": fbk.mark_reviewed(session, feedback_ids, resolution)}


# -------------------------------------------------------------------------------------
# Past orders (what was actually bought)
# -------------------------------------------------------------------------------------

@mcp.tool()
@logged
async def shopping_get_past_orders(limit: Annotated[int, Field(ge=1, le=50)] = 10) -> dict:
    """Recent placed orders from the user's No Frills account (newest first), and whether each has been
    imported. Needs the retailer session."""
    summaries = await context.client().list_past_orders()
    with session_scope() as session:
        imported = set(session.scalars(select(PastOrder.id)))
        return {"orders": [{**ords.summary_dict(s, None), "imported": s.order_id in imported} for s in summaries[:limit]]}


@mcp.tool()
@logged
async def shopping_get_past_order(order_id: Annotated[str, Field(description="Order number from shopping_get_past_orders")]) -> dict:
    """One placed order: every item actually bought (quantity or weight, price paid), which run item
    suggested it, items approved in the app but not bought (removed at checkout), and items added on
    nofrills.ca outside the app. Importing it records the purchases and prices paid in history, which
    also makes shopping_get_requirement_history reflect real purchases."""
    client = context.client()
    summaries = await client.list_past_orders()
    data = await client.get_past_order(order_id)
    with session_scope() as session:
        order = ords.import_order(session, data, next((s for s in summaries if s.order_id == order_id), None), store_id=settings.store_id)
        return ords.order_view(session, order)


# -------------------------------------------------------------------------------------
# Top deals (the Deals page)
# -------------------------------------------------------------------------------------

def _deals_url() -> str:
    return "#/deals"


@mcp.tool()
@logged
async def shopping_add_top_deal(
    product_id: Annotated[str, Field(description="Retailer product id, e.g. 20039581001_EA")],
    why: Annotated[str, Field(description="One or two sentences with the numbers: price vs regular / our history / unit price, and why it matters to this household")],
    headline: Annotated[str | None, Field(description="Short tag shown on the card, e.g. \"Lowest we've seen\", \"45% off\", \"Stock-up price\". Defaults to the % off.")] = None,
    relevant_to: Annotated[str | None, Field(description="The pantry / list item this is for, using its exact Notion name (e.g. 'Eggs')")] = None,
    rating: Annotated[int, Field(ge=1, le=3, description="1 good · 2 great · 3 stock up (exceptional; buy several)")] = 2,
    suggested_quantity: Annotated[float, Field(gt=0, le=24, description="Quantity pre-filled on the Add button")] = 1,
) -> dict:
    """Flag a standout deal for the user's Deals page, at the product's current price. The user adds it to the
    cart from there (or dismisses it, with a reason you can read in shopping_get_top_deals). Re-flagging a
    product that is already listed updates it. Use sparingly: a handful of genuinely good deals per shop."""
    p = await context.client().get_product(product_id)
    with session_scope() as session:
        deal = dls.flag_deal(session, p, headline=headline, why=why, relevant_to=relevant_to, rating=rating,
                             suggested_quantity=suggested_quantity, added_by="agent", store_id=settings.store_id)
        return {"deal": dls.deal_dict(session, deal, store_id=settings.store_id), "deals_url": _deals_url()}


@mcp.tool()
@logged
async def shopping_get_top_deals(
    include_closed: Annotated[bool, Field(description="Also return deals added to the cart, dismissed or ended recently")] = True,
    days: Annotated[int, Field(ge=1, le=365, description="How far back to include closed deals")] = 60,
) -> dict:
    """Deals on the Deals page and what the user did with them: added_to_cart, dismissed (with their reason in
    response_note), or retired/ended. Deals whose sale has ended are retired automatically."""
    with session_scope() as session:
        deals = [dls.deal_dict(session, d, store_id=settings.store_id) for d in dls.list_deals(session, include_closed=include_closed, days=days)]
    return {"deals_url": _deals_url(), "active": sum(1 for d in deals if d["status"] == "active"), "deals": deals}


@mcp.tool()
@logged
async def shopping_retire_top_deal(
    deal_id: int,
    reason: Annotated[str, Field(description="e.g. 'price back to regular', 'out of stock', 'flagged by mistake'")],
) -> dict:
    """Take a deal off the Deals page (it stays in history)."""
    with session_scope() as session:
        return {"deal": dls.deal_dict(session, dls.retire(session, deal_id, reason), store_id=settings.store_id)}


# -------------------------------------------------------------------------------------
# Catalogue and deal context
# -------------------------------------------------------------------------------------

@mcp.tool()
@logged
async def shopping_get_context(
    focus: Annotated[str, Field(description="'overview' (all aisles) or 'meals' (proteins, produce, dairy, pantry, frozen, grouped for meal planning)")] = "overview",
    aisles: Annotated[list[str] | None, Field(description="Only these aisles, e.g. ['Meat', 'Fish & Seafood'] (names from shopping_list_aisles)")] = None,
    per_aisle: Annotated[int, Field(ge=1, le=20, description="Deals per aisle/group")] = 6,
) -> dict:
    """Start here. Current deals ranked against the household's history: `your_items_on_sale` (things they've
    bought, on sale now), the best other deals per aisle (boosted when it's the kind of thing they buy; anything
    they've turned down is excluded), recent review notes in their own words, and how fresh the deal data is."""
    with session_scope() as session:
        return cat.shopping_context(session, focus=focus, aisles=aisles, per_aisle=per_aisle)


@mcp.tool()
@logged
async def shopping_search_catalog(
    query: Annotated[str, Field(description="Words to match in name, brand or category, e.g. 'chicken thigh'")] = "",
    aisle: Annotated[str | None, Field(description="Limit to one aisle, e.g. 'Meat'")] = None,
    on_sale_only: bool = False,
    bought_before_only: Annotated[bool, Field(description="Only products the household has bought")] = False,
    limit: Annotated[int, Field(ge=1, le=100)] = 30,
) -> dict:
    """Search every product we've seen at this store, with its latest known price/deal, category and the
    household's purchase count. Instant (no retailer call), but prices are as of `price_seen`: confirm live
    with shopping_get_product or shopping_search_products before staging."""
    with session_scope() as session:
        rows = cat.search_catalog(session, query=query, aisle=aisle, on_sale_only=on_sale_only, bought_only=bought_before_only, limit=limit)
    return {"returned": len(rows), "results": rows}


@mcp.tool()
@logged
async def shopping_list_aisles() -> dict:
    """The store's aisle tree (department > aisle > sub-aisle) with how many products we know and how many are on sale."""
    with session_scope() as session:
        return {"aisles": cat.aisle_tree(session), "last_scan": cat.scan_dict(cat.last_scan(session))}


@mcp.tool()
@logged
async def shopping_refresh_deals() -> dict:
    """Start a fresh scan of every aisle's deals (about 3 minutes, runs in the background; it also runs daily on
    its own). Check progress with shopping_get_context's deal_data."""
    with session_scope() as session:
        last = cat.scan_dict(cat.last_scan(session, done_only=False))
    if not cat.start_scan(context.client(), session_scope, store_id=settings.store_id):
        return {"started": False, "message": "A scan is already running.", "last_scan": last}
    return {"started": True, "message": "Scanning deals; ready in about 3 minutes.", "previous_scan": last}


def tool_names() -> list[str]:
    return [t.name for t in mcp._tool_manager.list_tools()]  # noqa: SLF001

