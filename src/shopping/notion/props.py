"""Notion property values ⇄ plain Python values, guided by the data source schema."""

from __future__ import annotations

from datetime import date, datetime


def plain_text(rich: list | None) -> str:
    return "".join((r.get("plain_text") or (r.get("text") or {}).get("content") or "") for r in rich or [])


def read(prop: dict):
    """One property value → a plain value (str, number, bool, list, date string, or None)."""
    t = prop.get("type")
    v = prop.get(t)
    if t in ("title", "rich_text"):
        return plain_text(v)
    if t in ("select", "status"):
        return (v or {}).get("name")
    if t == "multi_select":
        return [o.get("name") for o in v or []]
    if t == "date":
        return (v or {}).get("start")
    if t == "relation":
        return [r.get("id") for r in v or []]
    if t == "formula":
        return (v or {}).get((v or {}).get("type"))
    if t == "rollup":
        return (v or {}).get((v or {}).get("type"))
    if t in ("checkbox", "number", "url", "email", "phone_number", "created_time", "last_edited_time"):
        return v
    if t in ("created_by", "last_edited_by"):
        return (v or {}).get("name") or (v or {}).get("id")
    if t == "people":
        return [p.get("name") or p.get("id") for p in v or []]
    if t == "unique_id":
        return f"{(v or {}).get('prefix') or ''}{(v or {}).get('number')}"
    return None


def row(page: dict) -> dict:
    """A page (database row) → {"id", "url", <property name>: value…}."""
    out = {"id": page.get("id"), "url": page.get("url")}
    for name, prop in (page.get("properties") or {}).items():
        out[name] = read(prop)
    return out


def _rich(text: str) -> list[dict]:
    text = text or ""
    return [{"type": "text", "text": {"content": text[i:i + 2000]}} for i in range(0, len(text), 2000)] or []


def write(schema: dict, values: dict) -> tuple[dict, list[str]]:
    """Plain values → Notion property payloads for properties that exist in `schema` (a data source's
    "properties"). Returns (payload, skipped names) so callers can report what the database doesn't have."""
    payload, skipped = {}, []
    for name, value in values.items():
        prop = schema.get(name)
        if prop is None:
            skipped.append(name)
            continue
        t = prop.get("type")
        if isinstance(value, list) and t in ("title", "rich_text", "select", "status"):
            value = ", ".join(str(v) for v in value)  # e.g. a multi-select Cuisine copied into Cooking's text Cuisine
        if t == "title":
            payload[name] = {"title": _rich(str(value or ""))}
        elif t == "rich_text":
            payload[name] = {"rich_text": _rich(str(value or ""))}
        elif t in ("select", "status"):
            payload[name] = {t: {"name": str(value)} if value else None}
        elif t == "multi_select":
            items = value if isinstance(value, list) else [x.strip() for x in str(value or "").split(",") if x.strip()]
            payload[name] = {"multi_select": [{"name": str(x)[:100]} for x in items]}
        elif t == "checkbox":
            payload[name] = {"checkbox": bool(value)}
        elif t == "number":
            payload[name] = {"number": None if value in (None, "") else float(value)}
        elif t == "date":
            if isinstance(value, (date, datetime)):
                value = value.isoformat()
            payload[name] = {"date": {"start": str(value)} if value else None}
        elif t == "relation":
            ids = value if isinstance(value, list) else [value] if value else []
            payload[name] = {"relation": [{"id": str(i)} for i in ids]}
        elif t == "url":
            payload[name] = {"url": str(value) if value else None}
        else:
            skipped.append(name)  # formulas, rollups, created_time…: read-only
    return payload, skipped


def options(schema: dict, name: str) -> list[str]:
    """Allowed option names for a select / multi-select / status property."""
    prop = schema.get(name) or {}
    t = prop.get("type")
    return [o.get("name") for o in ((prop.get(t) or {}).get("options") or [])]
