"""End-to-end check that an external MCP client can use the shopping tools.

    python scripts/mcp_smoke.py [http://localhost:8000/mcp/]

Creates a real (small) shopping run. Does not touch the cart.
Set MCP_TOKEN in the environment if the server requires one.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx2  # bundled with the mcp SDK
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


def show(label: str, result) -> dict:
    if result.is_error:
        text = " ".join(getattr(c, "text", "") for c in result.content)
        raise SystemExit(f"{label} FAILED: {text}")
    data = result.structured_content or json.loads(result.content[0].text)
    if isinstance(data, dict) and set(data) == {"result"}:
        data = data["result"]
    print(f"✓ {label}")
    return data


async def main(url: str) -> None:
    headers = {"Authorization": f"Bearer {os.environ['MCP_TOKEN']}"} if os.environ.get("MCP_TOKEN") else {}
    async with httpx2.AsyncClient(headers=headers, timeout=60) as http, Client(streamable_http_client(url, http_client=http)) as c:
        tools = await c.list_tools()
        names = sorted(t.name for t in tools.tools)
        print(f"✓ list_tools: {len(names)} tools")
        assert not any(w in n for n in names for w in ("checkout", "payment", "purchase", "place", "submit_order")), names
        assert {n for n in names if "order" in n} <= {"shopping_get_past_orders", "shopping_get_past_order"}, names

        prefs = show("get_preferences", await c.call_tool("shopping_get_preferences", {}))
        print(f"    {len(prefs['preferences'])} active preferences")

        found = show("search_products", await c.call_tool("shopping_search_products", {"query": "peanut butter", "limit": 8, "sort_by_unit_price": True}))
        for r in found["results"][:4]:
            print(f"    {r['product_id']:<18} {r.get('brand') or '':<18} {r['name'][:32]:<32} ${r['price']:>6}  {r.get('unit_price')}/{r.get('unit_price_basis')}  {r.get('deal') or ''}")

        deals = show("get_deals", await c.call_tool("shopping_get_deals", {"kind": "flyer", "query": "milk", "limit": 5}))
        for r in deals["results"][:3]:
            print(f"    {r['name'][:40]:<40} ${r['price']}  {r.get('deal') or r.get('pc_optimum') or ''}")

        run = show("create_run", await c.call_tool("shopping_create_run", {"summary": "MCP smoke test", "notes": "Created by scripts/mcp_smoke.py"}))
        run_id = run["run_id"]

        jars = [r for r in found["results"] if r.get("comparison_family") == "weight"][:3]
        group = show("add_choice_group", await c.call_tool("shopping_add_choice_group", {
            "run_id": run_id, "label": "Peanut butter",
            "options": [{"product_id": r["product_id"], "quantity": 1, "reasons": ["grocery_list"],
                         "rationale": f"{r['name']} at {r.get('unit_price')} per {r.get('unit_price_basis')}"} for r in jars],
            "recommended_index": 0,
        }))
        print(f"    group {group['choice_group_id']} with {len(group['options'])} options")

        if deals["results"]:
            d = deals["results"][0]
            sel = show("add_selection", await c.call_tool("shopping_add_selection", {
                "run_id": run_id, "product_id": d["product_id"], "quantity": 1, "reasons": ["grocery_list", "good_deal"],
                "rationale": f"On the flyer: {d.get('deal') or 'sale'}", "requirement": "milk",
            }))
            show("update_selection", await c.call_tool("shopping_update_selection", {"selection_id": sel["selection_id"], "quantity": 2}))

        sub = show("submit_run_for_review", await c.call_tool("shopping_submit_run_for_review", {"run_id": run_id}))
        print(f"    review at {sub['review_url']}  (est. ${sub['estimated_total']})")

        got = show("get_run", await c.call_tool("shopping_get_run", {"run_id": run_id, "include_history": True}))
        print(f"    status={got['status']} total=${got['estimated_total']} counts={got['status_counts']}")
        show("list_runs", await c.call_tool("shopping_list_runs", {"limit": 3}))
        hist = show("get_product_history", await c.call_tool("shopping_get_product_history", {"product_id": jars[0]["product_id"]}))
        print(f"    {hist['stats'].get('observations')} observations of {hist['product']['name']}")
        show("get_selection_history", await c.call_tool("shopping_get_selection_history", {"query": "peanut"}))
        show("get_recent_price_history", await c.call_tool("shopping_get_recent_price_history", {"days": 30}))
        fb = show("get_feedback", await c.call_tool("shopping_get_feedback", {"days": 30}))
        print(f"    {fb['summary']['total']} feedback records, {len(fb['summary']['patterns'])} patterns")
        req = show("get_requirement_history", await c.call_tool("shopping_get_requirement_history", {"requirements": ["Peanut butter", "Milk"]}))
        for r in req["requirements"]:
            print(f"    {r['requirement']}: {r['status']} ({r['why']})")
        auth = show("auth_status", await c.call_tool("shopping_auth_status", {}))
        print(f"    retailer session: {auth['label']}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000/mcp/"))
