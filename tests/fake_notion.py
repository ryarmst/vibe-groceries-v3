"""A tiny in-memory stand-in for the Notion API (just what Kitchen uses)."""

import json
import uuid

import httpx

PANTRY, GROCERIES, COOKING, MEALS = (str(uuid.UUID(int=i)) for i in (1, 2, 3, 4))


def _opts(*names):
    return {"options": [{"name": n} for n in names]}


SCHEMAS = {
    PANTRY: {"Name": {"type": "title"}, "Type": {"type": "select", "select": _opts("Staple", "Ingredient")},
             "Location": {"type": "select"}, "Sub-Location": {"type": "select"}, "Replace?": {"type": "select"},
             "Regular Purchase?": {"type": "checkbox"}, "Shopping Preferences": {"type": "rich_text"},
             "Store Preference": {"type": "select"}, "Expiration": {"type": "date"}},
    GROCERIES: {"Name": {"type": "title"}, "Quantity": {"type": "rich_text"}, "Status": {"type": "status", "status": _opts("New", "Skipped", "Purchased")},
                "Purchase Date": {"type": "date"}, "🥕 Kitchen Pantry and Storage": {"type": "relation", "relation": {"data_source_id": PANTRY}}},
    COOKING: {"Name": {"type": "title"}, "Type": {"type": "select"}, "Course": {"type": "select"}, "Cuisine": {"type": "rich_text"},
              "Effort": {"type": "select"},
              "Main Ingredient": {"type": "rich_text"}, "Tags": {"type": "multi_select"}, "Regular Recipe?": {"type": "checkbox"},
              "Shopping Ingredients": {"type": "rich_text"}},
    MEALS: {"Name": {"type": "title"}, "Status": {"type": "status", "status": _opts("Suggested", "Planned", "Cooked", "Promoted", "Rejected", "Skipped")},
            "Week": {"type": "date"}, "Cook By": {"type": "date"}, "Course": {"type": "select"}, "Tags": {"type": "multi_select"},
            "Cuisine": {"type": "multi_select"},
            "Shopping Ingredients": {"type": "rich_text"}, "Effort": {"type": "select"}, "Feedback": {"type": "rich_text"},
            "Promote": {"type": "checkbox"}, "Promoted To": {"type": "relation", "relation": {"data_source_id": COOKING}},
            "Based On": {"type": "relation", "relation": {"data_source_id": COOKING}}},
}


class FakeNotion:
    def __init__(self):
        self.pages: dict[str, dict] = {}
        self.blocks: dict[str, list] = {}
        self.calls = []
        self.databases: dict[str, str] = {}  # database id -> data source id

    def add(self, ds: str, values: dict, body: list | None = None) -> str:
        pid = str(uuid.uuid4())
        props = {}
        for name, v in values.items():
            t = SCHEMAS[ds][name]["type"]
            props[name] = {"type": t, t: v}
        self.pages[pid] = {"id": pid, "url": f"https://notion.so/{pid}", "ds": ds, "properties": props}
        self.blocks[pid] = body or []
        return pid

    @staticmethod
    def _value(prop):
        t = prop["type"]
        v = prop.get(t)
        if t in ("title", "rich_text"):
            return "".join(x.get("text", {}).get("content", "") for x in v or [])
        if t in ("select", "status"):
            return (v or {}).get("name")
        if t == "checkbox":
            return v
        return v

    def _match(self, page, f):
        if not f:
            return True
        if "and" in f:
            return all(self._match(page, x) for x in f["and"])
        if "or" in f:
            return any(self._match(page, x) for x in f["or"])
        prop = page["properties"].get(f["property"])
        if prop is None:
            return False
        cond = next(v for k, v in f.items() if k != "property")
        val = self._value(prop)
        if "equals" in cond:
            return val == cond["equals"]
        if "contains" in cond:
            return cond["contains"].lower() in (val or "").lower()
        return True

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path.removeprefix("/v1"), request.method
        body = json.loads(request.content) if request.content else {}
        self.calls.append((method, path, body))
        parts = path.strip("/").split("/")
        if parts[0] == "data_sources" and method == "GET":
            if parts[1] not in SCHEMAS:
                return httpx.Response(404, json={"message": f"Could not find data_source with ID: {parts[1]}"})
            return httpx.Response(200, json={"properties": SCHEMAS[parts[1]]})
        if parts[0] == "databases" and method == "GET":
            ds = self.databases.get(parts[1])
            if ds is None:
                return httpx.Response(404, json={"message": "no database"})
            return httpx.Response(200, json={"id": parts[1], "data_sources": [{"id": ds, "name": "x"}]})
        if parts[0] == "data_sources" and parts[-1] == "query":
            rows = [p for p in self.pages.values() if p["ds"] == parts[1] and self._match(p, body.get("filter"))]
            return httpx.Response(200, json={"results": rows, "has_more": False})
        if parts == ["pages"] and method == "POST":
            ds = body["parent"]["data_source_id"]
            pid = str(uuid.uuid4())
            self.pages[pid] = {"id": pid, "url": f"https://notion.so/{pid}", "ds": ds,
                               "properties": {k: {"type": next(iter(v)), **v} for k, v in body["properties"].items()}}
            self.blocks[pid] = body.get("children") or []
            return httpx.Response(200, json=self.pages[pid])
        if parts[0] == "pages" and method == "PATCH":
            page = self.pages[parts[1]]
            for k, v in body["properties"].items():
                page["properties"][k] = {"type": next(iter(v)), **v}
            return httpx.Response(200, json=page)
        if parts[0] == "pages" and method == "GET":
            return httpx.Response(200, json=self.pages[parts[1]])
        if parts[0] == "blocks" and method == "GET":
            return httpx.Response(200, json={"results": self.blocks.get(parts[1], []), "has_more": False})
        if parts[0] == "blocks" and method == "PATCH":
            self.blocks.setdefault(parts[1], []).extend(body["children"])
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"message": f"no route {method} {path}"})
