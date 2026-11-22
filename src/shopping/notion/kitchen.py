"""The household's Notion databases, with meal-planner's rules built in.

- Pantry ("Kitchen Pantry and Storage"): one row per ingredient; Expiration only on perishables.
- Groceries: the shopping list; one active (Status New) row per ingredient, linked to its pantry row.
- Cooking: recipes; `Regular Recipe?` and `Shopping Ingredients` drive regular shopping.
- Suggested Meals: the agent's proposals (docs/notion-suggested-meals.md).

Property names are looked up in each data source's schema (cached); values for properties a database
doesn't have are skipped and reported, so a slightly different Meals schema still works.
"""

from __future__ import annotations

import re
import time
from contextlib import asynccontextmanager
from datetime import date, timedelta

from shopping import appsettings, context
from shopping.database import session_scope
from shopping.notion import props
from shopping.notion.blocks import blocks_to_text, text_to_blocks
from shopping.notion.client import NotionClient, NotionError, clean_id

SCHEMA_TTL = 600
ROWS_TTL = 120
_schema_cache: dict[str, tuple[float, dict]] = {}
_rows_cache: dict[str, tuple[float, list]] = {}
_resolved: dict[str, str] = {}  # configured id → data source id

PANTRY_FIELDS = ("Name", "Type", "Location", "Sub-Location", "Replace?", "Regular Purchase?", "Shopping Preferences",
                 "Store Preference", "Expiration")
PANTRY_WRITABLE = {"Type", "Location", "Sub-Location", "Replace?", "Regular Purchase?", "Shopping Preferences",
                   "Store Preference", "Expiration"}
MEAL_FIELDS = ("Name", "Status", "Week", "Cook By", "Course", "Cuisine", "Main Ingredient", "Tags", "Effort", "Servings",
               "Uses Up", "Shopping Ingredients", "Based On", "Grocery Run", "Source", "Feedback", "Promote", "Promoted To")
COOKING_COPY = ("Course", "Cuisine", "Main Ingredient", "Tags", "Effort", "Shopping Ingredients")


def norm(name: str) -> str:
    """Name matching: case, markup, punctuation and simple plurals ignored."""
    n = re.sub(r"<[^>]+>", "", name or "").lower()
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    words = [w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in n.split()]
    return " ".join(words)


def invalidate(ds_id: str | None = None) -> None:
    if ds_id is None:
        _rows_cache.clear()
    else:
        _rows_cache.pop(clean_id(ds_id), None)


