"""Anthropic Messages 流式协议适配器。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence

import httpx

from ..errors import ProtocolError
from ..models import (
    ChatMessage,
    ProviderConfig,
    ProviderEvent,
    ProviderEventType,
    SystemPrompt,
    ToolCall,
    ToolDefinition,
)
from .base import BaseHTTPProvider, normalize_http_error
from .sse import iter_sse

DEFAULT_URL = "https://api.anthropic.com/v1/messages"


class AnthropicProvider(BaseHTTPProvider):
    def __init__(self, config: ProviderConfig, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(config, client)

    async def stream(
        self,
        system_prompt: SystemPrompt | str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        prompt = _normalize_prompt(system_prompt)
        payload: dict[str, object] = {
            "model": self.config.model,
            "system": _system_blocks(prompt),
            "messages": _serialize_messages(messages),
            "max_tokens": 8192,
            "stream": True,
        }
        if self.config.thinking:
            payload["thinking"] = {"type": "enabled", "budget_tokens": 4096}
        if tools:
            tool_payload = [
                {
                    "name": item.name,
                    "description": item.description,
                    "input_schema": item.input_schema,
                }
                for item in tools
            ]
            tool_payload[-1]["cache_control"] = {"type": "ephemeral"}
            payload["tools"] = tool_payload
        headers = {
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        pending_calls: dict[int, dict[str, str]] = {}
        try:
            async with self.client.stream(
                "POST", self.config.base_url or DEFAULT_URL, headers=headers, json=payload
            ) as response:
                await self.check_response(response)
                async for event in iter_sse(response.aiter_lines()):
                    if event.event == "message_stop":
                        break
                    try:
                        data = json.loads(event.data)
                        if data.get("type") == "error":
                            raise ProtocolError(f"{self.config.name} 返回了 Anthropic 协议错误")
                        if data.get("type") == "message_start":
                            usage = (data.get("message") or {}).get("usage") or {}
                            input_tokens = int(usage.get("input_tokens") or 0)
                            cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
                            cache_read = int(usage.get("cache_read_input_tokens") or 0)
                            if input_tokens or cache_creation or cache_read:
                                yield ProviderEvent(
                                    ProviderEventType.USAGE,
                                    input_tokens=input_tokens,
                                    cache_creation_input_tokens=cache_creation,
                                    cache_read_input_tokens=cache_read,
                                )
                            continue
                        if data.get("type") == "message_delta":
                            usage = data.get("usage") or {}
                            output_tokens = int(usage.get("output_tokens") or 0)
                            if output_tokens:
                                yield ProviderEvent(
                                    ProviderEventType.USAGE,
                                    output_tokens=output_tokens,
                                )
                            continue
                        if data.get("type") == "content_block_start":
                            block = data.get("content_block", {})
                            if block.get("type") == "tool_use":
                                index = int(data.get("index", 0))
                                initial = block.get("input") or {}
                                pending_calls[index] = {
                                    "id": str(block.get("id") or f"tool_{index}"),
                                    "name": str(block.get("name") or ""),
                                    "arguments": (
                                        json.dumps(initial, ensure_ascii=False) if initial else ""
                                    ),
                                }
                            continue
                        if data.get("type") == "content_block_stop":
                            index = int(data.get("index", 0))
                            pending = pending_calls.pop(index, None)
                            if pending is not None:
                                yield ProviderEvent(
                                    ProviderEventType.TOOL_CALL,
                                    tool_call=_tool_call(index, pending),
                                )
                            continue
                        if data.get("type") != "content_block_delta":
                            continue
                        delta = data.get("delta", {})
                        if delta.get("type") == "text_delta":
                            text = delta.get("text")
                            if isinstance(text, str) and text:
                                yield ProviderEvent(ProviderEventType.TEXT_DELTA, text=text)
                        elif delta.get("type") == "input_json_delta":
                            index = int(data.get("index", 0))
                            pending = pending_calls.setdefault(
                                index,
                                {"id": f"tool_{index}", "name": "", "arguments": ""},
                            )
                            pending["arguments"] += str(delta.get("partial_json") or "")
                    except (json.JSONDecodeError, AttributeError, TypeError) as exc:
                        raise ProtocolError(
                            f"{self.config.name} 返回了无效的 Anthropic SSE 事件"
                        ) from exc
                for index in sorted(pending_calls):
                    yield ProviderEvent(
                        ProviderEventType.TOOL_CALL,
                        tool_call=_tool_call(index, pending_calls[index]),
                    )
        except Exception as exc:
            raise normalize_http_error(exc, self.config) from exc


def _normalize_prompt(prompt: SystemPrompt | str) -> SystemPrompt:
    return prompt if isinstance(prompt, SystemPrompt) else SystemPrompt(prompt)


def _system_blocks(prompt: SystemPrompt) -> str | list[dict[str, object]]:
    if not prompt.environment:
        return prompt.stable
    return [
        {
            "type": "text",
            "text": prompt.stable,
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": prompt.environment},
    ]


def _tool_call(index: int, pending: dict[str, str]) -> ToolCall:
    raw = pending["arguments"] or "{}"
    try:
        arguments = json.loads(raw)
        if not isinstance(arguments, dict):
            raise TypeError("参数必须是 JSON 对象")
        return ToolCall(pending["id"] or f"tool_{index}", pending["name"], arguments, raw)
    except (json.JSONDecodeError, TypeError) as exc:
        return ToolCall(
            pending["id"] or f"tool_{index}",
            pending["name"],
            {},
            raw,
            str(exc),
        )


def _serialize_messages(messages: Sequence[ChatMessage]) -> list[dict[str, object]]:
    serialized: list[dict[str, object]] = []
    for message in messages:
        if message.role == "assistant" and message.tool_calls:
            content: list[dict[str, object]] = []
            if message.content:
                content.append({"type": "text", "text": message.content})
            content.extend(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": call.arguments,
                }
                for call in message.tool_calls
            )
            serialized.append({"role": "assistant", "content": content})
        elif message.role == "tool":
            serialized.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": result.tool_call_id,
                            "content": result.model_content(),
                            "is_error": not result.success,
                        }
                        for result in message.tool_results
                    ],
                }
            )
        else:
            serialized.append({"role": message.role, "content": message.content})
    return serialized
