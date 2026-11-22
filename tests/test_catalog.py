from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal

from shopping.core import catalog, service
from shopping.models import DealScan, Feedback, PastOrder, PastOrderLine, Product, utcnow
from shopping.retailer.normalize import NormalizedProduct


def np_(pid, name, price, regular=None, brand=None, deal=None, cats=None, **kw):
    p = NormalizedProduct(retailer_product_id=pid, name=name, brand=brand, effective_price=Decimal(str(price)),
                          regular_price=Decimal(str(regular if regular is not None else price)),
                          sale_price=Decimal(str(price)) if regular else None, deal_text=deal, categories=cats or [], **kw)
    return p


FOOD, MEAT, BEEF, DELI, DELIMEAT = ("1", "Food"), ("2", "Meat"), ("3", "Beef"), ("4", "Deli"), ("5", "Deli Meat")


def test_categories_and_current_price(session):
    obs = service.record_observation(session, np_("a_EA", "Ground beef", 8, 10, deal="SAVE $2", cats=[FOOD, MEAT, BEEF]),
                                     source="product", store_id="1234")
    p = obs.product
    assert (p.aisle, p.category_path, p.category_code) == ("Meat", "Food > Meat > Beef", "3")
    assert p.current_on_sale and float(p.current_price) == 8.0
    # A deal scan that also lists it under another aisle doesn't move it; the product page would.
    service.set_category(session, p, [FOOD, DELI, DELIMEAT], authoritative=False)
    assert p.aisle == "Meat"
    service.set_category(session, p, [FOOD, DELI, DELIMEAT])
    assert p.aisle == "Deli"
    # An old order price never replaces today's shelf price.
    service.record_observation(session, np_("a_EA", "Ground beef", 12), source="order", store_id=None, dedupe=False)
    assert float(p.current_price) == 8.0


def test_seeing_the_same_price_again_reconfirms_it(session):
    p = service.record_observation(session, np_("b_EA", "Eggs", 4.99, 6.98), source="search", store_id="1234").product
    p.current_observed_at = utcnow().replace(year=2020)
    service.record_observation(session, np_("b_EA", "Eggs", 4.99, 6.98), source="deal_scan", store_id="1234")  # deduped row
    assert p.current_observed_at.year == utcnow().year


class TreeClient:
    """Search facets like No Frills': filtering by a category lists its children with counts."""
    tree = {None: [("1", "Food", 3), ("9", "Household Supplies", 1)],
            "1": [("2", "Meat", 2), ("6", "Natural and Organic", 1)], "2": [("3", "Beef", 1), ("7", "Chicken & Turkey", 1)],
            "9": [("10", "Laundry Needs", 1)]}
    items = {"3": [np_("beef_EA", "Stewing beef", 9, 12)], "7": [np_("chk_EA", "Chicken thighs", 10, 14), np_("org_EA", "Organic chicken", 15, 18)],
             "6": [np_("org_EA", "Organic chicken", 15, 18)], "10": [np_("soap_EA", "Detergent", 8, 12)], None: []}

    def __init__(self):
        self.calls = []

    async def _search(self, term, filters, page, size):
        code = (filters.get("category") or [None])[0]
        self.calls.append((code, page, tuple(sorted(filters))))
        children = self.tree.get(code, [])
        values = [{"code": c, "name": n, "count": k} for c, n, k in children]
        products = [replace(p) for p in self.items.get(code, [])] if page == 1 else []
        return products, {"total_results": len(products), "filter_groups": [{"code": "category", "values": values}]}

    async def get_product(self, pid):
        raise AssertionError("not needed")


async def test_scan_walks_aisles_and_retires_ended_deals(session):
    @contextmanager
    def scope():
        yield session
        session.flush()

    stale = service.record_observation(session, np_("old_EA", "Ended deal", 1, 2), source="search", store_id="1234").product
    stale.current_observed_at = utcnow().replace(year=2020)
    client = TreeClient()
    await catalog.scan_deals(client, scope, store_id="1234")
    by = {p.retailer_product_id: p for p in session.query(Product)}
    assert by["beef_EA"].category_path == "Food > Meat > Beef"
    assert by["org_EA"].aisle == "Meat"  # real aisle wins over the catch-all, which is walked last
    assert by["soap_EA"].category_path == "Household Supplies > Laundry Needs"  # non-food: aisle level
    assert by["chk_EA"].current_on_sale and not by["old_EA"].current_on_sale
    assert session.query(DealScan).one().status == "done"
    tree = {d["name"]: d for d in catalog.aisle_tree(session)}
    assert tree["Food"]["on_sale"] == 3


def _bought(session, product):
    order = PastOrder(id="1", placed_at=utcnow())
    order.lines.append(PastOrderLine(position=0, product_id=product.id, retailer_product_id=product.retailer_product_id, name=product.name))
    session.add(order)
    session.flush()


def test_context_ranks_toward_household_and_skips_rejects(session):
    rec = lambda *a, **k: service.record_observation(session, np_(*a, **k), source="deal_scan", store_id="1234").product  # noqa: E731
    mine = rec("mine_EA", "Our usual hummus", 5, 6, cats=[FOOD, DELI, ("8", "Dips")])
    rec("same_kind_EA", "Other hummus", 4, 5, cats=[FOOD, DELI, ("8", "Dips")])
    rec("big_EA", "Random 60% off", 2, 5, cats=[FOOD, ("11", "Snacks"), ("12", "Candy")])
    nope = rec("nope_EA", "Removed twice", 1, 5, cats=[FOOD, ("11", "Snacks"), ("12", "Candy")])
    for _ in range(2):
        session.add(Feedback(action="removed", product_id=nope.id, note="no"))
    for i in range(3):  # flavours of one product line collapse to one entry
        rec(f"hd{i}_EA", f"Hot dogs flavour {i}", 6, 10, brand="Schneiders", cats=[FOOD, MEAT, ("13", "Hot Dogs")])
    _bought(session, mine)
    ctx = catalog.shopping_context(session)
    assert [x["product_id"] for x in ctx["your_items_on_sale"]] == ["mine_EA"]
    deli = ctx["top_deals_by_aisle"]["Deli"]
    assert deli[0]["product_id"] == "same_kind_EA"
    flat = [x["product_id"] for v in ctx["top_deals_by_aisle"].values() for x in v]
    assert "nope_EA" not in flat and "big_EA" in flat
    meat = ctx["top_deals_by_aisle"]["Meat"]
    assert len(meat) == 1 and meat[0]["other_variants_same_price"] == 2
    meals = catalog.shopping_context(session, focus="meals")
    assert "Protein" in meals["meal_deals"] and all(k != "Other" for k in meals["meal_deals"])
    found = catalog.search_catalog(session, query="hummus")
    assert found[0]["product_id"] == "mine_EA" and found[0]["bought_before"]["times"] == 1


def test_typed_substitution_note_means_subs_ok():
    from shopping.core.views import subs_allowed as _subs_allowed

    assert _subs_allowed("", "any brand", None) is True
    assert _subs_allowed("", "any brand", "any brand") is False  # pre-filled note, box unticked: off
    assert _subs_allowed("1", "", None) is True and _subs_allowed("", "", None) is False
