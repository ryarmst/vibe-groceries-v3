import base64
import json
import os
import time
import urllib.parse

import httpx
import pytest

from shopping.auth.session import AuthRequired, AuthSession
from shopping.pcid.oauth import AuthError, NeedsLogin, PcidAuth, Unavailable, code_from_redirect
from shopping.pcid.store import SecretStore, StateError


def _jwt(sub: str) -> str:
    part = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).decode().rstrip("=")
    return f"h.{part}.s"


class FakePcid:
    """Rotating single-use refresh tokens, like IDCS."""

    def __init__(self):
        self.valid_refresh = set()
        self.codes = {}
        self.n = 0
        self.token_calls = []
        self.api_tokens = set()

    def issue(self) -> dict:
        self.n += 1
        rt, at = f"rt{self.n}", _jwt("cust-1") + str(self.n)
        self.valid_refresh.add(rt)
        self.api_tokens.add(at)
        return {"access_token": at, "refresh_token": rt, "expires_in": 3600}

    def handler(self, req: httpx.Request) -> httpx.Response:
        if req.url.host == "accounts.pcid.ca" and req.url.path.endswith("/token"):
            f = dict(urllib.parse.parse_qsl(req.content.decode()))
            self.token_calls.append(f)
            assert req.headers["relying-party"] == "pcexpress-android"
            if f.get("client_secret") != "sec":
                return httpx.Response(401, json={"error": "invalid_client"})
            if f["grant_type"] == "authorization_code":
                if self.codes.pop(f["code"], None) != f["code_verifier"]:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(200, json=self.issue())
            if f["refresh_token"] not in self.valid_refresh:
                return httpx.Response(400, json={"error": "invalid_grant"})
            self.valid_refresh.discard(f["refresh_token"])  # single use
            return httpx.Response(200, json=self.issue())
        if req.url.host == "api.pcexpress.ca":
            tok = req.headers.get("authorization", "").removeprefix("Bearer ")
            if tok not in self.api_tokens:
                return httpx.Response(401, json={})
            return httpx.Response(200, json={"orderHistory": [{"id": "1"}, {"id": "2"}]})
        return httpx.Response(404)


@pytest.fixture
def env(tmp_path):
    return {"secret": "sec", "dir": tmp_path, "key": os.urandom(32)}


@pytest.fixture
def fake():
    return FakePcid()


def make(env, fake):
    return PcidAuth(client_secret=env["secret"], state_dir=env["dir"], key=env["key"],
                    transport=httpx.MockTransport(fake.handler))


async def sign_in(auth: PcidAuth, fake: FakePcid, by: str | None = None) -> None:
    url = auth.start_login()
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert q["code_challenge_method"] == "S256" and "offline_access" in q["scope"]
    verifier = auth.pending.read()["verifier"]
    fake.codes["code123"] = verifier
    await auth.finish_login(f"com.loblaw.pcx://pcx-android/login/appredirect?code=code123&state={q['state']}", by=by)


def test_code_from_redirect_forms():
    assert code_from_redirect("com.loblaw.pcx://x/y?code=abc&state=s1") == ("abc", "s1")
    inner = urllib.parse.quote("com.loblaw.pcx://x/y?code=abc&state=s1", safe="")
    assert code_from_redirect(f"https://accounts.pcid.ca/login/success?redirectURL={inner}") == ("abc", "s1")
    assert code_from_redirect("  abc123 ") == ("abc123", None)
    with pytest.raises(AuthError):
        code_from_redirect("com.loblaw.pcx://x/y?error=access_denied")
    with pytest.raises(AuthError):
        code_from_redirect("https://example.com/no/code")


async def test_login_then_refresh_rotates_and_persists(env, fake):
    auth = make(env, fake)
    await sign_in(auth, fake)
    st = auth.status()
    assert st.signed_in and st.customer_id == "cust-1"
    assert not auth.pending.path.exists()
    first = await auth.access_token()
    assert first == await auth.access_token()  # cached, no refresh
    assert len(fake.token_calls) == 1

    second = await auth.access_token(rejected=first)
    assert second != first and fake.token_calls[-1]["refresh_token"] == "rt1"

    # A new process (fresh object) picks up the rotated refresh token from disk.
    other = make(env, fake)
    third = await other.access_token(rejected=second)
    assert fake.token_calls[-1]["refresh_token"] == "rt2" and third != second


