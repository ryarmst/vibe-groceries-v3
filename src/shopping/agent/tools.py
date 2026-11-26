"""Tools the agent can call, each with a permission tier.

Tiers (see docs/architecture.md):
  read     run immediately
  safe     run immediately; logged; reversible (stage a run, create a suggested meal, flag a deal)
  confirm  stop and ask a person (pantry / grocery list / Cooking edits); never run in proactive mode
There is deliberately no tier for checkout, payment, timeslots or fetching arbitrary URLs: no such tools exist.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from pydantic import ValidationError

from shopping.agent.providers.base import ToolSpec
from shopping.auth.session import AuthRequired
from shopping.core.service import ShoppingError
from shopping.database import session_scope
from shopping.models import AgentAudit
from shopping.notion.client import NotionError
from shopping.retailer.client import RetailerError
from shopping.secrets.vault import VaultError

log = logging.getLogger(__name__)

READ, SAFE, CONFIRM = "read", "safe", "confirm"
MAX_RESULT_CHARS = 14000


@dataclass
class ToolContext:
    thread_id: str | None = None
    device_id: str | None = None
    proactive: bool = False  # running from a schedule: no confirm-tier tools


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    tier: str
    run: Callable[[dict, ToolContext], Awaitable[Any]]
    preview: Callable[[dict], str] | None = None  # human-readable summary for confirmation cards
    tags: set[str] = field(default_factory=set)

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.parameters)


class Registry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def add(self, tool: Tool) -> None:
        if tool.tier not in (READ, SAFE, CONFIRM):
            raise ValueError(f"{tool.name}: bad tier {tool.tier}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def select(self, names: list[str]) -> list[Tool]:
        return [self._tools[n] for n in names if n in self._tools]


registry = Registry()


def _redact_args(args: dict) -> str:
    text = json.dumps(args, default=str)[:4000]
    return text


def preview_text(tool: Tool, args: dict) -> str:
    if tool.preview:
        try:
            return tool.preview(args)
        except Exception:  # noqa: BLE001
            pass
    return f"{tool.name}: " + ", ".join(f"{k}={json.dumps(v, default=str)[:80]}" for k, v in args.items())


def audit(*, tool: str, tier: str, args: dict | None, outcome: str, detail: str | None, ctx: ToolContext, ms: int | None = None) -> None:
    with session_scope() as session:
        session.add(AgentAudit(thread_id=ctx.thread_id, tool=tool, tier=tier, arguments=_redact_args(args or {}), outcome=outcome,
                               detail=(detail or "")[:2000] or None, duration_ms=ms, device_id=ctx.device_id))


def _shorten(result: Any) -> str:
    text = result if isinstance(result, str) else json.dumps(result, default=str, ensure_ascii=False)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"\n…[truncated {len(text) - MAX_RESULT_CHARS} characters; narrow the query for more]"
    return text


def _types(schema: dict) -> set[str]:
    t = schema.get("type")
    out = set(t) if isinstance(t, list) else ({t} if t else set())
    for alt in schema.get("anyOf") or schema.get("oneOf") or []:
        out |= _types(alt)
    return out


def coerce(value: Any, schema: dict | None, defs: dict | None = None) -> Any:
    """Undo a common model mistake: sending an object or list as a JSON *string* (e.g. a list of recipes where each
    recipe is '{"name": …}' in quotes). Only strings where the schema expects an object or array are parsed."""
    if not isinstance(schema, dict):
        return value
    defs = defs or {}
    if "$ref" in schema:
        schema = defs.get(schema["$ref"].rsplit("/", 1)[-1], {})
    for alt in schema.get("anyOf") or schema.get("oneOf") or []:
        if isinstance(alt, dict) and "$ref" in alt:
            schema = {**schema, **defs.get(alt["$ref"].rsplit("/", 1)[-1], {})}
    kinds = _types(schema)
    if isinstance(value, str) and kinds & {"object", "array"} and value.strip()[:1] in ("{", "["):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    if isinstance(value, list):
        return [coerce(v, schema.get("items"), defs) for v in value]
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        return {k: coerce(v, props.get(k), defs) for k, v in value.items()}
    return value


async def execute(tool: Tool, args: dict, ctx: ToolContext) -> tuple[bool, str]:
    """Run a tool. Returns (ok, result text for the model). Errors the model can act on are returned, not raised."""
    started = time.monotonic()
    args = coerce(args or {}, tool.parameters, (tool.parameters or {}).get("$defs"))
    try:
        result = await tool.run(args, ctx)
        text, ok = _shorten(result), True
    except ValidationError as exc:
        text, ok = f"Invalid arguments: {exc.errors()[:5]}", False
    except (ShoppingError, RetailerError, AuthRequired, NotionError, VaultError, ValueError, LookupError) as exc:
        text, ok = f"Error: {exc}", False
    except Exception as exc:  # noqa: BLE001 - report, don't crash the conversation
        log.exception("tool %s crashed", tool.name)
        text, ok = (f"Error: the tool failed unexpectedly ({type(exc).__name__}). Check the argument format against the "
                    "tool's schema (objects as objects, not quoted JSON) and try once more.", False)
    audit(tool=tool.name, tier=tool.tier, args=args, outcome="ok" if ok else "error", detail=None if ok else text, ctx=ctx,
          ms=int((time.monotonic() - started) * 1000))
    return ok, text


# -- shopping tools (agent/shopping_tools.py): their permission tiers --------------------------------

SHOPPING_TIERS = {
    # read
    "shopping_get_context": READ, "shopping_search_catalog": READ, "shopping_list_aisles": READ, "shopping_search_many": READ,
    "shopping_get_product": READ, "shopping_get_cart": READ, "shopping_auth_status": READ, "shopping_get_run": READ, "shopping_list_runs": READ,
    "shopping_get_product_history": READ, "shopping_get_recent_price_history": READ, "shopping_get_feedback": READ,
    "shopping_get_requirement_history": READ, "shopping_get_past_orders": READ, "shopping_get_top_deals": READ,
    # safe: reversible, or reviewed by a person before anything is bought
    "shopping_stage_items": SAFE, "shopping_update_selection": SAFE, "shopping_mark_feedback_reviewed": SAFE,
    "shopping_add_top_deal": SAFE, "shopping_retire_top_deal": SAFE,
    "shopping_get_past_order": SAFE,  # imports the order into history
    "shopping_apply_approved": SAFE,  # only items a person already approved
    "shopping_quick_add": SAFE,  # straight to the cart: Quick Shop only, at a person's request (never on a schedule)
}

# Tools that change the real cart without a review step. Only offered in Quick Shop, never on a schedule.
CART_WRITE = {"shopping_quick_add"}


def register_shopping_tools() -> None:
    from shopping.agent.shopping_tools import mcp

    for t in mcp._tool_manager.list_tools():  # noqa: SLF001
        tier = SHOPPING_TIERS.get(t.name)
        if tier is None:
            raise RuntimeError(f"Tool {t.name} has no permission tier; add it to SHOPPING_TIERS.")

        def make_run(tool):
            async def run(args: dict, ctx: ToolContext):
                return await tool.fn_metadata.call_fn_with_arg_validation(tool.fn, tool.is_async, args, None)
            return run

        registry.add(Tool(name=t.name, description=t.description or "", parameters=t.parameters, tier=tier, run=make_run(t)))
