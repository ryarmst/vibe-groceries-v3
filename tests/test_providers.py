import json

import httpx
import pytest

from shopping.agent.providers.anthropic import Anthropic, to_anthropic_messages
from shopping.agent.providers.base import Done, ProviderError, TextDelta, ToolCall, ToolSpec, Usage
from shopping.agent.providers.openai_compat import OpenAICompatible, simplify_schema, to_openai_messages


def sse(chunks):
    return "".join(f"data: {json.dumps(c) if not isinstance(c, str) else c}\n\n" for c in chunks)


async def collect(gen):
    return [ev async for ev in gen]


async def test_openai_stream_text_tool_calls_and_usage():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["authorization"]
        body = sse([
            {"choices": [{"delta": {"content": "Checking "}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "notion_pantry", "arguments": '{"que'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'ry": "eggs"}'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 1, "function": {"name": "notion_grocery_list", "arguments": "{}"}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 120, "completion_tokens": 30}},
            "[DONE]",
        ])
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    p = OpenAICompatible(name="gemini", base_url="https://g.example/v1beta/openai", api_key="k", transport=httpx.MockTransport(handler))
    schema = {"type": "object", "properties": {"query": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Query"}}}
    evs = await collect(p.stream(system="sys", messages=[{"role": "user", "content": "hi"}], tools=[ToolSpec("notion_pantry", "d", schema)],
                                 model="gemini-2.5-flash", temperature=0.2, max_tokens=100))
    assert [e.text for e in evs if isinstance(e, TextDelta)] == ["Checking "]
    calls = [e for e in evs if isinstance(e, ToolCall)]
    assert calls[0] == ToolCall("c1", "notion_pantry", {"query": "eggs"})
    assert calls[1].name == "notion_grocery_list" and calls[1].id.startswith("call_")  # id synthesized
    assert next(e for e in evs if isinstance(e, Usage)) == Usage(120, 30)
    assert isinstance(evs[-1], Done) and evs[-1].stop_reason == "tool_calls"
    # Gemini gets the simplified schema; system prompt first; key in the header.
    assert seen["body"]["tools"][0]["function"]["parameters"]["properties"]["query"] == {"type": "string"}
    assert seen["body"]["messages"][0] == {"role": "system", "content": "sys"} and seen["auth"] == "Bearer k"


@pytest.mark.parametrize("status,retryable", [(429, True), (503, True), (401, False), (400, False)])
async def test_openai_errors(status, retryable):
    p = OpenAICompatible(name="groq", base_url="https://x/v1", api_key="k",
                         transport=httpx.MockTransport(lambda r: httpx.Response(status, text='{"error":"nope"}')))
    with pytest.raises(ProviderError) as exc:
        await collect(p.stream(system="", messages=[], tools=[], model="m", temperature=0, max_tokens=5))
    assert exc.value.retryable is retryable


async def test_anthropic_stream():
    def handler(request):
        assert request.headers["x-api-key"] == "ak"
        body = json.loads(request.content)
        assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
        events = [
            {"type": "message_start", "message": {"usage": {"input_tokens": 50, "cache_read_input_tokens": 10}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "tu1", "name": "meals_context"}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"expiring_'}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": 'days": 5}'}},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 22}},
            {"type": "message_stop"},
        ]
        return httpx.Response(200, text="".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events))

    p = Anthropic(api_key="ak", transport=httpx.MockTransport(handler))
    evs = await collect(p.stream(system="sys", messages=[{"role": "user", "content": "plan"}], tools=[], model="claude", temperature=0.4, max_tokens=100))
    assert [e.text for e in evs if isinstance(e, TextDelta)] == ["Hello"]
    assert [e for e in evs if isinstance(e, ToolCall)] == [ToolCall("tu1", "meals_context", {"expiring_days": 5})]
    assert next(e for e in evs if isinstance(e, Usage)) == Usage(60, 22)


def test_message_conversions():
    msgs = [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "name": "t1", "arguments": {}}, {"id": "b", "name": "t2", "arguments": {"x": 1}}]},
            {"role": "tool", "tool_call_id": "a", "name": "t1", "content": "r1"},
            {"role": "tool", "tool_call_id": "b", "name": "t2", "content": "r2"},
            {"role": "assistant", "content": "done", "tool_calls": []}]
    anth = to_anthropic_messages(msgs)
    assert [m["role"] for m in anth] == ["user", "assistant", "user", "assistant"]
    assert [b["tool_use_id"] for b in anth[2]["content"]] == ["a", "b"]  # results merged into one turn
    oa = to_openai_messages("s", msgs)
    assert oa[2]["content"] is None and oa[2]["tool_calls"][1]["function"]["arguments"] == '{"x": 1}'
    assert oa[3] == {"role": "tool", "tool_call_id": "a", "content": "r1"}


def test_simplify_schema_drops_unsupported_keywords():
    s = simplify_schema({"type": "object", "title": "X", "properties": {
        "a": {"type": "integer", "minimum": 1, "default": 3, "title": "A"},
        "b": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}], "description": "list"},
        "c": {"anyOf": [{"type": "string"}, {"type": "integer"}]}}, "required": ["a"]})
    assert s == {"type": "object", "properties": {"a": {"type": "integer", "minimum": 1},
                                                  "b": {"type": "array", "items": {"type": "string"}, "description": "list"},
                                                  "c": {"type": "string"}}, "required": ["a"]}



async def test_gemini_thought_signature_round_trip():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        if len(sent) == 1:
            body = sse([{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                         "extra_content": {"google": {"thought_signature": "SIG123"}},
                         "function": {"name": "notion_pantry", "arguments": "{}"}}]}}]}, "[DONE]"])
        else:
            body = sse([{"choices": [{"delta": {"content": "done"}}]}, "[DONE]"])
        return httpx.Response(200, text=body)

    p = OpenAICompatible(name="gemini", base_url="https://g/v1", api_key="k", transport=httpx.MockTransport(handler))
    evs = await collect(p.stream(system="", messages=[{"role": "user", "content": "hi"}], tools=[], model="m", temperature=0, max_tokens=50))
    call = next(e for e in evs if isinstance(e, ToolCall))
    assert call.extra == {"google": {"thought_signature": "SIG123"}}
    history = [{"role": "user", "content": "hi"},
               {"role": "assistant", "content": "", "tool_calls": [{"id": call.id, "name": call.name, "arguments": {}, "extra": call.extra}]},
               {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": "[]"}]
    await collect(p.stream(system="", messages=history, tools=[], model="m", temperature=0, max_tokens=50))
    echoed = sent[1]["messages"][1]["tool_calls"][0]
    assert echoed["extra_content"] == {"google": {"thought_signature": "SIG123"}}
