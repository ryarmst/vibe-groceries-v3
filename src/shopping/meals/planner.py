"""Deterministic meal-planning helpers.

The model chooses meals and writes them up; these functions do the arithmetic and matching it would
otherwise guess at: which week it is, what's expiring, which regular recipes line up with this week's deals,
and roughly what a recipe costs from the catalogue.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from sqlalchemy.orm import Session

from shopping.core import catalog
from shopping.notion.kitchen import Kitchen, norm


def week_start(today: date | None = None, weekday: int = 3) -> date:
    """The plan week starts on flyer day (Thursday by default)."""
    today = today or date.today()
    return today - timedelta(days=(today.weekday() - weekday) % 7)


def parse_manifest(text: str | None) -> tuple[int | None, list[dict]]:
    """`Serves: 6` then `Name | quantity [| optional]` lines (the Cooking database's Shopping Ingredients)."""
    serves, items = None, []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"(?i)^serves\s*:\s*(\d+)", line)
        if m:
            serves = int(m.group(1))
            continue
        parts = [p.strip() for p in line.split("|")]
        if parts[0]:
            items.append({"name": parts[0], "quantity": parts[1] if len(parts) > 1 else "",
                          "optional": len(parts) > 2 and parts[2].lower().startswith("optional")})
    return serves, items


def price_ingredient(session: Session, name: str) -> dict | None:
    """The product the household would most likely buy for an ingredient, with its current price."""
    rows = catalog.search_catalog(session, query=name, limit=30)
    rows = [r for r in rows if r.get("price") is not None and (r.get("aisle") or "") not in
            ("Pet Food & Supplies", "Baby", "Personal Care & Beauty", "Household Supplies")]
    if not rows:
        return None
    bought = [r for r in rows if r.get("bought_before")]
    if bought:
        return bought[0]
    on_sale = [r for r in rows if r.get("discount_pct")]
    pool = on_sale or rows
    return sorted(pool, key=lambda r: (r.get("unit_price") is None, r.get("unit_price") or 0))[0]


def cost_manifest(session: Session, manifest: str) -> dict:
    """Rough cost: one package of the likely product per ingredient (pantry staples on hand aren't excluded
    here; the caller decides). Good enough to compare meals and show cost per serving."""
    serves, items = parse_manifest(manifest)
    lines, total, missing = [], 0.0, []
    for it in items:
        p = price_ingredient(session, it["name"])
        if p is None:
            missing.append(it["name"])
            continue
        total += float(p["price"])
        lines.append({"ingredient": it["name"], "quantity": it["quantity"], "product": f"{p.get('brand') or ''} {p['name']}".strip(),
                      "product_id": p["product_id"], "price": p["price"], "on_sale": bool(p.get("discount_pct")),
                      "deal": p.get("deal"), "deal_ends": p.get("deal_ends")})
    return {"serves": serves, "estimated_total": round(total, 2),
            "per_serving": round(total / serves, 2) if serves else None, "lines": lines, "not_found": missing,
            "note": "Estimate: one package of the usual product per ingredient, current prices."}


DISCOVERY_REGULARS = 8  # fewer regular recipes than this: the household is still finding its regulars
MEAL_COURSES = {"Main", "Soup", "Salad", "Breakfast"}  # saved recipes worth planning a meal around (or no Course set)
GENERIC_MAIN = {"fruit", "vegetable", "vegetables", "veg", "fish", "meat", "seafood", "various", "mixed", "dairy",
                "grain", "grains", "bean", "beans", "herb", "herbs", "spice", "spices", "other", "none"}
STOP = {"and", "with", "the", "of", "in", "fresh", "frozen", "ground", "whole", "boneless", "skinless", "large", "small"}


def _words(text: str) -> set[str]:
    return {w for w in norm(text).split() if len(w) > 2 and w not in STOP}


