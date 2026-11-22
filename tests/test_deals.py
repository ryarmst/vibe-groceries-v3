import json
from dataclasses import replace

import pytest

from shopping.core import activity as act
from shopping.core import deals as dls
from shopping.core.service import ShoppingError
from shopping.models import Feedback, TopDeal
from test_service import FakeClient


@pytest.fixture
def client():
    return FakeClient()


async def test_flag_and_refresh(session, client):
    pid = list(client.products)[0]
    np = replace(await client.get_product(pid), deal_expiry=None)
    deal = dls.flag_deal(session, np, headline="Lowest we've seen", why="half price", relevant_to="Peanut butter", rating=3,
                         suggested_quantity=2, store_id="1234")
    again = dls.flag_deal(session, np, headline="Still cheap", why=None, rating=2, store_id="1234")
    assert again.id == deal.id and again.headline == "Still cheap" and again.why == "half price"  # refreshed, not duplicated
    assert session.query(TopDeal).count() == 1

    with pytest.raises(ShoppingError):
        dls.flag_deal(session, np, headline=None, why=None, rating=5)


async def test_dismiss_records_feedback(session, client):
    np = replace(await client.get_product(list(client.products)[1]), deal_expiry=None)
    deal = dls.flag_deal(session, np, headline=None, why="good unit price", relevant_to="Butter", store_id="1234")
    assert deal.headline  # defaults to % off / deal text
    dls.dismiss(session, deal.id, note="Costco")
    fb = session.query(Feedback).filter_by(action="deal_dismissed").one()
    assert fb.note == "Costco" and fb.requirement == "Butter" and json.loads(fb.context)["from"]["agent_rationale"] == "good unit price"
    assert deal.response_note == "Costco"
    kinds = {e["title"] for e in act.activity(session, kind="deal")}
    assert {"Deal flagged", "Deal dismissed"} <= kinds


async def test_ended_sales_retire_and_out_of_stock_refused(session, client):
    pid = list(client.products)[2]
    np = await client.get_product(pid)
    deal = dls.flag_deal(session, replace(np, deal_expiry="2020-01-01T00:00"), headline="x", why=None, store_id="1234")
    assert dls.list_deals(session) == [] and deal.status == "retired"
    assert [d.id for d in dls.list_deals(session, include_closed=True)] == [deal.id]
    with pytest.raises(ShoppingError):
        dls.flag_deal(session, replace(np, availability="OUT_OF_STOCK"), headline="x", why=None)


