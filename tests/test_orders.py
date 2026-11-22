from datetime import datetime, timedelta, timezone

import pytest

from conftest import load_fixture
from shopping.core import activity, orders, service
from shopping.core.feedback import _name_matches, requirement_history
from shopping.models import PastOrderLine, PriceObservation
from shopping.retailer.normalize import (
    from_product_detail,
    past_order_from_detail,
    past_orders_from_history,
)
from test_service import FakeClient

ORDER_ID = "100000000000012"


def order_data():
    return past_order_from_detail(load_fixture("past_order_detail.json"))


def summary():
    return next(s for s in past_orders_from_history(load_fixture("past_orders.json")) if s.order_id == ORDER_ID)


def test_parse_order():
    o = order_data()
    assert o.order_id == ORDER_ID and str(o.total) == "176.26"
    fees = [l.name for l in o.lines if l.is_fee]
    assert fees == ["PD ORDER", "WAIVE PD ORDER", "$3 DRIVER TIP"]
    assert [l.name for l in o.lines if not l.fulfilled] == ["Okra"]
    cabbage = next(l for l in o.lines if l.name == "Cabbage, Green")
    assert cabbage.weighted and cabbage.weight_kg == 2.3 and cabbage.quantity is None
    pepsi = next(l for l in o.lines if l.name == "Zero Sugar Cola")
    assert pepsi.quantity == 1  # reported as 0 but billed once
    assert len(past_orders_from_history(load_fixture("past_orders.json"))) == 12


@pytest.mark.parametrize("req, name, ok", [
    ("broccoli", "Broccoli Crown", True),
    ("avocados", "Avocado Bag", True),
    ("napa cabbage", "Chinese Napa Cabbage", True),
    ("tomatoes", "Tomato Beefsteak  Red", True),
    ("milk", "2% Milk", True),
    ("garlic", "Garlic Powder", True),
    ("lemons", "Lime Juice", False),
    ("eggs", "Eggplant", False),
])
def test_name_matching(req, name, ok):
    assert _name_matches(req, name) is ok


async def test_import_matches_runs_and_records_prices_once(session):
    client = FakeClient()
    lemons = from_product_detail(load_fixture("product_detail.json"))  # any product will do for the run
    lemons.retailer_product_id = "20795315001_EA"
    client.products["20795315001_EA"] = lemons
    run = service.create_run(session, summary="w", store_id="1234")
    sel = await service.add_selection(session, client, run.id, product_id="20795315001_EA", reasons=["regular_staple"], requirement="Lemons")
    hummus_id = next(iter(client.products))
    hummus = await service.add_selection(session, client, run.id, product_id=hummus_id, reasons=["regular_staple"], requirement="Hummus")
    placed = datetime.fromisoformat(summary().placed_at.replace("Z", "+00:00"))
    for s in (sel, hummus):
        service.decide_selection(session, s.id, approve=True)
        s.status, s.applied_to_cart_at = "applied", placed - timedelta(hours=2)
    session.flush()

    order = orders.import_order(session, order_data(), summary(), store_id="1234")
    view = orders.order_view(session, order)
    assert view["from_runs"] == 1
    assert next(i for i in view["items"] if i["product_id"] == "20795315001_EA")["from_run"]["requirement"] == "Lemons"
    assert [a["requirement"] for a in view["approved_in_app_but_not_bought"]] == ["Hummus"]
    assert "Kale Bunch" in view["added_outside_app"] and "Okra" in view["not_fulfilled"]
    assert len(view["fees_and_tips"]) == 3 and view["item_count"] == 30
    n_obs = session.query(PriceObservation).filter_by(source="order").count()
    assert n_obs == 29  # fulfilled, non-fee lines

    orders.import_order(session, order_data(), summary(), store_id="1234")  # re-import: refresh, no duplicate prices
    assert session.query(PriceObservation).filter_by(source="order").count() == n_obs
    assert session.query(PastOrderLine).count() == 33


async def test_requirement_history_uses_purchases(session):
    orders.import_order(session, order_data(), summary(), store_id="1234")
    h = {r["requirement"]: r for r in requirement_history(session, ["Broccoli", "Napa Cabbage", "Hummus"])}
    assert h["Broccoli"]["last_purchased"]["quantity"] == 3 and h["Broccoli"]["times_purchased"] == 1
    assert h["Napa Cabbage"]["last_purchased"]["quantity"] == "1.985 kg"
    assert h["Hummus"]["last_purchased"] is None and h["Hummus"]["status"] == "unknown"
    assert "bought" in h["Broccoli"]["why"]


def test_activity_shows_orders(session):
    order = orders.import_order(session, order_data(), summary(), store_id="1234")
    order.placed_at = datetime.now(timezone.utc) - timedelta(days=1)
    session.flush()
    ev = activity.activity(session, days=7, kind="purchased")
    assert ev and ev[0]["order_id"] == ORDER_ID and "29 items" in ev[0]["detail"]  # okra wasn't fulfilled
    per_product = activity.activity(session, days=7, product_id="21121560001_EA")
    assert per_product[0]["title"] == "Bought" and "×3" in per_product[0]["detail"]
