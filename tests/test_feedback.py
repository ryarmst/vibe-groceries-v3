from datetime import timedelta

import pytest

from shopping.core import feedback as fbk
from shopping.core import service
from shopping.models import Feedback, Preference
from test_service import FakeClient, build_run


@pytest.fixture
def client():
    return FakeClient()


async def test_remove_with_reason_and_rule(session, client):
    run, group, single, ids = await build_run(session, client)
    fb = fbk.triage(session, single.id, decision="remove", tags=["have_enough"], note="still two jars", rule="blacklist_product")
    assert single.status == "rejected"
    assert fb.action == "removed" and fb.tags == ["have_enough"] and fb.note == "still two jars"
    pref = session.get(Preference, fb.preference_id)
    assert pref.kind == "blacklist_product" and pref.target == single.product.retailer_product_id and pref.scope is None
    assert fb.requirement == single.requirement


async def test_choose_alternative_records_both_products(session, client):
    run, group, single, ids = await build_run(session, client)
    rec, alt = group.selections
    fb = fbk.triage(session, rec.id, decision="choose", chosen_selection_id=alt.id, tags=["better_size"], rule="prefer_for_type")
    assert group.selected_selection_id == alt.id and alt.status == "approved" and rec.status == "rejected"
    assert fb.action == "chose_alternative" and fb.product_id == rec.product_id and fb.chosen_product_id == alt.product_id
    pref = session.get(Preference, fb.preference_id)
    assert pref.kind == "prefer_for_type" and pref.scope == "Peanut butter" and pref.target == alt.product.retailer_product_id


async def test_keep_without_reason_records_nothing(session, client):
    run, group, single, ids = await build_run(session, client)
    assert fbk.triage(session, single.id, decision="keep") is None
    assert single.status == "approved"
    fb = fbk.triage(session, group.selections[0].id, decision="keep", quantity=3)
    assert fb.action == "quantity_changed" and group.selections[0].quantity == 3


async def test_bad_tags_rejected(session, client):
    run, group, single, ids = await build_run(session, client)
    with pytest.raises(service.ShoppingError):
        fbk.triage(session, single.id, decision="remove", tags=["cheaper"])  # a choose-reason, not a remove-reason


async def test_patterns_and_requirement_history(session, client):
    ids = list(client.products)
    # Two runs where the user removes the same product as disliked and picks the same alternative.
    for _ in range(2):
        run = service.create_run(session, summary="w", store_id="1234")
        group = await service.add_choice_group(session, client, run.id, label="Peanut butter", options=[
            {"product_id": ids[0], "reasons": ["grocery_list"]}, {"product_id": ids[1], "reasons": ["grocery_list"]}])
        fbk.triage(session, group.selections[0].id, decision="choose", chosen_selection_id=group.selections[1].id, tags=["cheaper"])
        milk = await service.add_selection(session, client, run.id, product_id=ids[2], reasons=["regular_staple"], requirement="Milk")
        fbk.triage(session, milk.id, decision="remove", tags=["have_enough", "dislike_product"])
    summary = fbk.feedback_summary(session)
    kinds = {(p["kind"], p["target"]) for p in summary["patterns"]}
    assert ("prefer_for_type", ids[1]) in kinds
    assert ("blacklist_product", ids[2]) in kinds
    assert any(p["kind"] == "cadence" and not p["actionable"] for p in summary["patterns"])
    assert summary["by_action"] == {"chose_alternative": 2, "removed": 2}

    hist = {h["requirement"]: h for h in fbk.requirement_history(session, ["Milk", "Bread"])}
    assert hist["Milk"]["status"] == "not_due" and hist["Milk"]["times_removed"] == 2
    assert hist["Bread"]["status"] == "unknown" and hist["Bread"]["times_suggested"] == 0

    by_product = fbk.feedback_by_product(session, [ids[0], ids[2]])
    assert by_product[ids[0]]["passed_over"] == 2 and by_product[ids[2]]["removed"] == 2


async def test_requirement_due_from_cart_history(session, client):
    ids = list(client.products)
    for days_ago in (21, 14):
        run = service.create_run(session, summary="w", store_id="1234")
        sel = await service.add_selection(session, client, run.id, product_id=ids[0], reasons=["regular_staple"], requirement="Eggs")
        service.decide_selection(session, sel.id, approve=True)
        await service.apply_approved(session, client, run.id)
        sel.applied_to_cart_at = sel.applied_to_cart_at - timedelta(days=days_ago)
        client.cart.clear()
    h = fbk.requirement_history(session, ["eggs"])[0]
    assert h["typical_interval_days"] == 7 and h["status"] == "due" and h["last_added"]["days_ago"] == 14


async def test_mark_reviewed(session, client):
    run, group, single, ids = await build_run(session, client)
    fb = fbk.triage(session, single.id, decision="remove", tags=["too_expensive"])
    assert fbk.mark_reviewed(session, [fb.id], "added to skill") == 1
    assert session.get(Feedback, fb.id).resolution == "added to skill"
    assert fbk.list_feedback(session, unreviewed_only=True) == []
