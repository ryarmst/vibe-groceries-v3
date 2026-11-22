"""Playbooks: one system prompt + a fixed tool list per task. The text lives in shopping/prompts/ (editable in
the app); the tool list always comes from the file, so editing a prompt can't change what a task may do."""

from __future__ import annotations

import re
from dataclasses import dataclass

from shopping import prompts
from shopping.database import session_scope

MODES = ("ask", "shop", "quick", "meals", "pantry", "recipes")  # chosen in the app; plan_week is for the scheduler
# Never picked by "auto": Quick Shop changes the cart directly, so a person has to choose it.


@dataclass(frozen=True)
class Playbook:
    name: str
    title: str
    tier: str
    tools: tuple[str, ...]
    body: str


def prompt_id(name: str) -> str:
    return "jobs/weekly_plan" if name == "plan_week" else f"tasks/{name}"


def load(name: str) -> Playbook:
    p = prompts.get(prompt_id(name))
    with session_scope() as session:
        body, tier, _ = prompts.current(session, p.id)
    return Playbook(name, p.title, tier or "fast", p.tools, body)


def about() -> str:
    """The household's own notes ("About your household"), as a section, or "" if they left it empty."""
    with session_scope() as session:
        body, _, _ = prompts.current(session, "about")
    return f"## About this household\n{body}" if body.strip() else ""


def common() -> str:
    """Shared instructions for every chat task and the weekly plan, plus the household's notes."""
    with session_scope() as session:
        body, _, _ = prompts.current(session, "household")
    return "\n\n".join(x for x in (body, about()) if x)


_RULES = [
    ("pantry", r"\b(receipt\w*|used up|ran out|run out|threw (out|away)|toss(ed)?|froze\w*|freezer|fridge|expir\w*|leftover\w*|"
               r"pantry|inventory)\b|\badd\b.*\b(to )?(the |my |our )?(grocery )?list\b|\bmark\b.*\b(bought|purchased)\b"),
    ("meals", r"\b(meal\w*|plan the week|weekly plan|dinner ideas|what should we (eat|cook)|suggest\w*)\b"),
    ("recipes", r"\b(recipe\w*|what can i (make|cook)|how do i (make|cook)|save (it|this) to cooking)\b"),
    ("shop", r"\b(shop\w*|cart|grocery run|order\w*|restock|buy|stage|regulars?)\b"),
]


def route(text: str, previous: str | None = None) -> str:
    t = (text or "").lower()
    for mode, pattern in _RULES:
        if re.search(pattern, t):
            return mode
    # Short follow-ups ("yes", "the second one", "go ahead") stay in the previous mode.
    if previous in MODES and len(t.split()) <= 8:
        return previous
    return "ask"