async def test_expired_access_token_refreshes(env, fake):
    auth = make(env, fake)
    await sign_in(auth, fake)
    state = auth.tokens.read()
    auth.tokens.write({**state, "expires_at": time.time() + 30})
    auth._cache = None
    await auth.access_token()
    assert fake.token_calls[-1]["grant_type"] == "refresh_token"


async def test_second_process_reuses_fresh_token(env, fake):
    a, b = make(env, fake), make(env, fake)
    await sign_in(a, fake)
    tok = await a.access_token(rejected="old")  # forces a refresh in a
    calls = len(fake.token_calls)
    assert await b.access_token() == tok  # b reads it from disk instead of spending the refresh token
    assert len(fake.token_calls) == calls


async def test_revoked_refresh_token_needs_login(env, fake):
    auth = make(env, fake)
    await sign_in(auth, fake)
    fake.valid_refresh.clear()
    with pytest.raises(NeedsLogin):
        await auth.access_token(rejected=(await auth.access_token()))


async def test_wrong_state_and_reused_code(env, fake):
    auth = make(env, fake)
    auth.start_login()
    with pytest.raises(AuthError, match="different sign-in"):
        await auth.finish_login("com.loblaw.pcx://x?code=c&state=nope")
    with pytest.raises(AuthError, match="sign-in code"):
        await auth.finish_login("com.loblaw.pcx://x?code=unknown&state=" + auth.pending.read()["state"])


async def test_finish_without_start(env, fake):
    with pytest.raises(AuthError, match="No sign-in in progress"):
        await make(env, fake).finish_login("com.loblaw.pcx://x?code=c")


async def test_bad_client_secret(env, fake):
    auth = make({**env, "secret": "wrong"}, fake)
    auth.start_login()
    with pytest.raises(AuthError, match="app client"):
        await auth.finish_login("plaincode")


async def test_state_is_encrypted_and_key_bound(env, fake, tmp_path):
    auth = make(env, fake)
    await sign_in(auth, fake)
    raw = auth.tokens.path.read_bytes()
    assert b"rt1" not in raw and b"access_token" not in raw
    assert oct(auth.tokens.path.stat().st_mode & 0o777) == "0o600"
    wrong = SecretStore(auth.tokens.path, os.urandom(32))
    with pytest.raises(StateError):
        wrong.read()


async def test_session_adapter(env, fake):
    auth = make(env, fake)
    session = AuthSession(auth)
    assert session.status().state == "required"
    with pytest.raises(AuthRequired, match="Sign in under Settings"):
        await session.get_headers()
    await sign_in(auth, fake, by="Sam")
    st = session.status()
    assert st.state == "connected" and st.customer_id == "cust-1" and st.signed_in_by == "Sam"
    h = await session.get_headers()
    assert h["authorization"].startswith("Bearer ") and h["is-helios-account"] == "true"
    before = len(fake.token_calls)
    await session.token_rejected()
    assert len(fake.token_calls) == before + 1 and (await session.get_headers()) != h


async def test_pcid_outage_is_not_a_sign_out(env, fake):
    auth = make(env, fake)
    await sign_in(auth, fake)
    auth.tokens.write({**auth.tokens.read(), "expires_at": 0})
    auth._cache = None
    auth._transport = httpx.MockTransport(lambda r: httpx.Response(503, text="down"))
    with pytest.raises(Unavailable):
        await auth.access_token()
    assert auth.status().signed_in  # refresh token kept


async def test_logout_clears(env, fake):
    auth = make(env, fake)
    await sign_in(auth, fake)
    await auth.logout()
    assert not auth.status().signed_in
    with pytest.raises(NeedsLogin):
        await auth.access_token()
