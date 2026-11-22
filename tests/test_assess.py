from datetime import timedelta
from decimal import Decimal

import httpx
import pytest

from shopping.core import assess
from shopping.core import service
from shopping.models import PastOrder, PastOrderLine, Product, TopDeal, utcnow
from shopping.notify import push
from shopping.retailer.normalize import NormalizedProduct

FOOD, DELI, DIPS = ("1", "Food"), ("4", "Deli"), ("8", "Dips")


def obs(session, pid, name, price, regular, *, days_ago=0, cats=None):
    np = NormalizedProduct(retailer_product_id=pid, name=name, effective_price=Decimal(str(price)), regular_price=Decimal(str(regular)),
                           sale_price=Decimal(str(price)) if price < regular else None, deal_text="SALE" if price < regular else None,
                           categories=cats or [FOOD, DELI, DIPS])
    o = service.record_observation(session, np, source="deal_scan", store_id="1234", dedupe=False)
    o.observed_at = utcnow() - timedelta(days=days_ago)
    o.product.current_observed_at = utcnow()
    session.flush()
    return o.product


def bought(session, *products):
    order = PastOrder(id=str(len(session.query(PastOrder).all()) + 1), placed_at=utcnow() - timedelta(days=10))
    for i, p in enumerate(products):
        order.lines.append(PastOrderLine(position=i, product_id=p.id, retailer_product_id=p.retailer_product_id, name=p.name))
    session.add(order)
    session.flush()


def test_assessment_levels_dismissals_and_retirement(session):
    hummus = obs(session, "h_EA", "Hummus", 6.0, 6.0, days_ago=30)
    obs(session, "h_EA", "Hummus", 4.0, 6.0)                       # bought, 33% off + lowest over 30 days → stock up
    yeast = obs(session, "y_EA", "Yeast", 5.0, 5.0, days_ago=3)
    obs(session, "y_EA", "Yeast", 4.8, 5.0)                        # "lowest" over 3 days, 4% off → nothing
    eggs = obs(session, "e_EA", "Eggs", 5.0, 7.0)                   # bought, 28% off → great
    other = obs(session, "o_EA", "Other dip", 8.0, 8.0, days_ago=20)
    obs(session, "o_EA", "Other dip", 4.0, 8.0)                     # same category, 50% off, lowest → great
    meh = obs(session, "m_EA", "Meh dip", 9.0, 10.0)                # 10% off, not bought → nothing
    bought(session, hummus, eggs, yeast)
    r = assess.assess(session)
    deals = {d.product.name: d for d in session.query(TopDeal).filter_by(added_by="auto", status="active")}
    assert set(deals) == {"Hummus", "Eggs", "Other dip"}  # not Meh dip (10% off, never bought) or Yeast (short history)
    assert deals["Hummus"].rating == 3 and deals["Hummus"].headline == "Lowest we've seen"
    assert deals["Eggs"].rating == 2 and deals["Eggs"].headline == "28.6% off" and "bought it 1×" in deals["Eggs"].why
    assert [a.product.name for a in r["stock_up_alerts"]] == ["Hummus"]
    # Run again: no repeat alert; dismissing keeps it off; a deal that ends retires.
    assert assess.assess(session)["stock_up_alerts"] == []
    deals["Eggs"].status = "dismissed"
    obs(session, "o_EA", "Other dip", 8.0, 8.0)  # back to regular price
    other.current_on_sale = False
    r = assess.assess(session)
    active = {d.product.name for d in session.query(TopDeal).filter_by(added_by="auto", status="active")}
    assert active == {"Hummus"} and r["retired"] == 1


async def test_push(monkeypatch):
    sent = []

    def handler(request):
        sent.append((str(request.url), request.headers["title"], request.headers["priority"], request.content.decode()))
        return httpx.Response(200)

    monkeypatch.setattr(push, "topic_url", lambda: "https://ntfy.example/vibe-x9")
    assert await push.send("2 stock-up deals", "• Hummus: $3.00", transport=httpx.MockTransport(handler))
    assert sent == [("https://ntfy.example/vibe-x9", "2 stock-up deals", "4", "• Hummus: $3.00")]
    monkeypatch.setattr(push, "topic_url", lambda: None)
    assert await push.send("x", "y") is False  # not configured: quietly nothing


def test_insecure_topic_refused(monkeypatch):
    from shopping import context
    from shopping.database import session_scope

    with session_scope() as s:
        context.vault.set(s, push.SECRET, "http://ntfy.example/plaintext-topic")
    with pytest.raises(push.NotifyError):
        push.topic_url()
    with session_scope() as s:
        context.vault.delete(s, push.SECRET)


def test_topic_forms():
    assert push.normalize_topic("vibe-groceries-k3j9") == "https://ntfy.sh/vibe-groceries-k3j9"
    assert push.normalize_topic("ntfy.sh/abc") == "https://ntfy.sh/abc"
    assert push.normalize_topic("https://ntfy.example.org/abc") == "https://ntfy.example.org/abc"
    assert push.normalize_topic("  ") is None
    with pytest.raises(push.NotifyError):
        push.normalize_topic("http://ntfy.sh/abc")
