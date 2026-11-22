from decimal import Decimal

import pytest

from conftest import load_fixture
from shopping.retailer.normalize import (
    cart_from_response,
    from_product_detail,
    from_search_tile,
    money,
    parse_retailer_unit_prices,
    parse_size,
)


@pytest.mark.parametrize(
    "text, value, unit, family",
    [
        ("1 kg, $0.60/100g", Decimal(1000), "g", "weight"),
        ("226 g, $3.54/100g", Decimal(226), "g", "weight"),
        ("4 l, $0.16/100ml", Decimal(4000), "ml", "volume"),
        ("946 ml, $0.32/100ml", Decimal(946), "ml", "volume"),
        ("4x300.0 ml, $0.58/100ml", Decimal(1200), "ml", "volume"),
        ("12x355.0 ml, $0.20/100ml", Decimal(4260), "ml", "volume"),
        ("12 ea, $0.33/1ea", Decimal(12), "ea", "count"),
        ("3x98.0 ea, $2.33/1ea", Decimal(3), "ea", "count"),  # 3 rolls, not 294 sheets
        ("$15.41/1kg $6.99/1lb", None, None, "unknown"),
        ("", None, None, "unknown"),
    ],
)
def test_parse_size(text, value, unit, family):
    s = parse_size(text)
    assert (s.value, s.unit, s.family) == (value, unit, family)


def test_retailer_unit_prices_convert_to_basis():
    rps = parse_retailer_unit_prices("$15.41/1kg $6.99/1lb")
    assert rps[0].family == "weight"
    assert rps[0].per_basis == Decimal("1.541")  # per 100 g
    assert abs(rps[1].per_basis - Decimal("1.541")) < Decimal("0.001")


def test_money():
    assert money("$6.29") == Decimal("6.29")
    assert money("5.97") == Decimal("5.97")
    assert money(None) is None
    assert money("$1,299.00") == Decimal("1299.00")


def test_every_varied_tile_gets_family_and_unit_price():
    for tile in load_fixture("tiles_varied.json"):
        p = from_search_tile(tile)
        assert p.retailer_product_id
        assert p.comparison_family in ("weight", "volume", "count"), tile["packageSizing"]
        assert p.unit_price is not None and p.unit_price > 0, tile["packageSizing"]


def test_sale_tile_prices():
    data = load_fixture("search_peanut_butter.json")
    tiles = [t for c in data["layout"]["sections"]["mainContentCollection"]["components"] for t in (c.get("data") or {}).get("productTiles") or []]
    sale = next(from_search_tile(t) for t in tiles if t["pricing"]["wasPrice"])
    assert sale.sale_price == sale.effective_price
    assert sale.regular_price > sale.effective_price
    assert sale.deal_type == "SALE"
    assert sale.discount_pct and sale.discount_pct > 0


def test_weight_priced_item_uses_per_kg_and_estimates_size():
    tile = next(t for t in load_fixture("tiles_varied.json") if t["pricingUnits"]["type"] == "SOLD_BY_EACH_PRICED_BY_WEIGHT")
    p = from_search_tile(tile)
    assert p.sold_by == "each_priced_by_weight"
    assert p.unit_price_basis == "100g"
    assert p.size_estimated
    # estimated pack weight * unit price ~= displayed estimated price
    assert abs(p.size_value / 100 * p.unit_price - p.effective_price) < Decimal("0.05")


def test_product_detail():
    p = from_product_detail(load_fixture("product_detail.json"))
    assert p.retailer_product_id == "20039581001_EA"
    assert p.brand == "Kraft"
    assert p.size_value == Decimal(1000) and p.comparison_family == "weight"
    assert p.effective_price == Decimal("5.97") and p.regular_price == Decimal("6.29")
    assert p.unit_price == Decimal("0.597")
    assert p.gtin


def test_cart_parsing():
    empty = cart_from_response(load_fixture("cart_empty.json"))
    assert empty.lines == [] and empty.store_id
    cart = cart_from_response(load_fixture("cart_populated.json"))
    assert len(cart.lines) == 2
    assert cart.quantity_of("20039581001_EA") == 2.0
    assert cart.quantity_of("nope") == 0
    updated = cart_from_response(load_fixture("cart_update.json"))  # {"cart": ..., "errors": []}
    assert updated.lines
