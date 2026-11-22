import logging

import pytest

from sqlalchemy import create_engine, inspect

from shopping.logging_setup import RedactingFilter, redact
from shopping.models import Base


def test_redaction():
    jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlc2lnbmF0dXJl"
    assert jwt not in redact(f"authorization: Bearer {jwt}")
    assert "s3cret" not in redact('{"password":"s3cret","email":"a@b"}')
    assert "abc123" not in redact("x-apikey: abc123")
    assert "rt-value" not in redact("grant_type=refresh_token&refresh_token=rt-value")
    record = logging.LogRecord("t", logging.DEBUG, __file__, 1, "token=%s", (jwt,), None)
    RedactingFilter().filter(record)
    assert jwt not in record.getMessage()


def test_migration_matches_models(tmp_path):
    from shopping.database import run_migrations

    url = f"sqlite:///{tmp_path / 'm.db'}"
    run_migrations(url)
    migrated = inspect(create_engine(url))
    for table in Base.metadata.sorted_tables:
        cols = {c["name"] for c in migrated.get_columns(table.name)}
        assert cols == {c.name for c in table.columns}, table.name


def test_no_purchase_tools():
    from shopping.agent.shopping_tools import tool_names

    names = tool_names()
    assert "shopping_search_many" in names and "shopping_apply_approved" in names
    for banned in ("checkout", "payment", "purchase", "timeslot", "http", "request", "place", "submit_order", "create_order"):
        assert not any(banned in n for n in names), banned
    # The only order-related tools are read-only views of past orders.
    assert {n for n in names if "order" in n} == {"shopping_get_past_orders", "shopping_get_past_order"}


async def test_tool_errors_reach_the_agent():
    from mcp.server.mcpserver.exceptions import ToolError

    from shopping.database import run_migrations
    from shopping.agent.shopping_tools import shopping_get_run

    run_migrations()  # the tool uses the app engine (a temp DB under tests)
    with pytest.raises(ToolError, match="No shopping run"):
        await shopping_get_run(run_id="does-not-exist")


async def test_keepalive_reports_a_lapsed_sign_in(monkeypatch):
    from shopping import context
    from shopping.auth.session import AuthRequired, AuthStatus
    from shopping.core import inbox as inbox_mod
    from shopping.database import session_scope
    from shopping.jobs import scheduler

    monkeypatch.setattr(context.auth, "status", lambda: AuthStatus("connected", None, None, None, "c"))

    async def lapsed():
        raise AuthRequired("PC ID sign-in expired or was revoked; sign in again.")
    monkeypatch.setattr(context.auth, "ensure_authenticated", lapsed)
    assert (await scheduler.keep_session_alive()).startswith("signed out")
    with session_scope() as s:
        titles = [i["title"] for i in inbox_mod.items(s, session_state="connected")]
    assert "No Frills sign-in ended" in titles
