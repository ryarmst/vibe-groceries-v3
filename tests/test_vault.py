import base64

import pytest

from shopping.models import Secret
from shopping.secrets.vault import Vault, VaultError, load_master_key


def test_round_trip_status_and_write_only(session):
    v = Vault(b"k" * 32)
    v.set(session, "llm:gemini", "  AIzaSy-secret-value-1234  ", by="dev1")
    assert v.get(session, "llm:gemini") == "AIzaSy-secret-value-1234"
    row = session.get(Secret, "llm:gemini")
    assert "secret" not in row.ciphertext and row.last4 == "1234"
    st = {s["name"]: s for s in v.status(session)}
    assert st["llm:gemini"]["set"] and st["llm:gemini"]["last4"] == "1234" and "value" not in st["llm:gemini"]
    assert "retailer:access_token" not in st  # shown via the session status instead
    assert v.delete(session, "llm:gemini") and v.get(session, "llm:gemini") is None


def test_short_values_dont_leak_last4_and_unknown_names_refused(session):
    v = Vault(b"k" * 32)
    v.set(session, "notion", "short")
    assert session.get(Secret, "notion").last4 is None
    with pytest.raises(VaultError):
        v.set(session, "anything-else", "x" * 20)
    with pytest.raises(VaultError):
        v.set(session, "notion", "   ")


def test_wrong_key_or_moved_ciphertext_fails(session):
    Vault(b"a" * 32).set(session, "llm:openai", "sk-abcdefghijklmnop")
    with pytest.raises(VaultError):
        Vault(b"b" * 32).get(session, "llm:openai")
    # A ciphertext copied under another name doesn't decrypt (the name is authenticated).
    row = session.get(Secret, "llm:openai")
    session.add(Secret(name="llm:groq", nonce=row.nonce, ciphertext=row.ciphertext))
    with pytest.raises(VaultError):
        Vault(b"a" * 32).get(session, "llm:groq")


def test_master_key_loading(tmp_path):
    good = base64.b64encode(b"x" * 32).decode()
    assert load_master_key(good, None) == b"x" * 32
    with pytest.raises(VaultError):
        load_master_key(base64.b64encode(b"short").decode(), None)
    f = tmp_path / "master.key"
    k1 = load_master_key("", f)
    assert len(k1) == 32 and oct(f.stat().st_mode)[-3:] == "600" and load_master_key("", f) == k1
