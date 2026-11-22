"""Agent tools for Notion (pantry, grocery list, Cooking, Suggested Meals) and meal planning.

Reads are free; creating/updating Suggested Meals is "safe" (it's the agent's own proposal list); anything
that changes the pantry, the grocery list or Cooking needs a person's confirmation.
"""

from __future__ import annotations

from pathlib import Path

from shopping import appsettings
from shopping.agent.tools import CONFIRM, READ, SAFE, Tool, ToolContext, registry
from shopping.core import catalog
from shopping.database import session_scope
from shopping.meals import planner
from shopping.notion.kitchen import open_kitchen

REFERENCES = Path(__file__).parent / "references"

# Fields the agent may set on a Suggested Meal. Feedback and Promote are people's.
MEAL_AGENT_FIELDS = {"Name", "Status", "Week", "Cook By", "Course", "Cuisine", "Main Ingredient", "Tags", "Effort", "Servings",
                     "Uses Up", "Shopping Ingredients", "Based On", "Grocery Run", "Source"}
AGENT_MEAL_STATUSES = {"Suggested", "Skipped"}  # the agent never marks something cooked, planned or promoted


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


S = {"type": "string"}
N = {"type": "number"}
B = {"type": "boolean"}


def _str_list(desc: str) -> dict:
    return {"type": "array", "items": {"type": "string"}, "description": desc}


# -- reads ------------------------------------------------------------------------------------

async def _pantry(args: dict, ctx: ToolContext):
    async with open_kitchen() as k:
        rows = await k.pantry()
    q = (args.get("query") or "").lower()
    loc = (args.get("location") or "").lower()
    days = args.get("expiring_within_days")
    if q:
        rows = [r for r in rows if q in r["Name"].lower()]
    if loc:
        rows = [r for r in rows if loc in f"{r.get('Location') or ''} {r.get('Sub-Location') or ''}".lower()]
    if args.get("regular_only"):
        rows = [r for r in rows if r.get("Regular Purchase?")]
    if days is not None:
        from datetime import date, timedelta
        limit = (date.today() + timedelta(days=int(days))).isoformat()
        rows = [r for r in rows if r.get("Expiration") and r["Expiration"][:10] <= limit]
    return {"count": len(rows), "rows": rows[:200]}


async def _grocery_list(args: dict, ctx: ToolContext):
    async with open_kitchen() as k:
        return {"active": await k.groceries(active_only=True)}


async def _cooking_search(args: dict, ctx: ToolContext):
    async with open_kitchen() as k:
        return {"results": await k.cooking_search(args.get("query") or "", regular_only=bool(args.get("regular_only")),
                                                  limit=int(args.get("limit") or 25))}


async def _get_page(args: dict, ctx: ToolContext):
    async with open_kitchen() as k:
        p = await k.page(args["page_id"])
    if len(p.get("body") or "") > 8000:
        p["body"] = p["body"][:8000] + "\n…[truncated]"
    return p


async def _meals(args: dict, ctx: ToolContext):
    async with open_kitchen() as k:
        return {"meals": await k.meals(week=args.get("week"), statuses=args.get("statuses") or None)}


async def _meal_context(args: dict, ctx: ToolContext):
    """Everything the meal playbook needs in one call (deterministic)."""
    with session_scope() as session:
        jobs = appsettings.get(session, "jobs")
        deals = catalog.shopping_context(session, focus="meals", per_aisle=int(args.get("per_group") or 6))
    week = planner.week_start(weekday=int(jobs["weekly_plan_weekday"]))
    out = {"week_start": week.isoformat(), "meals_per_week": jobs["meals_per_week"], "deals": deals}
    try:
        async with open_kitchen() as k:
            with session_scope() as session:
                out.update(await planner.candidates(session, k, expiring_days=int(args.get("expiring_days") or 7)))
            try:
                out["meal_history"] = planner.recent_meal_history(await k.meals(limit=150))
            except Exception as exc:  # noqa: BLE001
                out["meal_history"] = {"note": f"Suggested Meals not available: {exc}"}
    except Exception as exc:  # noqa: BLE001
        out["notion"] = f"Notion not available ({exc}); plan from deals only and say so."
    return out


async def _meal_cost(args: dict, ctx: ToolContext):
    """One recipe (shopping_ingredients) or several at once (recipes: [{name, shopping_ingredients}])."""
    with session_scope() as session:
        if args.get("recipes"):
            return {"costs": [{"name": r.get("name"), **planner.cost_manifest(session, r.get("shopping_ingredients") or "")}
                              for r in args["recipes"][:12]]}
        return planner.cost_manifest(session, args["shopping_ingredients"])


async def _reference(args: dict, ctx: ToolContext):
    name = args.get("name")
    files = {p.stem: p for p in REFERENCES.glob("*.md")}
    if name not in files:
        return {"available": sorted(files)}
    return files[name].read_text()


# -- safe writes: the agent's own suggestions ---------------------------------------------------

