import base64
import json
import time

import httpx
import pytest

from conftest import load_fixture
from shopping.auth.session import AuthRequired
from shopping.retailer.client import PCExpressClient, RetailerError


def fake_jwt(exp_in: int = 3600, sub: str = "customer-1") -> str:
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'none'})}.{b64({'sub': sub, 'exp': int(time.time()) + exp_in})}.sig"


class StubAuth:
    """Stands in for the PC ID session: a fixed token, and a log of renewals the client asked for."""

    def __init__(self):
        self.token: str | None = None
        self.renewals = 0
        self.next_token: str | None = None

    def set_access_token(self, token: str) -> None:
        self.token = token.removeprefix("Bearer ")

    async def ensure_authenticated(self) -> str:
        if not self.token:
            raise AuthRequired("Not signed in.")
        part = self.token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))["sub"]

    async def get_headers(self) -> dict:
        await self.ensure_authenticated()
        return {"authorization": "Bearer " + self.token, "is-helios-account": "true"}

    async def token_rejected(self) -> None:
        self.renewals += 1
        if self.next_token:
            self.token = self.next_token


def make_client(tmp_path, handler) -> tuple[PCExpressClient, StubAuth]:
    auth = StubAuth()
    client = PCExpressClient(api_base="https://api.test/pcx-bff/api", api_key="k", banner="nofrills", store_id="1234", auth=auth)
    client.MIN_INTERVAL_SECONDS = 0
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=client._http.headers)  # noqa: SLF001
    return client, auth


def test_no_generic_request_surface():
    public = [n for n in dir(PCExpressClient) if not n.startswith("_")]
    assert sorted(public) == sorted(
        ["MIN_INTERVAL_SECONDS", "RETRY_DELAYS", "aclose", "get_cart", "get_deals", "get_past_order", "get_product", "list_past_orders", "remove_cart_item", "search_products", "set_cart_quantity", "set_cart_substitution"]
    )


@pytest.mark.parametrize("method, path", [
    ("POST", "/v1/carts/abc/checkout"),
    ("POST", "/v1/carts/abc/submit"),
    ("GET", "/v1/customers/abc/payment-cards"),
    ("POST", "/v1/fulfillment/timeslots"),
    ("POST", "/v1/carts/abc/merge"),
    ("DELETE", "/v1/carts/abc"),
])
async def test_allowlist_blocks_everything_else(tmp_path, method, path):
    client, _ = make_client(tmp_path, lambda r: httpx.Response(200, json={}))
    with pytest.raises(RetailerError, match="non-allowlisted"):
        await client._call("x", method, path)  # noqa: SLF001


async def test_search_parses_and_drops_sponsored(tmp_path):
    seen = {}

    def handler(request: httpx.Request):
        seen["body"] = json.loads(request.content)
        seen["headers"] = request.headers
        return httpx.Response(200, json=load_fixture("search_peanut_butter.json"))

    client, _ = make_client(tmp_path, handler)
    res = await client.search_products("peanut butter")
    assert seen["body"]["searchRelatedInfo"]["term"] == "peanut butter"
    assert seen["body"]["fulfillmentInfo"]["storeId"] == "1234"
    assert "authorization" not in seen["headers"]  # search is anonymous
    assert res["products"] and not any(p.sponsored for p in res["products"])
    assert len({p.retailer_product_id for p in res["products"]}) == len(res["products"])


async def test_cart_requires_auth(tmp_path):
    client, _ = make_client(tmp_path, lambda r: httpx.Response(200, json={}))
    with pytest.raises(AuthRequired):
        await client.get_cart()


async def test_set_quantity_sends_entries_with_bearer(tmp_path):
    calls = []

    def handler(request: httpx.Request):
        calls.append(request)
        if request.url.path.endswith("/carts") and request.method == "GET":
            return httpx.Response(200, json={"carts": [{"id": "cart-1"}]})
        return httpx.Response(200, json=load_fixture("cart_update.json"))

    client, auth = make_client(tmp_path, handler)
    auth.set_access_token(fake_jwt())
    await client.set_cart_quantity("20296401_EA", 2)
    post = calls[-1]
    assert post.method == "POST" and post.url.path.endswith("/v1/carts/cart-1")
    assert json.loads(post.content) == {"entries": {"20296401_EA": {"quantity": 2, "fulfillmentMethod": "pickup", "sellerId": "1234"}}}
    assert post.headers["authorization"].startswith("Bearer ")
    assert "/customers/customer-1/carts" in str(calls[0].url)


async def test_cart_errors_raise(tmp_path):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"carts": [{"id": "c"}]})
        return httpx.Response(200, json={"cart": {}, "errors": [{"code": "MAX_QTY"}]})

    client, auth = make_client(tmp_path, handler)
    auth.set_access_token(fake_jwt())
    with pytest.raises(RetailerError, match="MAX_QTY"):
        await client.set_cart_quantity("x_EA", 99)


async def test_401_renews_the_token_once_and_retries(tmp_path):
    good = fake_jwt(sub="customer-1") + "x"
    seen = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        if request.headers.get("authorization") != "Bearer " + good:
            return httpx.Response(401, json={})
        if request.url.path.endswith("/carts"):
            return httpx.Response(200, json={"carts": [{"id": "c"}]})
        return httpx.Response(200, json=load_fixture("cart_empty.json"))

    client, auth = make_client(tmp_path, handler)
    auth.set_access_token(fake_jwt())
    auth.next_token = good
    await client.get_cart()
    assert auth.renewals == 1 and seen[-1] == "Bearer " + good

    auth.next_token = fake_jwt(sub="customer-1") + "y"  # renewed, but No Frills still refuses it
    auth.set_access_token(fake_jwt())
    client._cart_id = None  # noqa: SLF001
    with pytest.raises(RetailerError, match="sign in again"):
        await client.get_cart()
    assert auth.renewals == 2  # only once per call


