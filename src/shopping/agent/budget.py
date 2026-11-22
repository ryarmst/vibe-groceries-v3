"""Spend and request caps for LLM calls. The cap is a setting the household changes from the app."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shopping import appsettings
from shopping.agent.providers.registry import cost_usd
from shopping.config import settings
from shopping.models import LLMUsage


class BudgetExceeded(Exception):
    pass


def _month_start() -> datetime:
    now = datetime.now(ZoneInfo(settings.timezone))
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _day_start() -> datetime:
    now = datetime.now(ZoneInfo(settings.timezone))
    return datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)


def summary(session: Session) -> dict:
    b = appsettings.get(session, "budget")
    spent = session.scalar(select(func.coalesce(func.sum(LLMUsage.cost_usd), 0.0)).where(LLMUsage.at >= _month_start())) or 0.0
    requests = session.scalar(select(func.count(LLMUsage.id)).where(LLMUsage.at >= _day_start())) or 0
    tokens = session.execute(select(func.coalesce(func.sum(LLMUsage.tokens_in), 0), func.coalesce(func.sum(LLMUsage.tokens_out), 0))
                             .where(LLMUsage.at >= _month_start())).one()
    cap = float(b["monthly_usd"])
    return {"month_spent_usd": round(float(spent), 4), "monthly_cap_usd": cap,
            "pct": round(spent / cap * 100, 1) if cap else (100.0 if spent else 0.0),
            "warn": cap > 0 and spent >= cap * b["warn_at_pct"] / 100, "requests_today": requests,
            "daily_request_limit": b["daily_requests"], "month_tokens_in": int(tokens[0]), "month_tokens_out": int(tokens[1])}


def check(session: Session) -> None:
    s = summary(session)
    if s["month_spent_usd"] >= s["monthly_cap_usd"] and s["month_spent_usd"] > 0:
        raise BudgetExceeded(f"Monthly LLM budget reached (${s['month_spent_usd']:.2f} of ${s['monthly_cap_usd']:.2f}). "
                             "Raise it in Settings → Budget, or wait until next month.")
    if s["requests_today"] >= s["daily_request_limit"]:
        raise BudgetExceeded(f"Daily request limit reached ({s['daily_request_limit']}). Raise it in Settings → Budget.")


def record(session: Session, *, provider: str, model: str, tokens_in: int, tokens_out: int, thread_id: str | None) -> float:
    cost = cost_usd(session, provider, model, tokens_in, tokens_out)
    session.add(LLMUsage(provider=provider, model=model, tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost, thread_id=thread_id))
    session.flush()
    return cost


def daily_breakdown(session: Session, days: int = 30) -> list[dict]:
    since = _day_start() - timedelta(days=days - 1)
    rows = session.execute(select(func.date(LLMUsage.at), LLMUsage.provider, LLMUsage.model, func.count(LLMUsage.id),
                                  func.sum(LLMUsage.tokens_in), func.sum(LLMUsage.tokens_out), func.sum(LLMUsage.cost_usd))
                           .where(LLMUsage.at >= since).group_by(func.date(LLMUsage.at), LLMUsage.provider, LLMUsage.model)
                           .order_by(func.date(LLMUsage.at).desc())).all()
    return [{"day": str(d), "provider": p, "model": m, "requests": n, "tokens_in": int(ti or 0), "tokens_out": int(to or 0),
             "cost_usd": round(float(c or 0), 4)} for d, p, m, n, ti, to, c in rows]