def _meal_values(args: dict) -> dict:
    values = {k: v for k, v in (args.get("fields") or {}).items() if k in MEAL_AGENT_FIELDS}
    if "Status" in values and values["Status"] not in AGENT_MEAL_STATUSES:
        raise ValueError(f"The agent can only set Status to {sorted(AGENT_MEAL_STATUSES)}; people set Planned/Cooked/Rejected.")
    return values


async def _meal_create(args: dict, ctx: ToolContext):
    """One meal (name, fields, body) or several at once (meals: [{name, fields, body}])."""
    meals = args.get("meals") or [args]
    out = []
    async with open_kitchen() as k:
        for m in meals[:10]:
            try:
                values = _meal_values(m)
                values["Name"] = m["name"]
                values.setdefault("Status", "Suggested")
                values.setdefault("Source", "Weekly Plan" if ctx.proactive else "Chat")
                out.append({"name": m["name"], **(await k.meal_create(values, m.get("body") or ""))})
            except (ValueError, KeyError) as exc:
                out.append({"name": m.get("name"), "error": str(exc)})
    return out[0] if len(out) == 1 and not args.get("meals") else {"meals": out}


async def _meal_update(args: dict, ctx: ToolContext):
    values = _meal_values(args)
    if not values:
        raise ValueError(f"Nothing to update; allowed fields: {sorted(MEAL_AGENT_FIELDS)}.")
    async with open_kitchen() as k:
        return await k.meal_update(args["page_id"], values)


# -- confirm-tier writes ---------------------------------------------------------------------

async def _grocery_add(args: dict, ctx: ToolContext):
    out = []
    async with open_kitchen() as k:
        for it in args["items"][:40]:
            out.append(await k.grocery_upsert(it["name"], it.get("quantity")))
    return {"results": out}


async def _grocery_status(args: dict, ctx: ToolContext):
    out = []
    async with open_kitchen() as k:
        for it in args["items"][:60]:
            out.append(await k.grocery_set_status(it["id"], it["status"], it.get("purchase_date")))
    return {"results": out}


async def _pantry_update(args: dict, ctx: ToolContext):
    out = []
    async with open_kitchen() as k:
        for it in args["items"][:40]:
            out.append(await k.pantry_update(it["page_id"], it["values"]))
    return {"results": out}


async def _pantry_create(args: dict, ctx: ToolContext):
    async with open_kitchen() as k:
        return await k.pantry_create(args["name"], args.get("values") or {})


async def _cooking_create(args: dict, ctx: ToolContext):
    async with open_kitchen() as k:
        return await k.cooking_create(args["name"], args.get("fields") or {}, args.get("body") or "")


async def _meal_promote(args: dict, ctx: ToolContext):
    async with open_kitchen() as k:
        return await k.promote_meal(args["page_id"])


def _lines(items, fmt) -> str:
    return "\n".join(fmt(i) for i in items[:40])


