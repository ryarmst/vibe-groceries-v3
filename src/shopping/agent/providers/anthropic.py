"""Anthropic Messages API (native tool use, prompt caching on the system prompt)."""

from __future__ import annotations

import json
from typing import AsyncIterator

import httpx

from shopping.agent.providers.base import Done, ProviderError, TextDelta, ToolCall, ToolSpec, Usage, error_from_status

API_VERSION = "2023-06-01"


def to_anthropic_messages(messages: list[dict]) -> list[dict]:
    """Neutral → Anthropic. Consecutive tool results merge into one user turn of tool_result blocks."""
    out: list[dict] = []
    for m in messages:
        if m["role"] == "assistant":
            blocks = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for c in m.get("tool_calls") or []:
                blocks.append({"type": "tool_use", "id": c["id"], "name": c["name"], "input": c.get("arguments") or {}})
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
        elif m["role"] == "tool":
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m.get("content") or ""}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) and out[-1]["content"] \
                    and out[-1]["content"][0].get("type") == "tool_result":
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        else:
            out.append({"role": "user", "content": m.get("content") or ""})
    return out


class Anthropic:
    def __init__(self, *, api_key: str, base_url: str = "https://api.anthropic.com", timeout: float = 120.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        if not api_key:
            raise ProviderError("anthropic: no API key set.")
        self.name = "anthropic"
        self._base = base_url.rstrip("/")
        self._headers = {"x-api-key": api_key, "anthropic-version": API_VERSION, "content-type": "application/json"}
        self._timeout, self._transport = timeout, transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=httpx.Timeout(self._timeout, connect=15), transport=self._transport)

    async def list_models(self) -> list[str]:
        async with self._client() as c:
            r = await c.get(f"{self._base}/v1/models", headers=self._headers, params={"limit": 100})
        if r.status_code >= 400:
            raise error_from_status(r.status_code, r.text, self.name)
        return sorted(m["id"] for m in r.json().get("data") or [] if m.get("id"))

    async def stream(self, *, system: str, messages: list[dict], tools: list[ToolSpec], model: str,
                     temperature: float, max_tokens: int) -> AsyncIterator[TextDelta | ToolCall | Usage | Done]:
        body: dict = {"model": model, "max_tokens": max_tokens, "temperature": min(temperature, 1.0), "stream": True,
                      "messages": to_anthropic_messages(messages)}
        if system:
            body["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        if tools:
            body["tools"] = [{"name": t.name, "description": t.description[:1024], "input_schema": t.parameters} for t in tools]
        usage = Usage()
        blocks: dict[int, dict] = {}
        stop = "end_turn"
        try:
            async with self._client() as c:
                async with c.stream("POST", f"{self._base}/v1/messages", headers=self._headers, json=body) as r:
                    if r.status_code >= 400:
                        raise error_from_status(r.status_code, (await r.aread()).decode(errors="replace"), self.name)
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        try:
                            ev = json.loads(line[5:].strip())
                        except ValueError:
                            continue
                        kind = ev.get("type")
                        if kind == "message_start":
                            u = (ev.get("message") or {}).get("usage") or {}
                            usage.input_tokens = int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) \
                                + int(u.get("cache_creation_input_tokens") or 0)
                        elif kind == "content_block_start":
                            cb = ev.get("content_block") or {}
                            blocks[ev["index"]] = {"type": cb.get("type"), "id": cb.get("id"), "name": cb.get("name"), "json": ""}
                        elif kind == "content_block_delta":
                            d = ev.get("delta") or {}
                            if d.get("type") == "text_delta" and d.get("text"):
                                yield TextDelta(d["text"])
                            elif d.get("type") == "input_json_delta":
                                blocks.setdefault(ev["index"], {"type": "tool_use", "json": ""})["json"] += d.get("partial_json") or ""
                        elif kind == "message_delta":
                            stop = (ev.get("delta") or {}).get("stop_reason") or stop
                            usage.output_tokens = int((ev.get("usage") or {}).get("output_tokens") or usage.output_tokens)
                        elif kind == "error":
                            err = ev.get("error") or {}
                            raise ProviderError(f"anthropic: {err.get('message', 'error')}", retryable=err.get("type") == "overloaded_error")
        except httpx.TimeoutException as exc:
            raise ProviderError("anthropic timed out.", retryable=True) from exc
        except httpx.TransportError as exc:
            raise ProviderError(f"anthropic unreachable ({type(exc).__name__}).", retryable=True) from exc
        for _, b in sorted(blocks.items()):
            if b.get("type") == "tool_use":
                try:
                    args = json.loads(b["json"]) if b["json"].strip() else {}
                except ValueError:
                    args = {"_unparsed": b["json"][:2000]}
                yield ToolCall(b["id"], b["name"], args)
        yield usage
        yield Done(stop)
