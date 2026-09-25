from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from tuicodingagent.errors import ProtocolError
from tuicodingagent.models import (
    ChatMessage,
    ProviderConfig,
    ProviderEventType,
    SystemPrompt,
    ToolResult,
)
from tuicodingagent.providers import OpenAIResponsesProvider, create_provider
from tuicodingagent.tools import create_default_registry


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _event(payload: dict[str, object], name: str | None = None) -> str:
    prefix = f"event: {name}\n" if name else ""
    return f"{prefix}data: {json.dumps(payload, ensure_ascii=False)}"


@pytest.mark.asyncio
async def test_responses_stream_function_call_replay_and_usage(tmp_path: Path) -> None:
    bodies: list[dict[str, object]] = []
    reasoning = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [],
        "encrypted_content": "opaque-value",
    }
    first = "\n\n".join(
        (
            _event(
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": reasoning,
                }
            ),
            _event(
                {
                    "type": "response.output_item.added",
                    "output_index": 1,
                    "item": {
                        "type": "function_call",
                        "id": "fc_1",
                        "call_id": "call_1",
                        "name": "read_file",
                        "arguments": "",
                    },
                }
            ),
            _event(
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "fc_1",
                    "delta": '{"path":"README.md"}',
                }
            ),
            _event(
                {
                    "type": "response.output_item.done",
                    "output_index": 1,
                    "item": {
                        "type": "function_call",
                        "id": "fc_1",
                        "call_id": "call_1",
                        "name": "read_file",
                        "arguments": '{"path":"README.md"}',
                    },
                }
            ),
            _event(
                {
                    "type": "response.completed",
                    "response": {
                        "output": [reasoning],
                        "usage": {
                            "input_tokens": 20,
                            "output_tokens": 8,
                            "input_tokens_details": {
                                "cached_tokens": 5,
                                "cache_write_tokens": 2,
                            },
                        },
                    },
                }
            ),
            "data: [DONE]",
            "",
        )
    )
    second = "\n\n".join(
        (
            _event(
                {
                    "type": "response.output_text.delta",
                    "item_id": "msg_2",
                    "delta": "完成",
                }
            ),
            _event(
                {
                    "type": "response.completed",
                    "response": {
                        "output": [],
                        "usage": {"input_tokens": 30, "output_tokens": 4},
                    },
                }
            ),
            "data: [DONE]",
            "",
        )
    )
    streams = iter((first, second))

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200, text=next(streams), headers={"content-type": "text/event-stream"}
        )

    config = ProviderConfig(
        "Responses",
        "openai",
        "gpt-5.5",
        "secret",
        "https://gateway.example",
        True,
        1_000_000,
        "responses",
    )
    client = _client(handler)
    provider = OpenAIResponsesProvider(config, client)
    registry = create_default_registry(tmp_path)
    events = [
        event
        async for event in provider.stream(
            SystemPrompt("稳定", "环境"),
            [ChatMessage("user", "读取")],
            registry.definitions(),
        )
    ]
    call = next(event.tool_call for event in events if event.type == ProviderEventType.TOOL_CALL)
    assert call is not None
    assert (call.id, call.name, call.arguments) == (
        "call_1",
        "read_file",
        {"path": "README.md"},
    )
    opaque = next(
        event.provider_item for event in events if event.type == ProviderEventType.PROVIDER_ITEM
    )
    usage = next(event for event in events if event.type == ProviderEventType.USAGE)
    assert (usage.input_tokens, usage.output_tokens) == (20, 8)
    assert (usage.cache_read_input_tokens, usage.cache_creation_input_tokens) == (5, 2)

    staged = (
        ChatMessage(
            "assistant",
            tool_calls=(call,),
            provider_items=(opaque,),  # type: ignore[arg-type]
        ),
        ChatMessage(
            "tool",
            tool_results=(ToolResult("call_1", "read_file", True, "内容"),),
        ),
    )
    followup = [
        event async for event in provider.stream(SystemPrompt("稳定", "环境二"), staged, ())
    ]
    assert "".join(event.text for event in followup) == "完成"

    first_body, second_body = bodies
    assert first_body["store"] is False
    assert first_body["include"] == ["reasoning.encrypted_content"]
    assert first_body["reasoning"] == {"effort": "high"}
    assert first_body["input"][0] == {"role": "system", "content": "环境"}
    assert len(first_body["tools"]) == 8
    assert second_body["input"][1]["type"] == "reasoning"
    assert second_body["input"][2]["type"] == "function_call"
    assert second_body["input"][3]["type"] == "function_call_output"
    assert str(bodies[0]).find("secret") == -1
    assert isinstance(create_provider(config, client), OpenAIResponsesProvider)
    await client.aclose()


@pytest.mark.asyncio
async def test_responses_error_is_typed_redacted_and_root_url_is_expanded() -> None:
    captured: list[str] = []
    streams = iter(
        (
            _event(
                {
                    "type": "response.failed",
                    "response": {"error": {"message": "bad secret"}},
                }
            ),
            "data: [DONE]\n\n",
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return httpx.Response(200, text=next(streams))

    config = ProviderConfig(
        "Responses", "openai", "m", "secret", "https://gateway.example", wire_api="responses"
    )
    client = _client(handler)
    provider = OpenAIResponsesProvider(config, client)
    with pytest.raises(ProtocolError, match="Responses 流式错误") as caught:
        _ = [event async for event in provider.stream("s", [ChatMessage("user", "u")])]
    assert "bad secret" not in str(caught.value)
    assert "bad ***" in str(caught.value)
    with pytest.raises(ProtocolError, match="空的 Responses 流"):
        _ = [event async for event in provider.stream("s", [ChatMessage("user", "u")])]
    assert captured == [
        "https://gateway.example/v1/responses",
        "https://gateway.example/v1/responses",
    ]
    await client.aclose()
