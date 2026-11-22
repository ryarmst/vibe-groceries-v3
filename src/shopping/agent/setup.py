"""Register all agent tools once."""

from __future__ import annotations

_done = False


def ensure_tools() -> None:
    global _done
    if _done:
        return
    from shopping.agent.kitchen_tools import register_kitchen_tools
    from shopping.agent.tools import register_shopping_tools

    register_shopping_tools()
    register_kitchen_tools()
    _done = True
