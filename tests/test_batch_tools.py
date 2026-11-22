from dataclasses import replace

import pytest

from shopping import context
from shopping.database import session_scope
from shopping.agent import shopping_tools as mcp
from shopping.models import ChoiceGroup, Selection, ShoppingRun
from test_service import FakeClient


class SearchingClient(FakeClient):
    async def search_products(self, query, page=1, **kw):
        q = query.lower().split()[0]
        return {"products": [p for p in self.products.values() if q in p.name.lower()], "total_results": 0}


@pytest.fixture
def fake(monkeypatch):
    c = SearchingClient()
    monkeypatch.setattr(context, "client", lambda: c)
    return c


async def test_stage_items_creates_run_stages_all_and_submits(fake):
    ids = list(fake.products)
    out_of_stock = ids[3]
    fake.products[out_of_stock] = replace(fake.products[out_of_stock], availability="OUT_OF_STOCK")
    r = await mcp.shopping_stage_items(summary="Batch test", submit=True, items=[
        mcp.StageItem(requirement="Peanut Butter", options=[mcp.ChoiceOption(product_id=ids[0]), mcp.ChoiceOption(product_id=ids[1])],
                      recommended_index=1, reasons=["grocery_list"]),
        mcp.StageItem(requirement="Jam", product_id=ids[2], quantity=2, reasons=["regular_staple"], rationale="due"),
        mcp.StageItem(requirement="Crackers", product_id=out_of_stock, reasons=["grocery_list"]),
        mcp.StageItem(requirement="Nothing"),
    ])
    assert r["staged"] == 2 and r["status"] == "review" and r["review_url"].startswith("#/runs/")
    assert {f["requirement"] for f in r["failed"]} == {"Crackers", "Nothing"} and "out of stock" in r["failed"][0]["error"]
    with session_scope() as s:
        run = s.get(ShoppingRun, r["run_id"])
        group = s.query(ChoiceGroup).filter_by(shopping_run_id=run.id).one()
        assert group.label == "Peanut Butter" and group.recommended_selection_id == group.selections[1].id
        assert {sel.requirement for sel in group.selections} == {"Peanut Butter"}
        jam = s.query(Selection).filter_by(shopping_run_id=run.id, requirement="Jam").one()
        assert jam.quantity == 2 and jam.reason_codes == ["regular_staple"]
    # A second batch into the same run.
    r2 = await mcp.shopping_stage_items(run_id=r["run_id"], items=[mcp.StageItem(requirement="More Jam", product_id=ids[4])])
    assert r2["run_id"] == r["run_id"] and r2["staged"] == 1


async def test_search_many(fake):
    some = next(iter(fake.products.values()))
    word = some.name.split()[0]
    fake.products["gone_EA"] = replace(some, retailer_product_id="gone_EA", availability="OUT_OF_STOCK")
    r = await mcp.shopping_search_many(queries=[word, word, "zzz-nothing"], per_query=3)
    assert set(r["results"]) == {word, "zzz-nothing"}  # duplicates collapsed
    hits = r["results"][word]
    assert 0 < len(hits) <= 3 and all("gone_EA" != h["product_id"] for h in hits)
    assert set(hits[0]) <= set(mcp.COMPACT_KEYS)
    assert r["results"]["zzz-nothing"] == []


async def test_get_past_orders_marks_imported(fake, monkeypatch):
    from shopping.models import PastOrder, utcnow
    from shopping.retailer.normalize import PastOrderSummary

    async def list_past_orders():
        return [PastOrderSummary("111", "2026-11-17T10:00:00Z", 75.6, "NOFRILLS", "DELIVERY", None),
                PastOrderSummary("222", "2026-11-08T10:00:00Z", 176.26, "NOFRILLS", "DELIVERY", None)]

    fake.list_past_orders = list_past_orders
    with session_scope() as s:
        if s.get(PastOrder, "111") is None:
            s.add(PastOrder(id="111", placed_at=utcnow()))
    r = await mcp.shopping_get_past_orders(limit=5)
    assert {o["order_id"]: o["imported"] for o in r["orders"]} == {"111": True, "222": False}


async def test_quick_add_goes_straight_to_the_cart(fake):
    ids = list(fake.products)
    fake.cart[ids[1]] = 3  # already has more than asked: never lowered
    fake.products[ids[2]] = replace(fake.products[ids[2]], availability="OUT_OF_STOCK")
    r = await mcp.shopping_quick_add(summary="Taco night", items=[
        mcp.QuickItem(requirement="Peanut butter", product_id=ids[0], quantity=2),
        mcp.QuickItem(requirement="Jam", product_id=ids[1], quantity=1),
        mcp.QuickItem(requirement="Crackers", product_id=ids[2]),
    ])
    assert fake.cart[ids[0]] == 2 and fake.cart[ids[1]] == 3 and ids[2] not in fake.cart
    assert {a["name"] for a in r["added"]} and len(r["added"]) == 2
    assert [f["requirement"] for f in r["failed"]] == ["Crackers"] and r["run_url"].startswith("#/runs/")
    with session_scope() as s:
        run = s.get(ShoppingRun, r["run_id"])
        assert run.summary == "Quick Shop: Taco night" and run.status == "applied"
        assert {sel.status for sel in run.selections} == {"applied"}


async def test_quick_add_limits(fake):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        mcp.QuickItem(requirement="x", product_id="y", quantity=13)


async def test_quick_add_is_refused_on_a_schedule(monkeypatch):
    from shopping.agent import loop, tools
    assert "shopping_quick_add" in tools.CART_WRITE
    assert "shopping_quick_add" not in loop.playbooks.load("shop").tools  # review-first everywhere else
    assert "shopping_quick_add" in loop.playbooks.load("quick").tools
    assert loop.playbooks.route("buy milk and add it to my cart") != "quick"  # never auto-routed


def test_size_parsing():
    assert mcp.parse_size("2% milk 1 L") == (1000.0, "ml")
    assert mcp.parse_size("1.36 kg") == (1360.0, "g")
    assert mcp.parse_size("3 lb bag") == (pytest.approx(1360.8), "g")
    assert mcp.parse_size("2% milk") is None and mcp.parse_size("12 eggs") is None
    assert mcp.same_size(mcp.parse_size("1 l"), mcp.parse_size("1000 ml"))
    assert not mcp.same_size(mcp.parse_size("4 l"), mcp.parse_size("1 l"))


async def test_search_many_puts_the_named_size_first(fake):
    some = next(iter(fake.products.values()))
    word = some.name.split()[0]
    fake.products.clear()
    fake.products["big_EA"] = replace(some, retailer_product_id="big_EA", raw_size="4 l", unit_price=0.1)
    fake.products["small_EA"] = replace(some, retailer_product_id="small_EA", raw_size="1 l", unit_price=0.3)
    r = await mcp.shopping_search_many(queries=[f"{word} 1 L"])
    assert r["results"][f"{word} 1 L"][0]["product_id"] == "small_EA"
    r = await mcp.shopping_search_many(queries=[word])
    assert r["results"][word][0]["product_id"] == "big_EA"  # no size named: cheapest per unit


async def test_quick_add_requirement_is_optional(fake):
    pid = list(fake.products)[0]
    r = await mcp.shopping_quick_add(items=[mcp.QuickItem(product_id=pid)])
    assert len(r["added"]) == 1
