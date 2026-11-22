"""Provider-neutral types.

Messages: {"role": "user"|"assistant"|"tool", "content": str|None,
           "tool_calls": [{"id", "name", "arguments": dict}]   (assistant)
           "tool_call_id": str, "name": str                    (tool result)}
The system prompt is passed separately. Adapters translate to and from each API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    # Provider data that must be sent back with this call, e.g. Gemini's {"google": {"thought_signature": …}}.
    extra: dict | None = None


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class Done:
    stop_reason: str = "stop"


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict = field(default_factory=lambda: {"type": "object", "properties": {}})


class ProviderError(Exception):
    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.retryable, self.status, self.body = retryable, status, body


class Provider(Protocol):
    name: str

    def stream(self, *, system: str, messages: list[dict], tools: list[ToolSpec], model: str,
               temperature: float, max_tokens: int) -> AsyncIterator[TextDelta | ToolCall | Usage | Done]: ...

    async def list_models(self) -> list[str]: ...


def error_from_status(status: int, body: str, provider: str) -> ProviderError:
    snippet = body.strip().replace("\n", " ")[:300]
    if status in (401, 403):
        return ProviderError(f"{provider} rejected the API key ({status}). Check it in Settings.", status=status, body=body)
    if status == 429:
        return ProviderError(f"{provider} rate limit or quota reached (429): {snippet}", retryable=True, status=status, body=body)
    if status >= 500:
        return ProviderError(f"{provider} is having trouble ({status}).", retryable=True, status=status, body=body)
    return ProviderError(f"{provider} refused the request ({status}): {snippet}", status=status, body=body)
