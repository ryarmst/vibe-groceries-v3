"""Build a provider from settings + the vault, and estimate cost."""

from __future__ import annotations

from sqlalchemy.orm import Session

from shopping import appsettings
from shopping.agent.providers.anthropic import Anthropic
from shopping.agent.providers.base import ProviderError
from shopping.agent.providers.openai_compat import OpenAICompatible

# USD per million tokens (input, output) when LLM settings have no "prices" entry for the model.
# Gemini is used on the free tier (the household's choice), so it counts as $0 unless priced in settings.
# Anything else unpriced is charged at a deliberately high estimate so the budget cap errs on the safe side.
FREE_PROVIDERS = {"gemini"}
CONSERVATIVE = (5.0, 20.0)


def build(session: Session, vault, provider: str, *, transport=None):
    if provider not in appsettings.PROVIDERS:
        raise ProviderError(f"Unknown provider {provider!r}.")
    adapter, base_url, secret = appsettings.PROVIDERS[provider]
    llm = appsettings.get(session, "llm")
    key = vault.get(session, secret)
    if provider == "custom":
        base_url = llm.get("custom_base_url") or ""
    elif not key:
        raise ProviderError(f"No API key set for {provider}. Add one in Settings → LLM.")
    if adapter == "anthropic":
        return Anthropic(api_key=key or "", base_url=base_url, transport=transport)
    return OpenAICompatible(name=provider, base_url=base_url, api_key=key, transport=transport)


def cost_usd(session: Session, provider: str, model: str, tokens_in: int, tokens_out: int) -> float:
    prices = appsettings.get(session, "llm").get("prices") or {}
    if model in prices:
        pin, pout = prices[model]
    elif provider in FREE_PROVIDERS or provider == "custom":
        pin, pout = 0.0, 0.0
    else:
        pin, pout = CONSERVATIVE
    return round((tokens_in * pin + tokens_out * pout) / 1_000_000, 6)
