"""Playbooks: one short system prompt + a fixed tool list per job (the skills, ported for any LLM)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DIR = Path(__file__).parent / "playbooks"
MODES = ("ask", "shop", "quick", "meals", "pantry", "recipes")  # chosen in the app; plan_week is for the scheduler
# Never picked by "auto": Quick Shop changes the cart directly, so a person has to choose it.


@dataclass(frozen=True)
class Playbook:
    name: str
    title: str
    tier: str
    tools: tuple[str, ...]
    body: str


@lru_cache(maxsize=None)
def load(name: str) -> Playbook:
    text = (DIR / f"{name}.md").read_text()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise ValueError(f"playbook {name} has no header")
    meta = dict(line.split(":", 1) for line in m.group(1).splitlines() if ":" in line)
    meta = {k.strip(): v.strip() for k, v in meta.items()}
    tools = tuple(t.strip() for t in meta.get("tools", "").split(",") if t.strip())
    return Playbook(name, meta.get("title", name), meta.get("tier", "fast"), tools, m.group(2).strip())


@lru_cache(maxsize=None)
def common() -> str:
    return (DIR / "_common.md").read_text().strip()


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
