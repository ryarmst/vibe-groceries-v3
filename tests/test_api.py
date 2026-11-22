import base64
import json
import time

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

from shopping.api import deps
from shopping.auth.access import AccessVerifier
from shopping.database import session_scope

TEAM, AUD = "team.cloudflareaccess.com", "aud-tag-123"
USERS = {"alex@example.com": "Alex", "sam@example.com": "Sam"}
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _jwks(request):
    n = _KEY.public_key().public_numbers()
    jwk = {"kty": "RSA", "kid": "k1", "alg": "RS256", "e": _b64(n.e.to_bytes(3, "big")), "n": _b64(n.n.to_bytes(256, "big"))}
    assert request.url == f"https://{TEAM}/cdn-cgi/access/certs"
    return httpx.Response(200, json={"keys": [jwk]})


def access_jwt(email="alex@example.com", *, key=None, kid="k1", aud=AUD, iss=f"https://{TEAM}", exp_in=3600) -> str:
    header = _b64(json.dumps({"alg": "RS256", "kid": kid}).encode())
    claims = _b64(json.dumps({"email": email, "aud": [aud], "iss": iss, "exp": int(time.time()) + exp_in,
                              "iat": int(time.time())}).encode())
    sig = (key or _KEY).sign(f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{claims}.{_b64(sig)}"


def signed_in(email="alex@example.com") -> dict:
    return {"cf-access-jwt-assertion": access_jwt(email), "x-vibe": "1"}


@pytest.fixture(scope="module")
def client():
    from shopping.app import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def access(monkeypatch):
    deps.api_limiter._hits.clear()
    monkeypatch.setattr(deps, "_verifier", AccessVerifier(TEAM, AUD, USERS, transport=httpx.MockTransport(_jwks)))


def test_everything_needs_access(client):
    for path in ("/api/inbox", "/api/settings", "/api/runs", "/api/chat/threads", "/api/cart", "/api/retailer/session"):
        assert client.get(path).status_code == 401, path
    assert client.get("/healthz").status_code == 200


@pytest.mark.parametrize("token, status", [
    (lambda: access_jwt("someone@else.com"), 403),  # Access let them through, the app doesn't
    (lambda: access_jwt(key=_OTHER), 401),  # forged signature
    (lambda: access_jwt(kid="nope"), 401),
    (lambda: access_jwt(aud="other-app"), 401),
    (lambda: access_jwt(iss="https://evil.cloudflareaccess.com"), 401),
    (lambda: access_jwt(exp_in=-600), 401),
    (lambda: "garbage", 401),
])
def test_bad_access_tokens_are_refused(client, token, status):
    assert client.get("/api/inbox", headers={"cf-access-jwt-assertion": token()}).status_code == status


def test_access_user_is_recorded_and_cookie_works(client):
    r = client.get("/api/me", headers=signed_in("Sam@Example.com".lower()))
    assert r.status_code == 200 and r.json()["name"] == "Sam"
    client.cookies.set("CF_Authorization", access_jwt())
    try:
        assert client.get("/api/me").json()["name"] == "Alex"
    finally:
        client.cookies.clear()


def test_writes_need_the_app_header_and_same_origin(client):
    h = signed_in()
    no_header = {"cf-access-jwt-assertion": h["cf-access-jwt-assertion"]}
    assert client.put("/api/settings/budget", json={"monthly_usd": 9}, headers=no_header).status_code == 403
    evil = {**h, "origin": "https://evil.example"}
    assert client.put("/api/settings/budget", json={"monthly_usd": 9}, headers=evil).status_code == 403
    same = {**h, "origin": "http://testserver"}
    assert client.put("/api/settings/budget", json={"monthly_usd": 9}, headers=same).status_code == 200


def test_dev_user_only_on_loopback(monkeypatch):
    import dataclasses

    from shopping.config import settings
    monkeypatch.setattr(deps, "settings", dataclasses.replace(settings, dev_user="d@x", host="127.0.0.1"))
    assert deps.dev_mode()
    monkeypatch.setattr(deps, "settings", dataclasses.replace(settings, dev_user="d@x", host="0.0.0.0"))
    assert not deps.dev_mode()


def test_pcid_signin_flow_never_returns_tokens(client, monkeypatch):
    from shopping import context

    h = signed_in()
    st = client.get("/api/retailer/session", headers=h).json()
    assert st["state"] in ("required", "connected")
    url = client.post("/api/retailer/signin/start", headers=h).json()
    assert (url["url"] or "").startswith("https://accounts.pcid.ca/oauth2/v1/authorize") or url.get("error")
    r = client.post("/api/retailer/signin/finish", json={"redirect": "com.loblaw.pcx://x?code=abc&state=wrong"}, headers=h).json()
    assert r["ok"] is False and r["error"]


def test_secrets_are_write_only(client):
    h = signed_in()
    secret = "AIzaSyD-not-a-real-key-9876"
    r = client.put("/api/settings/secrets/llm:gemini", json={"value": secret}, headers=h)
    assert r.status_code == 200 and secret not in r.text
    st = {s["name"]: s for s in client.get("/api/settings/secrets", headers=h).json()["secrets"]}
    assert st["llm:gemini"]["set"] and st["llm:gemini"]["last4"] == "9876"
    assert secret not in client.get("/api/settings", headers=h).text
    assert client.put("/api/settings/secrets/bogus", json={"value": "x" * 20}, headers=h).status_code == 400


def test_settings_validation_and_budget(client):
    h = signed_in()
    r = client.put("/api/settings/budget", json={"monthly_usd": 25}, headers=h)
    assert r.status_code == 200 and r.json()["budget"]["monthly_usd"] == 25
    assert client.put("/api/settings/budget", json={"monthly_usd": -1}, headers=h).status_code == 400
    assert client.put("/api/settings/llm", json={"tiers": {"fast": {"provider": "nope", "model": "x"}}}, headers=h).status_code == 400
    assert client.get("/api/llm/usage", headers=h).json()["summary"]["monthly_cap_usd"] == 25


def test_run_view_over_the_api(client):
    import asyncio

    from shopping.core import service
    from test_service import FakeClient

    h = signed_in()
    fake = FakeClient()

    async def build():
        with session_scope() as s:
            run = service.create_run(s, summary="API test", store_id="1234")
            ids = list(fake.products)
            await service.add_choice_group(s, fake, run.id, label="Peanut butter", recommended_index=0, options=[
                {"product_id": ids[0], "quantity": 1, "reasons": ["grocery_list"]}, {"product_id": ids[1], "quantity": 1}])
            await service.add_selection(s, fake, run.id, product_id=ids[2], reasons=["good_deal"], requirement="Jam")
            service.submit_run_for_review(s, run.id)
            return run.id

    run_id = asyncio.run(build())
    view = client.get(f"/api/runs/{run_id}", headers=h).json()
    assert view["summary"] == "API test" and len(view["cards"]) == 2
    group = next(c for c in view["cards"] if c["kind"] == "group")
    assert len(group["options"]) == 2 and group["main"]["product"]["name"]
    single = next(c for c in view["cards"] if c["kind"] == "single")
    r = client.post(f"/api/runs/{run_id}/selections/{single['main']['selection_id']}/triage",
                    json={"decision": "remove", "note": "have two jars"}, headers=h)
    assert r.status_code == 200
    fb = client.get("/api/history/feedback", headers=h).json()["records"]
    assert fb[0]["note"] == "have two jars" and fb[0]["device_id"]  # attributed to the user


def test_cart_add_adds_on_top(client, monkeypatch):
    from shopping import context
    from test_service import FakeClient

    h = signed_in()
    fake = FakeClient()
    pid = list(fake.products)[0]
    fake.cart[pid] = 2
    monkeypatch.setattr(context, "client", lambda: fake)
    r = client.post(f"/api/cart/{pid}/add", json={"quantity": 1}, headers=h)
    assert r.status_code == 200 and r.json()["quantity"] == 3 and fake.cart[pid] == 3
    assert client.post(f"/api/cart/{pid}/add", json={"quantity": 0}, headers=h).status_code == 400


def test_web_app_is_served_with_a_strict_csp(client):
    r = client.get("/")
    assert r.status_code == 200 and '<script type="module" src="/static/app.js">' in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe" not in csp
    assert client.get("/static/views/quick.js").status_code == 200
    assert client.get("/static/../app.py").status_code == 404
