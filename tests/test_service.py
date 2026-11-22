from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from conftest import load_fixture
from shopping.core import service
from shopping.models import CartAction, PriceObservation
from shopping.retailer.normalize import Cart, CartLine, from_search_tile


def catalogue():
    data = load_fixture("search_peanut_butter.json")
    tiles = [t for c in data["layout"]["sections"]["mainContentCollection"]["components"] for t in (c.get("data") or {}).get("productTiles") or []]
    return {p.retailer_product_id: p for p in (from_search_tile(t) for t in tiles) if not p.sponsored}


class FakeClient:
    store_id = "1234"

    def __init__(self):
        self.products = catalogue()
        self.cart: dict[str, float] = {}
        self.subs: dict[str, object] = {}  # product id -> SubstitutionSpec last sent
        self.set_calls: list[tuple[str, float]] = []

    async def get_product(self, product_id):
        return self.products[product_id]

    async def get_cart(self):
        lines = [CartLine(pid, "x", None, q, None, None, None, None, "1234",
                          substitution_allowed=getattr(self.subs.get(pid), "allowed", False))
                 for pid, q in self.cart.items()]
        return Cart("cart-1", "1234", "Test", lines, None, None, None)

    async def set_cart_quantity(self, product_id, quantity, substitution=None):
        self.set_calls.append((product_id, quantity))
        self.cart[product_id] = quantity
        if substitution is not None:
            self.subs[product_id] = substitution
        return await self.get_cart()

    async def set_cart_substitution(self, product_id, substitution):
        self.subs[product_id] = substitution
        return await self.get_cart()


@pytest.fixture
def client():
    return FakeClient()


async def build_run(session, client):
    ids = list(client.products)
    run = service.create_run(session, summary="Weekly", store_id="1234")
    group = await service.add_choice_group(session, client, run.id, label="Peanut butter", recommended_index=0, options=[
        {"product_id": ids[0], "quantity": 1, "reasons": ["grocery_list"], "rationale": "cheapest per 100 g"},
        {"product_id": ids[1], "quantity": 1, "reasons": ["grocery_list"]},
    ])
    single = await service.add_selection(session, client, run.id, product_id=ids[2], quantity=2, reasons=["pantry_low", "good_deal"], rationale="low")
    service.submit_run_for_review(session, run.id)
    return run, group, single, ids


async def test_run_lifecycle_and_apply(session, client):
    run, group, single, ids = await build_run(session, client)
    assert run.status == "review"
    assert group.selections[1].reason_codes == ["grocery_list", "alternative"]
    snapshot = [s.id for s in service.chosen_selections(run)]
    assert len(snapshot) == 2  # recommended option + standalone

    # The agent adds an item after the page was rendered: it must stay pending.
    late = await service.add_selection(session, client, run.id, product_id=ids[3], reasons=["user_requested"])
    assert service.approve_all_current(session, run.id, snapshot) == 2
    assert late.status == "pending" and run.status == "review"
    service.decide_selection(session, late.id, approve=False)
    assert run.status == "partially_approved"  # some approved, one rejected

    client.cart[ids[2]] = 1  # already in the cart with a lower quantity
    result = await service.apply_approved(session, client, run.id)
    assert result["applied"] == 2 and result["failed"] == 0
    assert client.cart[ids[0]] == 1 and client.cart[ids[2]] == 2
    assert run.status == "applied"
    actions = session.query(CartAction).all()
    assert {(a.retailer_product_id, a.quantity_before, a.quantity_after) for a in actions} == {(ids[0], 0, 1), (ids[2], 1, 2)}

    # Re-applying is a no-op.
    again = await service.apply_approved(session, client, run.id)
    assert again["applied"] == 0 and len(client.set_calls) == 2
    assert {a.source for a in actions} == {"apply"} and all(a.product_label for a in actions)


async def test_apply_never_lowers_cart_quantity(session, client):
    run, group, single, ids = await build_run(session, client)
    service.approve_all_current(session, run.id, [s.id for s in service.chosen_selections(run)])
    client.cart[ids[2]] = 5
    await service.apply_approved(session, client, run.id)
    assert client.cart[ids[2]] == 5
    assert (ids[2], 2) not in client.set_calls


