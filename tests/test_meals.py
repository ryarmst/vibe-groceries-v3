from datetime import date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from fake_notion import COOKING, GROCERIES, MEALS, PANTRY, FakeNotion
from shopping import appsettings
from shopping.core import service
from shopping.database import session_scope
from shopping.jobs import scheduler, weekly
from shopping.meals import planner
from shopping.notion import kitchen as kmod
from shopping.notion.client import NotionClient
from shopping.retailer.normalize import NormalizedProduct

T = lambda s: [{"type": "text", "text": {"content": s}}]  # noqa: E731


def test_week_and_manifest():
    assert planner.week_start(date(2026, 11, 20)) == date(2026, 11, 19)  # Friday → Thursday
    assert planner.week_start(date(2026, 11, 19)) == date(2026, 11, 19)
    assert planner.week_start(date(2026, 11, 18)) == date(2026, 11, 12)
    serves, items = planner.parse_manifest("Serves: 6\nPork Shoulder | 3 lb (1.4 kg)\nCilantro | 1 bunch | optional\n\n")
    assert serves == 6 and items[1] == {"name": "Cilantro", "quantity": "1 bunch", "optional": True}


def _product(session, pid, name, price, regular=None, cats=None):
    np = NormalizedProduct(retailer_product_id=pid, name=name, effective_price=Decimal(str(price)),
                           regular_price=Decimal(str(regular or price)), sale_price=Decimal(str(price)) if regular else None,
                           deal_text="SAVE" if regular else None, categories=cats or [("1", "Food"), ("2", "Meat"), ("3", "Pork & Ham")])
    service.record_observation(session, np, source="deal_scan", store_id="1234")


async def test_candidates_and_cost(session):
    _product(session, "pork_KG", "Pork Picnic Shoulder", 12.86, 16.0)
    _product(session, "cil_EA", "Cilantro", 1.29, cats=[("1", "Food"), ("4", "Fruits & Vegetables"), ("5", "Herbs")])
    fake = FakeNotion()
    soon = (date.today() + timedelta(days=2)).isoformat()
    fake.add(PANTRY, {"Name": T("Cilantro"), "Expiration": {"start": soon}})
    fake.add(PANTRY, {"Name": T("Ground Pork"), "Expiration": {"start": soon}})
    fake.add(COOKING, {"Name": T("Carnitas"), "Regular Recipe?": True, "Shopping Ingredients": T("Serves: 6\nPork Shoulder | 3 lb\nCilantro | 1 bunch")})
    fake.add(COOKING, {"Name": T("Plain Toast"), "Regular Recipe?": True, "Shopping Ingredients": T("Serves: 2\nBread | 1 loaf")})
    fake.add(MEALS, {"Name": T("Plain Toast"), "Status": {"name": "Rejected"}, "Feedback": T("boring")})
    fake.add(COOKING, {"Name": T("Cilantro Lime Rice"), "Type": {"name": "Recipe"}, "Main Ingredient": T("Cilantro")})
    fake.add(COOKING, {"Name": T("Stewed Pork"), "Type": {"name": "Recipe"}, "Main Ingredient": T("Pork Shoulder")})
    fake.add(COOKING, {"Name": T("Plain Rice"), "Type": {"name": "Recipe"}, "Main Ingredient": T("Rice")})  # nothing favours it
    fake.add(COOKING, {"Name": T("Fruit Salad"), "Type": {"name": "Recipe"}, "Main Ingredient": T("Fruit")})  # too generic
    fake.add(COOKING, {"Name": T("Cilantro Chutney"), "Type": {"name": "Recipe"}, "Course": {"name": "Sauce/Condiment"},
                       "Main Ingredient": T("Cilantro")})  # not a meal
    kmod._schema_cache.clear()
    client = NotionClient("x", transport=httpx.MockTransport(fake.handler))
    client.MIN_INTERVAL = 0
    k = kmod.Kitchen(client, {"pantry": PANTRY, "groceries": GROCERIES, "cooking": COOKING, "meals": MEALS})
    out = await planner.candidates(session, k)
    first, last = out["regular_recipes"][0], out["regular_recipes"][-1]
    assert first["name"] == "Carnitas" and first["uses_up"][0]["name"] == "Cilantro" and first["ingredients_on_sale"]
    assert last["name"] == "Plain Toast" and last["previously_rejected"] and last["last_feedback"] == "boring"
    assert {e["name"] for e in out["expiring_on_hand"]} == {"Cilantro", "Ground Pork"}
    assert out["discovery_mode"] and out["note"]
    saved = [c["name"] for c in out["saved_recipes"]]
    by = {c["name"]: c for c in out["saved_recipes"]}
    assert set(by) == {"Cilantro Lime Rice", "Stewed Pork"}  # generic, non-meal and unfavoured recipes are left out
    assert by["Cilantro Lime Rice"]["uses_up"] == [{"name": "Cilantro", "expires": soon}] and by["Cilantro Lime Rice"]["score"] == 10
    assert by["Stewed Pork"]["main_ingredient_deal"] and by["Stewed Pork"]["uses_up"][0] == {"name": "Ground Pork", "expires": soon, "match": "possible"}
    assert by["Stewed Pork"]["score"] == 6 + 4  # pork on sale + a word-only (possible) use-up
    cost = planner.cost_manifest(session, "Serves: 6\nPork Shoulder | 3 lb\nCilantro | 1 bunch\nUnobtainium | 1")
    assert cost["estimated_total"] == 14.15 and cost["per_serving"] == 2.36 and cost["not_found"] == ["Unobtainium"]


@pytest.fixture
def jobs_settings():
    with session_scope() as s:
        appsettings.update(s, "jobs", {"weekly_plan_enabled": True, "weekly_plan_weekday": 3, "weekly_plan_hour": 7,
                                       "checkins_enabled": False, "order_import_enabled": False})
        s.query(appsettings.AppSetting).filter(appsettings.AppSetting.key.like("job:%")).delete(synchronize_session=False)
    yield
    with session_scope() as s:
        appsettings.update(s, "jobs", {"weekly_plan_enabled": False})


async def test_weekly_plan_schedule(monkeypatch, jobs_settings):
    started = []
    monkeypatch.setattr(weekly, "start_in_background", lambda **kw: started.append(1) or "t1")
    monkeypatch.setattr(weekly, "running", lambda: False)
    await scheduler.tick(datetime(2026, 11, 26, 6, 30))  # Thursday before the hour
    assert started == []
    await scheduler.tick(datetime(2026, 11, 26, 7, 5))
    await scheduler.tick(datetime(2026, 11, 26, 9, 0))  # already done this week
    assert started == [1]
    await scheduler.tick(datetime(2026, 12, 1, 9, 0))  # Tuesday of the next week: too late to catch up
    assert started == [1]
    await scheduler.tick(datetime(2026, 12, 4, 8, 0))  # Friday after a missed Thursday: catch up once
    assert started == [1, 1]