class Kitchen:
    def __init__(self, client: NotionClient, ids: dict):
        self.n = client
        self.ids = {k: (clean_id(v) if v else "") for k, v in ids.items()}

    def _ds(self, which: str) -> str:
        ds = self.ids.get(which)
        if not ds:
            label = {"meals": "Suggested Meals"}.get(which, which.title())
            raise NotionError(f"The {label} database isn't configured. Add its data source id in Settings → Notion.")
        return ds

    async def _resolve(self, which: str) -> str:
        """Settings may hold a database id (what "Copy link" gives) instead of its data source id; look it up once."""
        ds = self._ds(which)
        if ds in _resolved:
            self.ids[which] = _resolved[ds]
            return _resolved[ds]
        try:
            await self.n.get_data_source(ds)
            _resolved[ds] = ds
        except NotionError as exc:
            if exc.status != 404:
                raise
            try:
                db = await self.n.get_database(ds)
            except NotionError:
                raise exc from None
            sources = db.get("data_sources") or []
            if not sources:
                raise exc
            _resolved[ds] = clean_id(sources[0]["id"])
        self.ids[which] = _resolved[ds]
        return _resolved[ds]

    async def schema(self, which: str) -> dict:
        ds = await self._resolve(which)
        hit = _schema_cache.get(ds)
        if hit and time.time() - hit[0] < SCHEMA_TTL:
            return hit[1]
        data = await self.n.get_data_source(ds)
        schema = data.get("properties") or {}
        _schema_cache[ds] = (time.time(), schema)
        return schema

    async def rows(self, which: str, *, filter: dict | None = None, sorts: list | None = None, cache: bool = True, limit: int = 500) -> list[dict]:
        ds = await self._resolve(which)
        key = ds if filter is None and sorts is None else None
        if cache and key:
            hit = _rows_cache.get(key)
            if hit and time.time() - hit[0] < ROWS_TTL:
                return hit[1]
        out = [props.row(p) for p in await self.n.query(ds, filter=filter, sorts=sorts, limit=limit)]
        if key:
            _rows_cache[key] = (time.time(), out)
        return out

    async def _write_values(self, which: str, values: dict) -> tuple[dict, list[str]]:
        return props.write(await self.schema(which), values)

    async def _relation_to(self, which: str, target: str) -> str | None:
        """Name of the relation property in `which` that points at the `target` database."""
        target_ds = self.ids.get(target)
        for name, p in (await self.schema(which)).items():
            if p.get("type") == "relation":
                rel = p.get("relation") or {}
                if target_ds and clean_id(rel.get("data_source_id") or rel.get("database_id") or "0" * 32) == target_ds:
                    return name
        for name, p in (await self.schema(which)).items():  # fall back on the name
            if p.get("type") == "relation" and target.lower() in name.lower():
                return name
        return None

    # -- pantry --------------------------------------------------------------------------
    async def pantry(self, *, cache: bool = True) -> list[dict]:
        rows = await self.rows("pantry", cache=cache)
        out = []
        for r in rows:
            name = re.sub(r"<[^>]+>", "", r.get("Name") or "").strip()
            if not name:
                continue
            out.append({"id": r["id"], **{k: r.get(k) for k in PANTRY_FIELDS}, "Name": name})
        return out

    async def find_pantry(self, name: str) -> dict | None:
        key = norm(name)
        rows = await self.pantry()
        return next((r for r in rows if norm(r["Name"]) == key), None)

    async def expiring(self, days: int = 7) -> list[dict]:
        limit = (date.today() + timedelta(days=days)).isoformat()
        return sorted([r for r in await self.pantry() if r.get("Expiration") and r["Expiration"][:10] <= limit],
                      key=lambda r: r["Expiration"])

    async def pantry_update(self, page_id: str, values: dict) -> dict:
        bad = set(values) - PANTRY_WRITABLE
        if bad:
            raise NotionError(f"Pantry fields not writable here: {', '.join(sorted(bad))}.")
        payload, skipped = await self._write_values("pantry", values)
        await self.n.update_page(page_id, payload)
        invalidate(self.ids["pantry"])
        return {"updated": page_id, "skipped": skipped}

    async def pantry_create(self, name: str, values: dict) -> dict:
        existing = await self.find_pantry(name)
        if existing:
            raise NotionError(f"A pantry row for {existing['Name']!r} already exists; update it instead (id {existing['id']}).")
        payload, skipped = await self._write_values("pantry", {"Name": name.strip().title(), **values})
        page = await self.n.create_page(self.ids["pantry"], payload)
        invalidate(self.ids["pantry"])
        return {"created": page["id"], "skipped": skipped}

    # -- groceries -----------------------------------------------------------------------
    async def groceries(self, *, active_only: bool = True) -> list[dict]:
        rel = await self._relation_to("groceries", "pantry")
        filt = {"property": "Status", "status": {"equals": "New"}} if active_only else None
        schema = await self.schema("groceries")
        if active_only and (schema.get("Status") or {}).get("type") == "select":
            filt = {"property": "Status", "select": {"equals": "New"}}
        rows = await self.rows("groceries", filter=filt, cache=False)
        return [{"id": r["id"], "Name": r.get("Name"), "Quantity": r.get("Quantity"), "Status": r.get("Status"),
                 "Purchase Date": r.get("Purchase Date"), "pantry_ids": r.get(rel) if rel else []} for r in rows]

    async def grocery_upsert(self, name: str, quantity: str | None = None) -> dict:
        """Add to the list, or update the active row's Quantity (never a duplicate). Links the pantry row."""
        key = norm(name)
        pantry = await self.find_pantry(name)
        for r in await self.groceries():
            if norm(r["Name"] or "") == key or (pantry and pantry["id"] in (r["pantry_ids"] or [])):
                if quantity:
                    payload, _ = await self._write_values("groceries", {"Quantity": quantity})
                    await self.n.update_page(r["id"], payload)
                return {"updated": r["id"], "name": r["Name"], "quantity": quantity or r["Quantity"]}
        values = {"Name": pantry["Name"] if pantry else name.strip(), "Quantity": quantity or "", "Status": "New"}
        rel = await self._relation_to("groceries", "pantry")
        if pantry and rel:
            values[rel] = [pantry["id"]]
        payload, skipped = await self._write_values("groceries", values)
        page = await self.n.create_page(self.ids["groceries"], payload)
        return {"created": page["id"], "name": values["Name"], "linked_pantry": bool(pantry and rel), "skipped": skipped}

    async def grocery_set_status(self, page_id: str, status: str, purchase_date: str | None = None) -> dict:
        if status not in ("New", "Skipped", "Purchased"):
            raise NotionError("Status must be New, Skipped or Purchased.")
        values = {"Status": status}
        if status == "Purchased":
            values["Purchase Date"] = purchase_date or date.today().isoformat()
        payload, _ = await self._write_values("groceries", values)
        await self.n.update_page(page_id, payload)
        return {"updated": page_id, "status": status}

    # -- cooking ---------------------------------------------------------------------------
    async def cooking_search(self, query: str = "", *, regular_only: bool = False, recipes_only: bool = False,
                             limit: int = 25) -> list[dict]:
        filters = []
        if query:
            filters.append({"property": "Name", "title": {"contains": query}})
        if regular_only:
            filters.append({"property": "Regular Recipe?", "checkbox": {"equals": True}})
        if recipes_only:
            filters.append({"property": "Type", "select": {"equals": "Recipe"}})
        filt = {"and": filters} if len(filters) > 1 else (filters[0] if filters else None)
        rows = await self.rows("cooking", filter=filt, cache=False, limit=limit)
        keep = ("Name", "Type", "Course", "Cuisine", "Main Ingredient", "Tags", "Regular Recipe?", "Shopping Ingredients", "Reference")
        return [{"id": r["id"], "url": r.get("url"), **{k: r.get(k) for k in keep if k in r}} for r in rows]

    async def page(self, page_id: str) -> dict:
        p = await self.n.get_page(page_id)
        body = blocks_to_text(await self.n.get_blocks(page_id))
        return {**props.row(p), "body": body}

    async def cooking_create(self, name: str, values: dict, body: str = "") -> dict:
        payload, skipped = await self._write_values("cooking", {"Name": name, **values})
        page = await self.n.create_page(self.ids["cooking"], payload, text_to_blocks(body))
        return {"created": page["id"], "url": page.get("url"), "skipped": skipped}

    # -- suggested meals ---------------------------------------------------------------------
    async def meals(self, *, week: str | None = None, statuses: list[str] | None = None, limit: int = 100) -> list[dict]:
        schema = await self.schema("meals")
        filters = []
        if week and "Week" in schema:
            filters.append({"property": "Week", "date": {"equals": week}})
        if statuses and "Status" in schema:
            kind = (schema["Status"] or {}).get("type", "status")
            filters.append({"or": [{"property": "Status", kind: {"equals": s}} for s in statuses]})
        filt = {"and": filters} if len(filters) > 1 else (filters[0] if filters else None)
        sorts = [{"property": "Cook By", "direction": "ascending"}] if "Cook By" in schema else None
        rows = await self.rows("meals", filter=filt, sorts=sorts, cache=False, limit=limit)
        return [{"id": r["id"], "url": r.get("url"), **{k: r.get(k) for k in MEAL_FIELDS if k in r}} for r in rows]

    async def meal_create(self, values: dict, body: str = "") -> dict:
        payload, skipped = await self._write_values("meals", values)
        page = await self.n.create_page(self.ids["meals"], payload, text_to_blocks(body))
        return {"created": page["id"], "url": page.get("url"), "skipped": skipped}

    async def meal_update(self, page_id: str, values: dict) -> dict:
        payload, skipped = await self._write_values("meals", values)
        await self.n.update_page(page_id, payload)
        return {"updated": page_id, "skipped": skipped}

    async def promote_meal(self, page_id: str) -> dict:
        """Copy a suggested meal into Cooking (fields + page body), link it, mark it Promoted."""
        meal = await self.page(page_id)
        if meal.get("Promoted To"):
            return {"already_promoted": meal["Promoted To"]}
        cooking_schema = await self.schema("cooking")
        values = {k: meal.get(k) for k in COOKING_COPY if meal.get(k) not in (None, "", []) and k in cooking_schema}
        if "Type" in cooking_schema:
            values["Type"] = "Recipe"
        created = await self.cooking_create(meal.get("Name") or "Untitled", values, meal.get("body") or "")
        # Meals has two relations to Cooking (Based On, Promoted To), so name the one we mean.
        await self.meal_update(page_id, {"Status": "Promoted", "Promote": False, "Promoted To": [created["created"]]})
        return {"cooking_page": created["created"], "url": created.get("url")}


@asynccontextmanager
async def open_kitchen():
    """A Kitchen using the Notion token from the vault and database ids from settings."""
    with session_scope() as session:
        token = context.vault.get(session, "notion")
        ids = appsettings.get(session, "notion")
    if not token:
        raise NotionError("No Notion token set. Add the integration token in Settings → Notion.")
    client = NotionClient(token)
    try:
        yield Kitchen(client, ids)
    finally:
        await client.aclose()
