import json

import httpx
import pytest

from conftest import load_fixture
from shopping.core import feedback as fbk
from shopping.core import service
from shopping.models import CartAction, Feedback
from shopping.retailer.normalize import SubstitutionSpec, cart_from_response, past_order_from_detail, substitute_text
from test_client import fake_jwt, make_client
from test_service import FakeClient, build_run


@pytest.fixture
def client():
    return FakeClient()


async def test_cart_update_body_matches_website(tmp_path):
    sent = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"carts": [{"id": "c"}]})
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=load_fixture("cart_update.json"))

    rclient, auth = make_client(tmp_path, handler)
    auth.set_access_token(fake_jwt())
    sub = from_fixture_product()
    await rclient.set_cart_quantity("20070132001_EA", 2, substitution=SubstitutionSpec(allowed=True, comment="any brand", substitute=sub))
    entry = sent[-1]["entries"]["20070132001_EA"]
    assert entry["quantity"] == 2 and entry["substitutionAllowed"] is True and entry["substitutionComment"] == "any brand"
    assert entry["selectedSubstitution"].startswith("Customer Substitute: Kraft, Smooth Peanut Butter, Price: 5.97, ArticleID: 20039581001_EA")
    await rclient.set_cart_quantity("x_EA", 1)  # plain quantity change: no substitution fields
    assert set(sent[-1]["entries"]["x_EA"]) == {"quantity", "fulfillmentMethod", "sellerId"}


def from_fixture_product():
    from shopping.retailer.normalize import from_product_detail

    return from_product_detail(load_fixture("product_detail.json"))


def test_substitute_text_format():
    assert substitute_text(from_fixture_product()) == "Customer Substitute: Kraft, Smooth Peanut Butter, Price: 5.97, ArticleID: 20039581001_EA"


def test_cart_and_order_parse_substitution():
    data = load_fixture("cart_populated.json")
    entry = data["orders"][0]["entries"][0]
    entry["substitutionPreference"] = {"substitutionAllowed": True, "comment": "same size"}
    entry["selectedSubstitution"] = "20737570001_EA"
    line = cart_from_response(data).lines[0]
    assert line.substitution_allowed is True and line.substitution_comment == "same size" and line.selected_substitution == "20737570001_EA"
    detail = load_fixture("past_order_detail.json")
    detail["orderDetails"]["entries"][0]["substitutionData"] = {"substitutionAllowed": False, "comment": None}
    assert past_order_from_detail(detail).lines[0].substitution_allowed is False


async def test_agent_choice_flows_to_cart_and_user_override_is_feedback(session, client):
    ids = list(client.products)
    run = service.create_run(session, summary="w", store_id="1234")
    ok = await service.add_selection(session, client, run.id, product_id=ids[0], reasons=["regular_staple"], requirement="Milk",
                                     substitution_allowed=True, substitution_note="any 2% 4 L")
    deal = await service.add_selection(session, client, run.id, product_id=ids[1], reasons=["good_deal"], requirement="Butter")
    assert ok.substitution_allowed and not deal.substitution_allowed
    for s in (ok, deal):
        service.decide_selection(session, s.id, approve=True)
    await service.apply_approved(session, client, run.id)
    assert client.subs[ids[0]].allowed is True and client.subs[ids[0]].comment == "any 2% 4 L"
    assert client.subs[ids[1]].allowed is False  # sent explicitly, not left to the retailer's default

    msg = await service.set_substitution(session, client, selection_id=ok.id, allowed=False, note=None)
    assert "off" in msg and "cart" in msg and client.subs[ids[0]].allowed is False
    fb = session.query(Feedback).filter_by(action="substitution_changed").one()
    assert json.loads(fb.context)["from"]["allowed"] is True and fb.requirement == "Milk"
    assert session.query(CartAction).filter_by(action="substitution").count() == 1

    hist = fbk.requirement_history(session, ["Milk"])[0]
    assert hist["last_substitution"] == {"allowed": False, "note": None}


async def test_repeated_substitution_changes_become_a_pattern(session, client):
    ids = list(client.products)
    for _ in range(2):
        run = service.create_run(session, summary="w", store_id="1234")
        s = await service.add_selection(session, client, run.id, product_id=ids[0], reasons=["regular_staple"], requirement="Bread",
                                        substitution_allowed=True)
        await service.set_substitution(session, client, selection_id=s.id, allowed=False, note=None)
    pats = fbk.feedback_summary(session)["patterns"]
    assert any(p["kind"] == "substitution" and "Subs: never" in p["why"] for p in pats)


async def test_agent_can_update_pending_substitution(session, client):
    run, group, single, ids = await build_run(session, client)
    service.update_selection(session, single.id, substitution_allowed=True, substitution_note="any brand")
    assert single.substitution_allowed and single.substitution_note == "any brand"
    service.update_selection(session, single.id, substitute_product_id=ids[4])
    assert single.substitute_product_id == ids[4] and single.substitution_allowed