def _ppe_error(status=526):
    inner = json.dumps({"error_response": {"message": "Unexpected error from PPE", "details": {"error": ":unavailable", "status": status},
                                           "error_code": "PRICING_PROMO_ENGINE_ERROR"}})
    return httpx.Response(400, json={"errors": [{"message": inner}]})


async def test_upstream_outage_is_retried_then_reported_readably(tmp_path):
    calls = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"carts": [{"id": "c"}]})
        calls.append(1)
        return _ppe_error()

    client, auth = make_client(tmp_path, handler)
    client.RETRY_DELAYS = (0, 0, 0)
    auth.set_access_token(fake_jwt())
    from shopping.retailer.client import RetailerUnavailable

    with pytest.raises(RetailerUnavailable, match="pricing service is unavailable") as exc:
        await client.set_cart_quantity("x_EA", 1)
    assert len(calls) == 3 and exc.value.code == "PRICING_PROMO_ENGINE_ERROR"
    assert "{" not in str(exc.value)  # no raw JSON in the message


async def test_transient_error_then_success(tmp_path):
    calls = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"carts": [{"id": "c"}]})
        calls.append(1)
        return _ppe_error() if len(calls) == 1 else httpx.Response(200, json=load_fixture("cart_update.json"))

    client, auth = make_client(tmp_path, handler)
    client.RETRY_DELAYS = (0, 0, 0)
    auth.set_access_token(fake_jwt())
    cart = await client.set_cart_quantity("20296401_EA", 2)
    assert cart.lines and len(calls) == 2


async def test_cart_cache_is_per_customer(tmp_path):
    seen = []

    def handler(request):
        if request.url.path.endswith("/carts"):
            seen.append(request.url.path)
            cid = "cart-a" if "/customer-a/" in request.url.path else "cart-b"
            return httpx.Response(200, json={"carts": [{"id": cid}]})
        return httpx.Response(200, json=load_fixture("cart_empty.json"))

    client, auth = make_client(tmp_path, handler)
    auth.set_access_token(fake_jwt(sub="customer-a"))
    assert await client._current_cart_id() == "cart-a"  # noqa: SLF001
    assert await client._current_cart_id() == "cart-a"  # noqa: SLF001 - cached
    auth.set_access_token(fake_jwt(sub="customer-b"))
    assert await client._current_cart_id() == "cart-b"  # noqa: SLF001
    assert len(seen) == 2


@pytest.mark.parametrize("body", [
    {"errors": ["Cart is locked for editing"]},
    {"errors": [{"message": json.dumps({"error_response": "Cart is locked for editing"})}]},
    {"errors": [{"message": json.dumps({"error_response": {"message": "Cart is locked for editing", "details": "x"}})}]},
    {"code": "CART_LOCKED", "message": "Cart is locked for editing"},
    ["Cart is locked for editing"],
    "Cart is locked for editing",
])
async def test_unusual_error_shapes_are_reported_not_crashed(tmp_path, body):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"carts": [{"id": "c"}]})
        return httpx.Response(400, json=body)

    client, auth = make_client(tmp_path, handler)
    auth.set_access_token(fake_jwt())
    with pytest.raises(RetailerError, match="locked"):
        await client.set_cart_quantity("x_EA", 1)


def test_cart_parser_tolerates_string_fields():
    data = load_fixture("cart_populated.json")
    entry = data["orders"][0]["entries"][0]
    entry["offer"]["vendor"] = "1234"
    entry["substitutionPreference"] = "NONE"
    data["orders"][0]["fulfillment"] = "pickup"
    from shopping.retailer.normalize import cart_from_response

    line = cart_from_response(data).lines[0]
    assert line.seller_id == "1234" and line.substitution_allowed is None


async def test_new_item_with_substitution_is_added_then_set(tmp_path):
    from shopping.retailer.normalize import SubstitutionSpec

    in_cart, sent = set(), []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"carts": [{"id": "c"}]})
        entries = json.loads(request.content)["entries"]
        sent.append(entries)
        pid, entry = next(iter(entries.items()))
        if "substitutionAllowed" in entry and pid not in in_cart:
            inner = json.dumps({"error_response": {"message": "Item was not found.", "error_code": "ITEM_NOT_FOUND"}})
            return httpx.Response(400, json={"errors": [{"message": inner}]})
        in_cart.add(pid)
        return httpx.Response(200, json=load_fixture("cart_update.json"))

    client, auth = make_client(tmp_path, handler)
    auth.set_access_token(fake_jwt())
    await client.set_cart_quantity("new_EA", 2, substitution=SubstitutionSpec(allowed=True, comment="any brand"))
    assert [("substitutionAllowed" in e["new_EA"]) for e in sent] == [True, False, True]
    assert sent[-1]["new_EA"]["substitutionComment"] == "any brand"


async def test_cart_error_message_is_readable(tmp_path):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"carts": [{"id": "c"}]})
        return httpx.Response(200, json={"cart": {}, "errors": [{"code": "0014", "requestId": None, "message": "Product is out of stock."}]})

    client, auth = make_client(tmp_path, handler)
    auth.set_access_token(fake_jwt())
    with pytest.raises(RetailerError) as exc:
        await client.set_cart_quantity("x_KG", 1)
    assert str(exc.value) == "No Frills didn't add it: Product is out of stock." and exc.value.code == "0014"
