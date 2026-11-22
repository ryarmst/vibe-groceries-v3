"""Free-tier quotas: requests per minute and per day, per model.

Limits are settings (LLM → limits, e.g. {"gemini-flash-latest": {"rpm": 5, "rpd": 20}}). Before each call:
  - a model already at its daily limit (counted from our usage log, or told by a 429) is skipped, so the loop
    moves to the next model instead of failing;
  - calls are spaced so the per-minute limit isn't hit (the caller waits, with a notice).
Google resets free-tier daily quotas at midnight Pacific time, so "today" is counted in that zone.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shopping.models import LLMUsage

RESET_TZ = ZoneInfo("America/Los_Angeles")
_recent: dict[str, deque] = defaultdict(deque)  # model key -> monotonic timestamps of recent calls
_exhausted: dict[str, str] = {}  # model key -> Pacific date it ran out


def _key(provider: str, model: str) -> str:
    return f"{provider}/{model}"


def _today() -> str:
    return datetime.now(RESET_TZ).date().isoformat()


def _day_start() -> datetime:
    now = datetime.now(RESET_TZ)
    return datetime.combine(now.date(), dtime.min, tzinfo=RESET_TZ)


def limits_for(llm: dict, model: str) -> dict:
    return (llm.get("limits") or {}).get(model) or {}


def requests_today(session: Session, provider: str, model: str) -> int:
    return session.scalar(select(func.count(LLMUsage.id)).where(LLMUsage.provider == provider, LLMUsage.model == model,
                                                                LLMUsage.at >= _day_start())) or 0


def check(session: Session, llm: dict, provider: str, model: str) -> tuple[bool, str | None, float]:
    """(usable now?, reason if not, seconds to wait first for the per-minute limit)."""
    key = _key(provider, model)
    if _exhausted.get(key) == _today():
        return False, f"{model} is out of free requests for today", 0.0
    lim = limits_for(llm, model)
    rpd = lim.get("rpd")
    if rpd and requests_today(session, provider, model) >= rpd:
        _exhausted[key] = _today()
        return False, f"{model} has used its {rpd} requests for today", 0.0
    rpm = lim.get("rpm")
    wait = 0.0
    if rpm:
        window = _recent[key]
        now = time.monotonic()
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= rpm:
            wait = 60 - (now - window[0]) + 0.5
    return True, None, wait


def note_request(provider: str, model: str) -> None:
    _recent[_key(provider, model)].append(time.monotonic())


def record_failed_attempt(session: Session, provider: str, model: str, thread_id: str | None = None) -> None:
    """Google counts failed requests against the daily quota too; so do we (zero tokens, zero cost)."""
    session.add(LLMUsage(provider=provider, model=model, tokens_in=0, tokens_out=0, cost_usd=0.0, thread_id=thread_id))


def classify_error(provider: str, model: str, exc) -> str | None:
    """'daily' (model done for today: skip it), 'minute' (wait and retry), or None."""
    if getattr(exc, "status", None) != 429:
        return None
    body = (getattr(exc, "body", "") or str(exc)).lower()
    if "perday" in body or "per day" in body or "per_day" in body or "daily" in body:
        _exhausted[_key(provider, model)] = _today()
        return "daily"
    return "minute"


def status(session: Session, llm: dict) -> list[dict]:
    """Requests used today per configured model, for Settings."""
    models = {(t["provider"], t["model"]) for t in llm["tiers"].values()} | {(f["provider"], f["model"]) for f in llm.get("fallback") or []}
    out = []
    for provider, model in sorted(models):
        lim = limits_for(llm, model)
        out.append({"provider": provider, "model": model, "used_today": requests_today(session, provider, model),
                    "rpd": lim.get("rpd"), "rpm": lim.get("rpm"), "exhausted": _exhausted.get(_key(provider, model)) == _today()})
    return out


def reset() -> None:
    """Tests."""
    _recent.clear()
    _exhausted.clear()
