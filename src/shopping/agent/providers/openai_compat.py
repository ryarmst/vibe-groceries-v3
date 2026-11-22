"""OpenAI-compatible Chat Completions (OpenAI, Gemini's OpenAI endpoint, Groq, OpenRouter, Mistral, Ollama…)."""

from __future__ import annotations

import json
import uuid
from typing import AsyncIterator

import httpx

from shopping.agent.providers.base import Done, ProviderError, TextDelta, ToolCall, ToolSpec, Usage, error_from_status

# JSON-Schema keywords the stricter providers (notably Gemini) accept in tool parameters.
_ALLOWED_KEYS = {"type", "properties", "required", "items", "enum", "description", "minimum", "maximum",
                 "minItems", "maxItems", "nullable", "format"}


def simplify_schema(schema: dict) -> dict:
    """Reduce a pydantic JSON schema to the common subset: no $refs/anyOf/titles/defaults.
    Optional[X] (anyOf [X, null]) becomes X; other unions fall back to string."""
    if not isinstance(schema, dict):
        return schema
    if "anyOf" in schema:
        options = [o for o in schema["anyOf"] if o.get("type") != "null"]
        base = simplify_schema(options[0]) if len(options) == 1 else {"type": "string"}
        if schema.get("description"):
            base["description"] = schema["description"]
        return base
    out = {}
    for k, v in schema.items():
        if k not in _ALLOWED_KEYS:
            continue
        if k == "properties":
            out[k] = {name: simplify_schema(p) for name, p in v.items()}
        elif k == "items":
            out[k] = simplify_schema(v)
        elif k == "format" and v not in ("date-time", "enum"):
            continue
        else:
            out[k] = v
    if out.get("type") == "object" and "properties" not in out:
        out["properties"] = {}
    return out


def to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    out = [{"role": "system", "content": system}] if system else []
    for m in messages:
        if m["role"] == "assistant":
            msg = {"role": "assistant", "content": m.get("content") or None}
            if m.get("tool_calls"):
                msg["tool_calls"] = []
                for c in m["tool_calls"]:
                    call = {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c.get("arguments") or {})}}
                    if c.get("extra"):
                        call["extra_content"] = c["extra"]  # Gemini requires its thought signature back, unchanged
                    msg["tool_calls"].append(call)
            if msg["content"] is None and not msg.get("tool_calls"):
                msg["content"] = ""
            out.append(msg)
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m.get("content") or ""})
        else:
            out.append({"role": "user", "content": m.get("content") or ""})
    return out


class OpenAICompatible:
    def __init__(self, *, name: str, base_url: str, api_key: str | None, timeout: float = 120.0,
                 transport: httpx.AsyncBaseTransport | None = None, strict_schema: bool = False):
        if not base_url:
            raise ProviderError(f"{name}: no base URL configured.")
        self.name = name
        self._base = base_url.rstrip("/")
        self._headers = {"content-type": "application/json"}
        if api_key:
            self._headers["authorization"] = f"Bearer {api_key}"
        if name == "openrouter":
            self._headers["x-title"] = "vibe-groceries"
        self._timeout = timeout
        self._transport = transport
        self._strict = strict_schema or name == "gemini"

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=httpx.Timeout(self._timeout, connect=15), transport=self._transport)

    async def list_models(self) -> list[str]:
        async with self._client() as c:
            r = await c.get(f"{self._base}/models", headers=self._headers)
        if r.status_code >= 400:
            raise error_from_status(r.status_code, r.text, self.name)
        data = r.json()
        return sorted(str(m.get("id", "")).removeprefix("models/") for m in data.get("data") or [] if m.get("id"))

    async def stream(self, *, system: str, messages: list[dict], tools: list[ToolSpec], model: str,
                     temperature: float, max_tokens: int) -> AsyncIterator[TextDelta | ToolCall | Usage | Done]:
        body: dict = {"model": model, "messages": to_openai_messages(system, messages), "stream": True,
                      "temperature": temperature, "max_tokens": max_tokens}
        if tools:
            body["tools"] = [{"type": "function", "function": {
                "name": t.name, "description": t.description[:1024],
                "parameters": simplify_schema(t.parameters) if self._strict else t.parameters}} for t in tools]
        if self.name in ("openai", "openrouter", "gemini", "groq", "mistral"):
            body["stream_options"] = {"include_usage": True}  # token counts on streamed replies (budget and limits)
        calls: dict[int, dict] = {}
        usage = Usage()
        stop = "stop"
        try:
            async with self._client() as c:
                async with c.stream("POST", f"{self._base}/chat/completions", headers=self._headers, json=body) as r:
                    if r.status_code >= 400:
                        raise error_from_status(r.status_code, (await r.aread()).decode(errors="replace"), self.name)
                    async for line in r.aiter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            chunk = json.loads(payload)
                        except ValueError:
                            continue
                        if chunk.get("error"):
                            raise ProviderError(f"{self.name}: {str(chunk['error'])[:300]}", retryable=False)
                        u = chunk.get("usage") or (chunk.get("x_groq") or {}).get("usage")
                        if u:
                            usage = Usage(int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0))
                        for choice in chunk.get("choices") or []:
                            delta = choice.get("delta") or {}
                            if delta.get("content"):
                                yield TextDelta(delta["content"])
                            for tc in delta.get("tool_calls") or []:
                                slot = calls.setdefault(int(tc.get("index", len(calls))), {"id": None, "name": "", "args": "", "extra": None})
                                if tc.get("extra_content"):
                                    slot["extra"] = tc["extra_content"]
                                slot["id"] = tc.get("id") or slot["id"]
                                fn = tc.get("function") or {}
                                if fn.get("name"):
                                    slot["name"] = fn["name"]  # sent whole, once per call
                                slot["args"] += fn.get("arguments") or ""
                            if choice.get("finish_reason"):
                                stop = choice["finish_reason"]
        except httpx.TimeoutException as exc:
            raise ProviderError(f"{self.name} timed out.", retryable=True) from exc
        except httpx.TransportError as exc:
            raise ProviderError(f"{self.name} unreachable ({type(exc).__name__}).", retryable=True) from exc
        for _, slot in sorted(calls.items()):
            try:
                args = json.loads(slot["args"]) if slot["args"].strip() else {}
            except ValueError:
                args = {"_unparsed": slot["args"][:2000]}
            yield ToolCall(slot["id"] or f"call_{uuid.uuid4().hex[:12]}", slot["name"], args if isinstance(args, dict) else {"value": args},
                           extra=slot["extra"])
        yield usage
        yield Done("tool_calls" if calls else stop)
