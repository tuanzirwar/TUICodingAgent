"""OpenAI Chat Completions 流式协议适配器。"""

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

DEFAULT_URL = "https://api.openai.com/v1/chat/completions"


class OpenAIProvider(BaseHTTPProvider):
    def __init__(self, config: ProviderConfig, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(config, client)

    async def stream(
        self,
        system_prompt: SystemPrompt | str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        prompt = _normalize_prompt(system_prompt)
        request_messages = [{"role": "system", "content": prompt.stable}]
        if prompt.environment:
            request_messages.append({"role": "system", "content": prompt.environment})
        request_messages.extend(_serialize_messages(messages))
        payload: dict[str, object] = {
            "model": self.config.model,
            "messages": request_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if self.config.thinking:
            payload["reasoning_effort"] = "high"
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": item.name,
                        "description": item.description,
                        "parameters": item.input_schema,
                    },
                }
                for item in tools
            ]
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        pending_calls: dict[int, dict[str, str]] = {}
        saw_output = False
        saw_usage = False
        try:
            async with self.client.stream(
                "POST", self.config.base_url or DEFAULT_URL, headers=headers, json=payload
            ) as response:
                await self.check_response(response)
                async for event in iter_sse(response.aiter_lines()):
                    if event.data == "[DONE]":
                        break
                    try:
                        data = json.loads(event.data)
                        if data.get("error"):
                            raise ProtocolError(
                                _stream_error(self.config.name, data, self.config.api_key)
                            )
                        usage = data.get("usage") or {}
                        input_tokens = int(usage.get("prompt_tokens") or 0)
                        output_tokens = int(usage.get("completion_tokens") or 0)
                        details = usage.get("prompt_tokens_details") or {}
                        cache_read = int(
                            details.get("cached_tokens")
                            or usage.get("cache_read_input_tokens")
                            or 0
                        )
                        cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
                        if input_tokens or output_tokens or cache_read or cache_creation:
                            saw_usage = True
                            yield ProviderEvent(
                                ProviderEventType.USAGE,
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                                cache_creation_input_tokens=cache_creation,
                                cache_read_input_tokens=cache_read,
                            )
                        choices = data.get("choices", [])
                        if not choices:
                            continue
                        delta = choices[0].get("delta", {})
                        content = delta.get("content")
                        if isinstance(content, str) and content:
                            saw_output = True
                            yield ProviderEvent(ProviderEventType.TEXT_DELTA, text=content)
                        for fragment in delta.get("tool_calls") or []:
                            saw_output = True
                            index = int(fragment.get("index", 0))
                            pending = pending_calls.setdefault(
                                index, {"id": "", "name": "", "arguments": ""}
                            )
                            pending["id"] += str(fragment.get("id") or "")
                            function = fragment.get("function") or {}
                            pending["name"] += str(function.get("name") or "")
                            pending["arguments"] += str(function.get("arguments") or "")
                    except (json.JSONDecodeError, AttributeError, IndexError, TypeError) as exc:
                        raise ProtocolError(
                            f"{self.config.name} 返回了无效的 OpenAI SSE 事件"
                        ) from exc
                for index in sorted(pending_calls):
                    yield ProviderEvent(
                        ProviderEventType.TOOL_CALL,
                        tool_call=_tool_call(index, pending_calls[index]),
                    )
                if not saw_output and not saw_usage:
                    raise ProtocolError(f"{self.config.name} 返回了空的流式响应")
        except Exception as exc:
            raise normalize_http_error(exc, self.config) from exc


def _normalize_prompt(prompt: SystemPrompt | str) -> SystemPrompt:
    return prompt if isinstance(prompt, SystemPrompt) else SystemPrompt(prompt)


def _stream_error(name: str, payload: dict[str, object], secret: str) -> str:
    """提取短流式错误，确保密钥不会进入异常文本。"""

    raw = payload.get("error")
    if isinstance(raw, dict):
        detail = str(raw.get("message") or raw.get("code") or "未知错误")
    else:
        detail = str(raw or "未知错误")
    detail = detail.replace(secret, "***").replace("\r", " ").replace("\n", " ")[:200]
    return f"{name} 返回流式错误：{detail}"


def _tool_call(index: int, pending: dict[str, str]) -> ToolCall:
    raw = pending["arguments"] or "{}"
    try:
        arguments = json.loads(raw)
        if not isinstance(arguments, dict):
            raise TypeError("参数必须是 JSON 对象")
        return ToolCall(pending["id"] or f"call_{index}", pending["name"], arguments, raw)
    except (json.JSONDecodeError, TypeError) as exc:
        return ToolCall(
            pending["id"] or f"call_{index}",
            pending["name"],
            {},
            raw,
            str(exc),
        )


def _serialize_messages(messages: Sequence[ChatMessage]) -> list[dict[str, object]]:
    serialized: list[dict[str, object]] = []
    for message in messages:
        if message.role == "assistant" and message.tool_calls:
            serialized.append(
                {
                    "role": "assistant",
                    "content": message.content or None,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": call.raw_arguments
                                or json.dumps(call.arguments, ensure_ascii=False),
                            },
                        }
                        for call in message.tool_calls
                    ],
                }
            )
        elif message.role == "tool":
            serialized.extend(
                {
                    "role": "tool",
                    "tool_call_id": result.tool_call_id,
                    "content": result.model_content(),
                }
                for result in message.tool_results
            )
        else:
            serialized.append({"role": message.role, "content": message.content})
    return serialized
