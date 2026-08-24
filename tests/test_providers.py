from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from tuicodingagent.errors import AuthenticationError, NetworkError, ProtocolError, RateLimitError
from tuicodingagent.models import ChatMessage, ProviderConfig, ProviderEventType
from tuicodingagent.providers import AnthropicProvider, OpenAIProvider, create_provider

FIXTURES = Path(__file__).parent / "fixtures"


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_openai_request_stream_and_thinking_filter() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers["authorization"]
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            text=(FIXTURES / "openai_stream.txt").read_text(encoding="utf-8"),
            headers={"content-type": "text/event-stream"},
        )

    config = ProviderConfig("OpenAI", "openai", "demo", "secret", "https://local/chat", True)
    client = _client(handler)
    provider = OpenAIProvider(config, client)
    chunks = [
        chunk
        async for chunk in provider.stream(
            "系统", [ChatMessage("user", "一"), ChatMessage("assistant", "二")]
        )
    ]
    await client.aclose()

    assert [event.text for event in chunks if event.type == ProviderEventType.TEXT_DELTA] == [
        "你",
        "好",
    ]
    assert captured["url"] == "https://local/chat"
    assert captured["auth"] == "Bearer secret"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["reasoning_effort"] == "high"
    assert body["messages"][0] == {"role": "system", "content": "系统"}
    assert len(body["messages"]) == 3


@pytest.mark.asyncio
async def test_anthropic_request_stream_and_thinking_filter() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["key"] = request.headers["x-api-key"]
        captured["version"] = request.headers["anthropic-version"]
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            text=(FIXTURES / "anthropic_stream.txt").read_text(encoding="utf-8"),
            headers={"content-type": "text/event-stream"},
        )

    config = ProviderConfig(
        "Anthropic", "anthropic", "claude-demo", "secret", "https://local/messages", True
    )
    client = _client(handler)
    provider = AnthropicProvider(config, client)
    chunks = [chunk async for chunk in provider.stream("系统", [ChatMessage("user", "你好")])]
    await client.aclose()

    assert [event.text for event in chunks if event.type == ProviderEventType.TEXT_DELTA] == [
        "你",
        "好",
    ]
    assert captured["url"] == "https://local/messages"
    assert captured["key"] == "secret"
    assert captured["version"] == "2023-06-01"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["system"] == "系统"
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 4096}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_type"),
    [(401, AuthenticationError), (429, RateLimitError)],
)
async def test_http_errors_are_typed_and_secret_is_redacted(status: int, error_type: type) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "bad secret-key"}})

    config = ProviderConfig("Demo", "openai", "m", "secret-key")
    client = _client(handler)
    provider = OpenAIProvider(config, client)
    with pytest.raises(error_type) as caught:
        _ = [chunk async for chunk in provider.stream("s", [ChatMessage("user", "u")])]
    await client.aclose()
    assert "secret-key" not in str(caught.value)
    assert "***" in str(caught.value)


def test_factory_selects_protocol() -> None:
    openai = ProviderConfig("o", "openai", "m", "k")
    anthropic = ProviderConfig("a", "anthropic", "m", "k")
    assert isinstance(create_provider(openai), OpenAIProvider)
    assert isinstance(create_provider(anthropic), AnthropicProvider)


@pytest.mark.asyncio
async def test_default_urls_and_network_error() -> None:
    urls: list[str] = []

    def ok_handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if "anthropic" in request.url.host:
            return httpx.Response(
                200, text='event: message_stop\ndata: {"type":"message_stop"}\n\n'
            )
        return httpx.Response(
            200,
            text=('data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'),
        )

    openai_config = ProviderConfig("o", "openai", "m", "k")
    anthropic_config = ProviderConfig("a", "anthropic", "m", "k")
    client = _client(ok_handler)
    _ = [chunk async for chunk in OpenAIProvider(openai_config, client).stream("s", [])]
    _ = [chunk async for chunk in AnthropicProvider(anthropic_config, client).stream("s", [])]
    await client.aclose()
    assert urls == [
        "https://api.openai.com/v1/chat/completions",
        "https://api.anthropic.com/v1/messages",
    ]

    def failed_handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret detail")

    failed_client = _client(failed_handler)
    with pytest.raises(NetworkError, match="网络连接失败"):
        _ = [chunk async for chunk in OpenAIProvider(openai_config, failed_client).stream("s", [])]
    await failed_client.aclose()


@pytest.mark.asyncio
async def test_openai_stream_error_and_empty_stream_are_not_silent() -> None:
    responses = iter(
        (
            'data: {"error":{"message":"bad secret"}}\n\n',
            "data: [DONE]\n\n",
        )
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=next(responses))

    config = ProviderConfig("OpenAI", "openai", "m", "secret")
    client = _client(handler)
    with pytest.raises(ProtocolError, match="流式错误") as caught:
        _ = [event async for event in OpenAIProvider(config, client).stream("s", [])]
    assert "bad secret" not in str(caught.value)
    assert "bad ***" in str(caught.value)
    with pytest.raises(ProtocolError, match="空的流式响应"):
        _ = [event async for event in OpenAIProvider(config, client).stream("s", [])]
    await client.aclose()