async def candidates(session: Session, kitchen: Kitchen, *, expiring_days: int = 7, limit: int = 12) -> dict:
    """Recipes ranked for this week: regular recipes (scored on their Shopping Ingredients) and, while there are
    few regulars, saved Cooking recipes scored on their Main Ingredient. Both get points for deals they'd use and
    expiring pantry items they'd use up, and lose points if cooked recently or rejected."""
    expiring = await kitchen.expiring(expiring_days)
    expiring_names = {norm(r["Name"]): r for r in expiring}
    recent: dict[str, dict] = {}
    try:
        for m in await kitchen.meals(limit=200):
            if m.get("Name"):
                recent[norm(m["Name"])] = m
    except Exception:  # noqa: BLE001 - the Meals database may not exist yet
        pass
    cutoff = (date.today() - timedelta(days=21)).isoformat()
    price_cache: dict[str, dict | None] = {}

    def priced(name: str) -> dict | None:
        key = norm(name)
        if key not in price_cache:
            price_cache[key] = price_ingredient(session, name)
        return price_cache[key]

    def history(name: str) -> tuple[dict | None, bool, bool, int]:
        last = recent.get(norm(name or ""))
        cooked_recently = bool(last and last.get("Status") in ("Cooked", "Promoted") and (last.get("Week") or "") >= cutoff)
        rejected = bool(last and last.get("Status") == "Rejected")
        adj = -(15 if cooked_recently else 0) - (20 if rejected else 0) + (5 if last and last.get("Status") == "Promoted" else 0)
        return last, cooked_recently, rejected, adj

    def uses_up_for(names: list[str], *, loose: bool = False) -> list[dict]:
        """Expiring pantry items this dish uses: exact name matches, or (loose, for saved recipes) a shared
        key word, so main ingredient "Beef" or a recipe called "Beef Tacos" uses up "Ground Beef"."""
        out = []
        words = set().union(*(_words(n) for n in names)) if loose else set()
        for key, hit in expiring_names.items():
            exact = any(norm(n) == key for n in names)
            if (exact or (loose and _words(hit["Name"]) & words)) and all(u["name"] != hit["Name"] for u in out):
                out.append({"name": hit["Name"], "expires": hit["Expiration"], **({} if exact else {"match": "possible"})})
        return out

    def use_up_points(uses_up: list[dict]) -> int:
        return sum(4 if u.get("match") == "possible" else 10 for u in uses_up)

    def deal_for(name: str, *, strict: bool = False) -> dict | None:
        p = priced(name)
        if strict and p and not (_words(name) & _words(p["name"])):
            return None  # the product must actually be that ingredient (not just share an aisle)
        if p and p.get("discount_pct"):
            return {"ingredient": name, "product": p["name"], "price": p["price"], "discount_pct": p["discount_pct"], "ends": p.get("deal_ends")}
        return None

    regulars = await kitchen.cooking_search(regular_only=True, limit=100)
    out = []
    for r in regulars:
        serves, items = parse_manifest(r.get("Shopping Ingredients"))
        names = [it["name"] for it in items] or [r.get("Main Ingredient") or ""]
        uses_up = uses_up_for(names)
        on_sale = [d for d in (deal_for(n) for n in names if n) if d]
        last, cooked_recently, rejected, adj = history(r.get("Name"))
        out.append({"cooking_id": r["id"], "name": r.get("Name"), "kind": "regular", "tags": r.get("Tags"),
                    "effort": r.get("Effort"), "main_ingredient": r.get("Main Ingredient"), "serves": serves,
                    "has_manifest": bool(items), "uses_up": uses_up, "ingredients_on_sale": on_sale,
                    "cooked_recently": cooked_recently, "previously_rejected": rejected,
                    "last_feedback": (last or {}).get("Feedback"), "score": 10 * len(uses_up) + 4 * len(on_sale) + 3 + adj})

    discovery = len(regulars) < DISCOVERY_REGULARS
    saved = []
    if discovery:
        regular_ids = {r["id"] for r in regulars}
        for r in await kitchen.cooking_search(recipes_only=True, limit=400):
            main = (r.get("Main Ingredient") or "").strip()
            if r["id"] in regular_ids or not main or (r.get("Course") and r["Course"] not in MEAL_COURSES):
                continue
            uses_up = uses_up_for([main, r.get("Name") or ""], loose=True)
            deal = None if norm(main) in GENERIC_MAIN else deal_for(main, strict=True)
            last, cooked_recently, rejected, adj = history(r.get("Name"))
            score = use_up_points(uses_up) + (6 if deal else 0) + adj
            if score <= 0:
                continue  # nothing about this week favours it
            saved.append({"cooking_id": r["id"], "name": r.get("Name"), "kind": "saved_recipe", "tags": r.get("Tags"),
                          "effort": r.get("Effort"), "course": r.get("Course"), "cuisine": r.get("Cuisine"), "main_ingredient": main,
                          "uses_up": uses_up, "main_ingredient_deal": deal, "cooked_recently": cooked_recently,
                          "previously_rejected": rejected, "last_feedback": (last or {}).get("Feedback"), "score": score})
        saved.sort(key=lambda c: -c["score"])
    out.sort(key=lambda c: -c["score"])
    return {
        "discovery_mode": discovery,
        "note": ("Few regular recipes yet: the household is still finding its regulars. Draw on saved_recipes (their Cooking "
                 "pages have the method; read one with notion_get_page) and new dishes built around the best deals; "
                 "after a meal is cooked and liked, offer to promote it and, if it keeps coming back, to make it a regular. "
                 "A uses_up entry marked match=possible only shares a word (e.g. beef): judge whether the dish really uses it.")
                if discovery else None,
        "regular_recipes": out[:limit],
        "saved_recipes": saved[:limit],
        "expiring_on_hand": [{"name": e["Name"], "expires": e["Expiration"], "where": f"{e.get('Location') or ''} / {e.get('Sub-Location') or ''}"}
                             for e in expiring],
    }


def recent_meal_history(meals: list[dict], *, weeks: int = 6) -> dict:
    """Outcomes of recent suggestions: what got cooked, rated, rejected or skipped, in people's words."""
    cutoff = (date.today() - timedelta(weeks=weeks)).isoformat()
    recent = [m for m in meals if (m.get("Week") or m.get("Cook By") or "") >= cutoff]
    pick = lambda status: [{k: m.get(k) for k in ("Name", "Feedback", "Effort", "Tags", "Week") if m.get(k)}  # noqa: E731
                           for m in recent if m.get("Status") == status]
    return {"cooked": pick("Cooked") + pick("Promoted"), "rejected": pick("Rejected"), "skipped": pick("Skipped"),
            "planned_now": pick("Planned"), "suggested_now": pick("Suggested")}
