import pytest

from shopping.core import activity, modify, service
from shopping.models import CartAction, Feedback
from test_service import FakeClient, build_run


@pytest.fixture
def client():
    return FakeClient()


async def _in_cart(session, client):
    run, group, single, ids = await build_run(session, client)
    service.approve_all_current(session, run.id, [s.id for s in service.chosen_selections(run)])
    await service.apply_approved(session, client, run.id, source="approve")
    return run, group, single, ids


async def test_swap_cart_item_from_run(session, client):
    run, group, single, ids = await _in_cart(session, client)
    new_pid = ids[5]
    msg = await modify.modify(session, client, action="swap", product_id=single.product.retailer_product_id, new_product_id=new_pid,
                              tags=["cheaper"], note="half the price", rule="prefer_for_type", in_cart=True)
    assert "Swapped" in msg
    assert client.cart[single.product.retailer_product_id] == 0 and client.cart[new_pid] == single.quantity
    assert single.status == "rejected"
    new_sel = next(s for s in run.selections if s.product.retailer_product_id == new_pid)
    assert new_sel.status == "applied" and new_sel.added_by == "user" and new_sel.reason_codes == ["user_requested"]
    fb = session.query(Feedback).filter_by(action="chose_alternative").one()
    assert fb.tags == ["cheaper"] and fb.note == "half the price" and fb.preference_id
    sources = [a.source for a in session.query(CartAction).order_by(CartAction.id)]
    assert sources[-2:] == ["modify", "modify"]


async def test_remove_and_restore(session, client):
    run, group, single, ids = await _in_cart(session, client)
    pid = single.product.retailer_product_id
    await modify.modify(session, client, action="remove", product_id=pid, tags=["have_enough", "cheaper"], note="2 left", in_cart=True)
    assert client.cart[pid] == 0 and single.status == "rejected"
    fb = session.query(Feedback).filter_by(action="removed").one()
    assert fb.tags == ["have_enough"]  # "cheaper" isn't a removal reason and is dropped
    await modify.modify(session, client, action="restore", product_id=pid, selection_id=single.id)
    assert single.status == "applied" and client.cart[pid] == single.quantity


async def test_quantity_and_note_on_item_added_outside_app(session, client):
    pid = list(client.products)[7]
    client.cart[pid] = 1
    await modify.modify(session, client, action="quantity", product_id=pid, quantity=3, in_cart=True)
    assert client.cart[pid] == 3
    assert session.query(Feedback).count() == 0  # no reason given -> only cart history
    action = session.query(CartAction).one()
    assert action.source == "modify" and action.product_label and action.quantity_before == 1
    await modify.modify(session, client, action="note", product_id=pid, note="kids like this one", in_cart=True)
    assert session.query(Feedback).filter_by(action="note").one().note == "kids like this one"


async def test_activity_feed(session, client):
    run, group, single, ids = await _in_cart(session, client)
    await modify.modify(session, client, action="remove", product_id=single.product.retailer_product_id, tags=["too_expensive"], in_cart=True)
    events = activity.activity(session, days=1)
    kinds = {e["kind"] for e in events}
    assert {"suggested", "approved", "removed", "cart"} <= kinds
    removed = next(e for e in events if e["kind"] == "removed")
    assert "Too expensive" in removed["detail"]
    assert activity.activity(session, days=1, run_id=run.id)
    assert all(e["product_id"] == ids[0] or e.get("chosen_product_id") == ids[0]
               for e in activity.activity(session, days=1, product_id=ids[0]))


async def test_activity_shows_picked_alternative_but_not_unpicked_ones(session, client):
    from shopping.core import feedback as fbk

    run, group, single, ids = await build_run(session, client)
    rec, alt = group.selections
    fbk.triage(session, rec.id, decision="choose", chosen_selection_id=alt.id)
    events = activity.activity(session, days=1, run_id=run.id)
    approved = [e for e in events if e["kind"] == "approved"]
    assert [e["product_id"] for e in approved] == [alt.product.retailer_product_id]
    suggested = [e for e in events if e["kind"] == "suggested"]
    assert len(suggested) == 2 and any("1 alternative" in e["detail"] for e in suggested)
    # The recommendation that was passed over shows as a swap, not as a bare removal.
    assert any(e["kind"] == "swapped" for e in events)
    assert not any(e["kind"] == "removed" and e["product_id"] == rec.product.retailer_product_id for e in events)