async def test_choose_alternative_carries_approval(session, client):
    run, group, single, ids = await build_run(session, client)
    rec, alt = group.selections
    service.decide_selection(session, rec.id, approve=True)
    service.choose_alternative(session, group.id, alt.id)
    assert group.selected_selection_id == alt.id
    assert alt.status == "approved" and rec.status == "rejected"


async def test_agent_cannot_edit_decided_or_use_unknown_reasons(session, client):
    run, group, single, ids = await build_run(session, client)
    with pytest.raises(service.ShoppingError, match="Unknown reason"):
        await service.add_selection(session, client, run.id, product_id=ids[3], reasons=["because"])
    service.decide_selection(session, single.id, approve=True)
    with pytest.raises(service.ShoppingError, match="only pending"):
        service.update_selection(session, single.id, quantity=9)


async def test_estimated_total_uses_chosen_options(session, client):
    run, group, single, ids = await build_run(session, client)
    p0, p2 = client.products[ids[0]], client.products[ids[2]]
    assert service.estimated_total(run) == p0.effective_price + p2.effective_price * 2
    service.decide_selection(session, single.id, approve=False)
    assert service.estimated_total(run) == p0.effective_price


def test_observation_dedupe_and_package_change(session):
    p = next(iter(catalogue().values()))
    a = service.record_observation(session, p, source="search", store_id="1234")
    b = service.record_observation(session, p, source="search", store_id="1234")
    assert a.id == b.id  # same day, same price

    # Six months earlier the same product was a bigger package at the same price (shrinkflation).
    bigger = replace(p, size_value=p.size_value * Decimal("1.2"), raw_size="bigger", unit_price=p.unit_price / Decimal("1.2"))
    old = service.record_observation(session, bigger, source="search", store_id="1234", dedupe=False)
    old.observed_at = old.observed_at - timedelta(days=180)
    session.flush()
    stats = service.price_stats(session, service.upsert_product(session, p), store_id="1234")
    assert stats["package_size_changed"] is True
    assert stats["max_unit_price"] > stats["min_unit_price"]
    assert session.query(PriceObservation).count() == 2


def test_price_stats_history(session):
    p = next(iter(catalogue().values()))
    for days_ago, price in [(60, "6.49"), (30, "5.99"), (0, "4.99")]:
        o = service.record_observation(session, replace(p, effective_price=Decimal(price)), source="search", store_id="1234", dedupe=False)
        o.observed_at = o.observed_at - timedelta(days=days_ago)
    session.flush()
    s = service.price_stats(session, service.upsert_product(session, p), store_id="1234")
    assert s["current_price"] == 4.99 and s["previous_price"] == 5.99
    assert s["min_price"] == 4.99 and s["max_price"] == 6.49
    assert s["is_lowest_observed"] is True and s["vs_avg_90d_pct"] < 0
    brief = service.history_brief(session, [p.retailer_product_id], "1234")
    assert brief[p.retailer_product_id]["days_observed"] == 3


async def test_packaged_items_take_whole_quantities(session, client):
    run, group, single, ids = await build_run(session, client)
    with pytest.raises(service.ShoppingError, match="whole number"):
        service.user_set_selection_quantity(session, single.id, 1.1)
    assert service.user_set_selection_quantity(session, single.id, 3.0).quantity == 3
    with pytest.raises(service.ShoppingError, match="whole number"):
        await service.add_selection(session, client, run.id, product_id=ids[4], quantity=0.5, reasons=["grocery_list"])
    assert service.whole_quantity(0.25, "weight") == 0.25  # sold-by-weight items may be fractional


async def test_out_of_stock_products_are_not_staged(session, client):
    from dataclasses import replace

    pid = list(client.products)[0]
    client.products[pid] = replace(client.products[pid], availability="OUT")
    run = service.create_run(session, summary="w", store_id="1234")
    with pytest.raises(service.ShoppingError, match="out of stock"):
        await service.add_selection(session, client, run.id, product_id=pid, reasons=["good_deal"])
