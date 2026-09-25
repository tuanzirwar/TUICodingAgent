from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from tuicodingagent.models import (
    ChatMessage,
    ProviderConfig,
    ProviderEventType,
    ToolCall,
    ToolResult,
)
from tuicodingagent.providers import AnthropicProvider, OpenAIProvider
from tuicodingagent.tools import create_default_registry


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _sse(data: object, event: str | None = None) -> str:
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {json.dumps(data, ensure_ascii=False)}"


@pytest.mark.asyncio
async def test_openai_assembles_fragmented_parallel_calls_and_serializes_history(
    tmp_path: Path,
) -> None:
    bodies: list[dict[str, object]] = []
    fragments = [
        [
            {
                "index": 0,
                "id": "call_",
                "function": {"name": "read_", "arguments": '{"pa'},
            },
            {
                "index": 1,
                "id": "call_b",
                "function": {"name": "gl", "arguments": '{"pat'},
            },
        ],
        [
            {
                "index": 0,
                "id": "a",
                "function": {"name": "file", "arguments": 'th":"README.md"}'},
            },
            {
                "index": 1,
                "function": {"name": "ob", "arguments": 'tern":"*.py"}'},
            },
        ],
    ]
    stream = "\n\n".join(
        [
            *(_sse({"choices": [{"delta": {"tool_calls": item}}]}) for item in fragments),
            _sse(
                {
                    "choices": [],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 3},
                }
            ),
            "data: [DONE]",
            "",
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})

    registry = create_default_registry(tmp_path)
    config = ProviderConfig("OpenAI", "openai", "demo", "secret", "https://local/chat")
    client = _client(handler)
    provider = OpenAIProvider(config, client)
    events = [
        event
        async for event in provider.stream(
            "系统", [ChatMessage("user", "读")], registry.definitions()
        )
    ]
    calls = [event.tool_call for event in events if event.type == ProviderEventType.TOOL_CALL]
    assert [(item.id, item.name, item.arguments) for item in calls if item] == [
        ("call_a", "read_file", {"path": "README.md"}),
        ("call_b", "glob", {"pattern": "*.py"}),
    ]
    assert len(bodies[0]["tools"]) == 8
    usage = next(event for event in events if event.type == ProviderEventType.USAGE)
    assert (usage.input_tokens, usage.output_tokens) == (12, 3)
    assert bodies[0]["stream_options"] == {"include_usage": True}

    staged = [
        ChatMessage("assistant", tool_calls=(calls[0],)),
        ChatMessage("tool", tool_results=(ToolResult("call_a", "read_file", True, "内容"),)),
    ]
    _ = [event async for event in provider.stream("系统", staged, registry.definitions())]
    messages = bodies[1]["messages"]
    assert messages[1]["tool_calls"][0]["function"]["arguments"] == '{"path":"README.md"}'
    assert messages[2]["role"] == "tool" and messages[2]["tool_call_id"] == "call_a"
    await client.aclose()


@pytest.mark.asyncio
async def test_anthropic_assembles_tool_use_and_serializes_tool_results(tmp_path: Path) -> None:
    bodies: list[dict[str, object]] = []
    start = {
        "type": "content_block_start",
        "index": 0,
        "content_block": {
            "type": "tool_use",
            "id": "tool-a",
            "name": "read_file",
            "input": {},
        },
    }
    delta_one = {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "input_json_delta", "partial_json": '{"path":'},
    }
    delta_two = {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "input_json_delta", "partial_json": '"README.md"}'},
    }
    stream = "\n\n".join(
        [
            _sse(
                {
                    "type": "message_start",
                    "message": {"usage": {"input_tokens": 9}},
                },
                "message_start",
            ),
            _sse(start, "content_block_start"),
            _sse(delta_one, "content_block_delta"),
            _sse(delta_two, "content_block_delta"),
            _sse({"type": "content_block_stop", "index": 0}, "content_block_stop"),
            _sse(
                {"type": "message_delta", "usage": {"output_tokens": 5}},
                "message_delta",
            ),
            _sse({"type": "message_stop"}, "message_stop"),
            "",
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})

    registry = create_default_registry(tmp_path)
    config = ProviderConfig("Anthropic", "anthropic", "demo", "secret", "https://local/messages")
    client = _client(handler)
    provider = AnthropicProvider(config, client)
    events = [
        event
        async for event in provider.stream(
            "系统", [ChatMessage("user", "读")], registry.definitions()
        )
    ]
    tool_call = next(
        event.tool_call for event in events if event.type == ProviderEventType.TOOL_CALL
    )
    assert tool_call == ToolCall(
        "tool-a", "read_file", {"path": "README.md"}, '{"path":"README.md"}'
    )
    assert len(bodies[0]["tools"]) == 8
    usages = [event for event in events if event.type == ProviderEventType.USAGE]
    assert [(item.input_tokens, item.output_tokens) for item in usages] == [(9, 0), (0, 5)]

    staged = [
        ChatMessage("assistant", tool_calls=(tool_call,)),
        ChatMessage(
            "tool",
            tool_results=(ToolResult("tool-a", "read_file", False, error="不存在"),),
        ),
    ]
    _ = [event async for event in provider.stream("系统", staged, registry.definitions())]
    messages = bodies[1]["messages"]
    assert messages[0]["content"][0]["type"] == "tool_use"
    assert messages[1]["content"][0]["type"] == "tool_result"
    assert messages[1]["content"][0]["is_error"] is True
    await client.aclose()