def register_kitchen_tools() -> None:
    add = registry.add
    add(Tool("notion_pantry", "Kitchen inventory rows (Name, Type, Location, Sub-Location, Replace?, Regular Purchase?, "
             "Shopping Preferences, Store Preference, Expiration). Filter by name text, location, regulars, or expiring soon.",
             _obj({"query": S, "location": S, "regular_only": B, "expiring_within_days": N}), READ, _pantry))
    add(Tool("notion_grocery_list", "The active grocery list (Groceries rows with Status New): Name, Quantity, linked pantry ids.",
             _obj({}), READ, _grocery_list))
    add(Tool("notion_cooking_search", "Search the Cooking database (saved recipes and notes) by name; regular_only for "
             "Regular Recipe? rows (which carry Shopping Ingredients).", _obj({"query": S, "regular_only": B, "limit": N}), READ, _cooking_search))
    add(Tool("notion_get_page", "Read one Notion page: its properties and body text (recipe, notes).", _obj({"page_id": S}, ["page_id"]),
             READ, _get_page))
    add(Tool("notion_meals", "Suggested Meals rows, optionally for one plan week (YYYY-MM-DD, the Thursday) or statuses "
             "(Suggested, Planned, Cooked, Promoted, Rejected, Skipped).",
             _obj({"week": S, "statuses": _str_list("statuses to include")}), READ, _meals))
    add(Tool("meals_context", "Start here for meal planning: this week's start date, meal-relevant deals (protein, produce…), "
             "on-hand items expiring soon, regular recipes scored against deals and expiring items, and how recent "
             "suggestions turned out (cooked, rated, rejected, skipped).", _obj({"expiring_days": N, "per_group": N}), READ, _meal_context))
    add(Tool("meal_cost", "Estimate recipe costs from the store catalogue. Pass `recipes` (several at once, preferred) or one "
             "`shopping_ingredients` text (`Serves: N` then `Name | quantity` lines). Returns total, per serving, and which "
             "ingredients are on sale.",
             _obj({"recipes": {"type": "array", "items": {"type": "object", "properties": {"name": S, "shopping_ingredients": S},
                                                         "required": ["shopping_ingredients"]}},
                   "shopping_ingredients": S}), READ, _meal_cost))
    add(Tool("get_reference", "Read a reference note: 'shelf-life' (expiry estimates by food and storage) or 'receipts' "
             "(decoding grocery receipt lines).", _obj({"name": S}, ["name"]), READ, _reference))

    meal_fields = {"type": "object", "description": "Suggested Meals properties: Week, Cook By (YYYY-MM-DD), Course (Main, Side, Soup, "
                   "Salad, Breakfast…), Cuisine, Main Ingredient, Tags (list: Bulk, Braise, Pressure Cooker, Quick, Sheet Pan, One Pot, "
                   "Freezer-Friendly, New…), Effort (Easy, Moderate or Gourmet), Servings, Uses Up (list of pantry page ids), "
                   "Shopping Ingredients, Based On (list of Cooking page ids), Grocery Run (link)", "properties": {}}
    meal_item = {"type": "object", "properties": {"name": S, "fields": meal_fields, "body": S}, "required": ["name", "fields", "body"]}
    add(Tool("meal_suggest", "Create Suggested Meals in Notion (Status Suggested). Pass `meals` to save several at once "
             "(preferred: all of the week's meals in one call), or a single name/fields/body. Put the recipe in `body` using the "
             "Cooking page style: a 📌 summary line (why this week: the deals it uses with price and end date, what it uses up, "
             "active/total time, estimated cost per serving), ## Ingredients (imperial and metric), ## Process (numbered, bold "
             "stage labels and times/temps), ## Leftovers & storage, ## Notes.",
             _obj({"meals": {"type": "array", "items": meal_item}, "name": S, "fields": meal_fields, "body": S}), SAFE, _meal_create))
    add(Tool("meal_update", "Update the agent-owned fields of a Suggested Meal (not Feedback or Promote).",
             _obj({"page_id": S, "fields": meal_fields}, ["page_id", "fields"]), SAFE, _meal_update))

    item = {"type": "object", "properties": {"name": S, "quantity": S}, "required": ["name"]}
    add(Tool("grocery_list_add", "Add items to the Notion grocery list (or update an active row's Quantity; never duplicates; "
             "links the pantry row). Quantities purchasable, e.g. '3 lb (1.4 kg)', '1 bag', '2'.",
             _obj({"items": {"type": "array", "items": item}}, ["items"]), CONFIRM, _grocery_add,
             preview=lambda a: "Grocery list:\n" + _lines(a.get("items") or [], lambda i: f"+ {i.get('name')} {i.get('quantity') or ''}".rstrip())))
    status_item = {"type": "object", "properties": {"id": S, "status": {"type": "string", "enum": ["New", "Skipped", "Purchased"]},
                                                    "purchase_date": S, "name": S}, "required": ["id", "status"]}
    add(Tool("grocery_list_set_status", "Mark grocery list rows Purchased (with date), Skipped, or back to New.",
             _obj({"items": {"type": "array", "items": status_item}}, ["items"]), CONFIRM, _grocery_status,
             preview=lambda a: "Grocery list:\n" + _lines(a.get("items") or [], lambda i: f"{i.get('name') or i.get('id')} → {i.get('status')}")))
    pantry_item = {"type": "object", "properties": {"page_id": S, "name": S, "values": {
        "type": "object", "description": "Any of: Expiration (YYYY-MM-DD or empty to clear), Shopping Preferences, Store Preference, "
        "Location, Sub-Location, Type, Replace?, Regular Purchase?", "properties": {}}}, "required": ["page_id", "values"]}
    add(Tool("pantry_update", "Update kitchen inventory rows (expiry dates, shopping preferences, location…).",
             _obj({"items": {"type": "array", "items": pantry_item}}, ["items"]), CONFIRM, _pantry_update,
             preview=lambda a: "Pantry:\n" + _lines(a.get("items") or [], lambda i: f"{i.get('name') or i.get('page_id')}: "
                                                    + ", ".join(f"{k} = {v or '(clear)'}" for k, v in (i.get('values') or {}).items()))))
    add(Tool("pantry_create", "Create a kitchen inventory row (generic Title Case ingredient name; check notion_pantry first).",
             _obj({"name": S, "values": {"type": "object", "properties": {}}}, ["name"]), CONFIRM, _pantry_create,
             preview=lambda a: f"New pantry row: {a.get('name')} " + ", ".join(f"{k}={v}" for k, v in (a.get('values') or {}).items())))
    add(Tool("cooking_save_recipe", "Save a recipe to the Cooking database (fields: Type, Course, Cuisine, Main Ingredient, Tags, "
             "Reference, Shopping Ingredients; body in the Cooking page style).",
             _obj({"name": S, "fields": {"type": "object", "properties": {}}, "body": S}, ["name", "body"]), CONFIRM, _cooking_create,
             preview=lambda a: f"Save to Cooking: {a.get('name')}"))
    add(Tool("meal_promote", "Promote a Suggested Meal to the Cooking database (copies fields and recipe, links it, marks it Promoted).",
             _obj({"page_id": S, "name": S}, ["page_id"]), CONFIRM, _meal_promote,
             preview=lambda a: f"Promote to Cooking: {a.get('name') or a.get('page_id')}"))
