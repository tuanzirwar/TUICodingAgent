from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from tuicodingagent.models import (
    ChatMessage,
    ProviderConfig,
    ProviderEventType,
    SystemPrompt,
)
from tuicodingagent.providers import AnthropicProvider, OpenAIProvider
from tuicodingagent.tools import create_default_registry


def _client(handler: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_openai_keeps_stable_prefix_and_parses_optional_cache_usage(tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    stream = (
        'data: {"choices":[],"usage":{"prompt_tokens":20,"completion_tokens":3,'
        '"prompt_tokens_details":{"cached_tokens":12}}}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})

    config = ProviderConfig("OpenAI", "openai", "demo", "secret", "https://local/chat")
    client = _client(handler)
    provider = OpenAIProvider(config, client)
    prompt = SystemPrompt("稳定块", "动态环境 A")
    events = [event async for event in provider.stream(prompt, [ChatMessage("user", "问题")])]
    await client.aclose()

    messages = captured["messages"]
    assert messages[:2] == [
        {"role": "system", "content": "稳定块"},
        {"role": "system", "content": "动态环境 A"},
    ]
    usage = next(event for event in events if event.type == ProviderEventType.USAGE)
    assert usage.cache_creation_input_tokens == 0
    assert usage.cache_read_input_tokens == 12


@pytest.mark.asyncio
async def test_anthropic_marks_stable_system_and_tools_and_parses_cache_usage(
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    stream = "\n\n".join(
        [
            "event: message_start\n"
            'data: {"type":"message_start","message":{"usage":{"input_tokens":20,'
            '"cache_creation_input_tokens":15,"cache_read_input_tokens":7}}}',
            'event: message_stop\ndata: {"type":"message_stop"}',
            "",
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})

    config = ProviderConfig("Anthropic", "anthropic", "demo", "secret", "https://local/messages")
    client = _client(handler)
    provider = AnthropicProvider(config, client)
    prompt = SystemPrompt("稳定块", "动态环境 B")
    events = [
        event
        async for event in provider.stream(
            prompt,
            [ChatMessage("user", "问题")],
            create_default_registry(tmp_path).definitions(),
        )
    ]
    await client.aclose()

    system = captured["system"]
    assert system[0] == {
        "type": "text",
        "text": "稳定块",
        "cache_control": {"type": "ephemeral"},
    }
    assert system[1] == {"type": "text", "text": "动态环境 B"}
    assert "cache_control" not in system[1]
    assert captured["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    usage = next(event for event in events if event.type == ProviderEventType.USAGE)
    assert (usage.cache_creation_input_tokens, usage.cache_read_input_tokens) == (15, 7)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type", [OpenAIProvider, AnthropicProvider])
async def test_missing_cache_fields_are_zero(provider_type: type, tmp_path: Path) -> None:
    if provider_type is OpenAIProvider:
        stream = (
            'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":1}}\n\n'
            "data: [DONE]\n\n"
        )
        protocol = "openai"
    else:
        stream = "\n\n".join(
            [
                'event: message_start\ndata: {"type":"message_start",'
                '"message":{"usage":{"input_tokens":2}}}',
                'event: message_stop\ndata: {"type":"message_stop"}',
                "",
            ]
        )
        protocol = "anthropic"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})

    config = ProviderConfig("服务", protocol, "demo", "secret", "https://local/api")
    client = _client(handler)
    provider = provider_type(config, client)
    events = [event async for event in provider.stream("系统", [ChatMessage("user", "问")])]
    await client.aclose()
    usage = next(event for event in events if event.type == ProviderEventType.USAGE)
    assert usage.cache_creation_input_tokens == usage.cache_read_input_tokens == 0
