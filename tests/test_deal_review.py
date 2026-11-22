import json
from decimal import Decimal

import pytest

from shopping.agent import deal_review
from shopping.agent.providers import registry as providers
from shopping.agent.providers.base import Done, ProviderError, TextDelta, Usage
from shopping.core import service
from shopping.database import session_scope
from shopping.models import TopDeal, utcnow
from shopping.retailer.normalize import NormalizedProduct


class Scripted:
    name = "gemini"

    def __init__(self, replies):
        self.replies, self.sent = list(replies), []

    async def stream(self, *, system, messages, tools, model, temperature, max_tokens):
        self.sent.append(json.loads(messages[0]["content"]))
        yield TextDelta(self.replies.pop(0))
        yield Usage(5000, 200)
        yield Done()


def seed():
    with session_scope() as s:
        s.query(TopDeal).delete()
        for pid, name, price, reg, cats in [
            ("rv_salmon", "Fresh Salmon", 14.95, 20.94, [("1", "Food"), ("2", "Fish & Seafood"), ("3", "Salmon")]),
            ("rv_okra", "Okra", 1.5, 3.0, [("1", "Food"), ("4", "Fruits & Vegetables"), ("5", "Fresh Vegetables")]),
            ("rv_chips", "Chips", 2.0, 4.0, [("1", "Food"), ("6", "Snacks, Chips & Candy"), ("7", "Chips")]),
            ("rv_tiny", "Tiny discount", 9.5, 10.0, [("1", "Food"), ("8", "Pantry"), ("9", "Rice")]),
        ]:
            np = NormalizedProduct(retailer_product_id=pid, name=name, effective_price=Decimal(str(price)), regular_price=Decimal(str(reg)),
                                   sale_price=Decimal(str(price)), deal_text="SALE", categories=cats)
            o = service.record_observation(s, np, source="deal_scan", store_id="1234")
            o.product.current_observed_at = utcnow()


async def test_review_picks_only_candidates_and_rotates(monkeypatch):
    seed()
    first = json.dumps({"picks": [
        {"product_id": "rv_salmon", "rating": 3, "headline": "Salmon at a low", "why": "29% off; freeze portions.", "for": "Salmon"},
        {"product_id": "rv_okra", "rating": 2, "headline": "Try okra", "why": "Half price; great in curry."},
        {"product_id": "made_up", "rating": 3, "headline": "Invented", "why": "x"}]})
    second = 'Sure! {"picks": [{"product_id": "rv_okra", "rating": 2, "headline": "Still okra", "why": "Half price."}]}'
    fake = Scripted([first, second])
    monkeypatch.setattr(providers, "build", lambda session, vault, name, transport=None: fake)
    r = await deal_review.review()
    assert r["picks"] == 2 and len(r["alerts"]) == 1
    sent = fake.sent[0]
    ids = [c["product_id"] for c in sent["candidates"]]
    assert "rv_salmon" in ids and "rv_tiny" not in ids  # 5% off and not lowest seen: not a candidate
    with session_scope() as s:
        picks = {d.product.retailer_product_id: d for d in s.query(TopDeal).filter_by(added_by="review", status="active")}
        assert set(picks) == {"rv_salmon", "rv_okra"} and picks["rv_salmon"].rating == 3 and picks["rv_salmon"].relevant_to == "Salmon"
    # Next review: salmon not repeated → retired; okra kept, no new alert.
    r = await deal_review.review()
    assert r["picks"] == 1 and r["alerts"] == []
    with session_scope() as s:
        active = {d.product.retailer_product_id for d in s.query(TopDeal).filter_by(added_by="review", status="active")}
        assert active == {"rv_okra"}
        assert s.query(TopDeal).filter_by(added_by="review", status="retired").count() == 1


async def test_dismissed_deals_are_not_offered_again(monkeypatch):
    seed()
    with session_scope() as s:
        from shopping.models import Product
        chips = s.query(Product).filter_by(retailer_product_id="rv_chips").one()
        s.add(TopDeal(product_id=chips.id, headline="x", status="dismissed", added_by="review", response_note="we don't buy chips"))
    fake = Scripted(['{"picks": []}'])
    monkeypatch.setattr(providers, "build", lambda session, vault, name, transport=None: fake)
    r = await deal_review.review()
    assert r["picks"] == 0
    assert "rv_chips" not in [c["product_id"] for c in fake.sent[0]["candidates"]]
    assert any("chips" in (t["note"] or "") for t in fake.sent[0]["household"]["turned_down"])


async def test_no_llm_configured_is_skipped(monkeypatch):
    seed()

    def no_key(session, vault, name, transport=None):
        raise ProviderError("No API key set for gemini.")

    monkeypatch.setattr(providers, "build", no_key)
    r = await deal_review.review()
    assert r["picks"] == 0 and "No API key" in r["skipped"]
