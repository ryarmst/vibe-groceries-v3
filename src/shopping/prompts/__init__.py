"""The assistant's prompts: Markdown files in this folder are the defaults; people can override any of them
from the app (stored in the database, with a revision history). See README.md in this folder."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from shopping.models import PromptOverride, PromptRevision, utcnow

DIR = Path(__file__).parent
# Display order, and the groups the Prompts page shows.
ORDER = ["about", "household", "tasks/week", "tasks/quick", "tasks/shop", "tasks/meals", "tasks/pantry", "tasks/recipes", "tasks/ask",
         "jobs/weekly_plan", "jobs/deal_review", "references/receipts", "references/shelf-life"]
GROUPS = {"about": "Your household", "household": "Shared instructions", "tasks": "Chat tasks", "jobs": "Background jobs",
          "references": "Reference notes"}
PLACEHOLDERS = {
    "household": "Names and region, from ALLOWED_USERS and HOUSEHOLD_REGION",
    "today": "Today's date (YYYY-MM-DD)", "weekday": "Today's weekday", "store_id": "Your store number",
    "person": "The person chatting", "meals_per_week": "Meals per week (Settings → Schedule)",
    "max_picks": "How many deals the daily picks may choose",
}
MAX_CHARS = 40_000
KEEP_REVISIONS = 20
_PH = re.compile(r"\{([a-z_]+)\}")


class PromptError(ValueError):
    pass


@dataclass(frozen=True)
class Prompt:
    id: str
    group: str
    title: str
    description: str
    tier: str | None  # fast / smart, for prompts that run a model
    tools: tuple[str, ...]
    steps: int | None  # model calls allowed per reply, when a task needs more than the usual
    placeholders: tuple[str, ...]
    default: str


def _parse(pid: str) -> Prompt:
    text = (DIR / f"{pid}.md").read_text()
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if not m:
        raise ValueError(f"prompt {pid} has no header")
    meta = {k.strip(): v.strip() for k, _, v in (line.partition(":") for line in m.group(1).splitlines()) if k.strip()}
    split = lambda key: tuple(x.strip() for x in meta.get(key, "").split(",") if x.strip())  # noqa: E731
    return Prompt(pid, GROUPS[pid.split("/")[0]], meta.get("title", pid), meta.get("description", ""),
                  meta.get("tier") or None, split("tools"), int(meta["steps"]) if meta.get("steps") else None,
                  split("placeholders"), m.group(2).strip())


@lru_cache(maxsize=None)
def catalog() -> dict[str, Prompt]:
    return {pid: _parse(pid) for pid in ORDER}


def get(pid: str) -> Prompt:
    try:
        return catalog()[pid]
    except KeyError:
        raise PromptError(f"No prompt {pid!r}.") from None


def current(session: Session, pid: str) -> tuple[str, str | None, PromptOverride | None]:
    """(body, tier, override row or None): the household's version if they edited it, else the file's."""
    p = get(pid)
    row = session.get(PromptOverride, pid)
    if row is None:
        return p.default, p.tier, None
    return row.body, (row.tier or p.tier) if p.tier else None, row


def render(text: str, fill: dict) -> str:
    """Fill {placeholders}; anything else in braces (JSON examples, unknown names) is left alone."""
    return _PH.sub(lambda m: str(fill[m.group(1)]) if m.group(1) in fill else m.group(0), text)


def unknown_placeholders(pid: str, body: str) -> list[str]:
    allowed = set(get(pid).placeholders)
    return sorted({n for n in _PH.findall(body) if n not in allowed})


def _revision(session: Session, pid: str, body: str, tier: str | None, by: str | None, note: str) -> None:
    session.add(PromptRevision(prompt_id=pid, body=body, tier=tier, saved_by=by, note=note))
    session.flush()
    ids = list(session.scalars(select(PromptRevision.id).where(PromptRevision.prompt_id == pid)
                               .order_by(PromptRevision.id.desc()).offset(KEEP_REVISIONS)))
    if ids:
        session.execute(delete(PromptRevision).where(PromptRevision.id.in_(ids)))


def save(session: Session, pid: str, *, body: str, tier: str | None = None, by: str | None = None) -> list[str]:
    """Store the household's version. Returns warnings (e.g. unknown placeholders); raises PromptError if invalid."""
    p = get(pid)
    body = body.replace("\r\n", "\n").strip()
    if len(body) > MAX_CHARS:
        raise PromptError(f"Too long ({len(body)} characters; the limit is {MAX_CHARS}).")
    if not body and pid != "about":
        raise PromptError("A prompt can't be empty. Use Reset to go back to the default.")
    if tier is not None and (not p.tier or tier not in ("fast", "smart")):
        raise PromptError("Model must be fast or smart (and only for prompts that run a model).")
    old_body, old_tier, row = current(session, pid)
    if body == old_body and (tier or old_tier) == old_tier:
        return unknown_placeholders(pid, body)
    _revision(session, pid, old_body, old_tier, by, "before edit")
    if body == p.default and (tier or p.tier) == p.tier:
        if row is not None:
            session.delete(row)  # identical to the file again: no override needed
    else:
        if row is None:
            row = PromptOverride(prompt_id=pid)
            session.add(row)
        row.body, row.tier, row.updated_at, row.updated_by = body, tier if tier != p.tier else None, utcnow(), by
    session.flush()
    return unknown_placeholders(pid, body)


def reset(session: Session, pid: str, by: str | None = None) -> None:
    get(pid)
    row = session.get(PromptOverride, pid)
    if row is not None:
        _revision(session, pid, row.body, row.tier, by, "before reset")
        session.delete(row)
        session.flush()


def revisions(session: Session, pid: str) -> list[PromptRevision]:
    return list(session.scalars(select(PromptRevision).where(PromptRevision.prompt_id == pid).order_by(PromptRevision.id.desc())))
