"""OpenAI Responses API 流式协议适配器。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from copy import deepcopy
from urllib.parse import urlsplit, urlunsplit

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

DEFAULT_URL = "https://api.openai.com/v1/responses"


class OpenAIResponsesProvider(BaseHTTPProvider):
    """以无服务端存储方式管理完整 Responses 上下文。"""

    async def stream(
        self,
        system_prompt: SystemPrompt | str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        prompt = _normalize_prompt(system_prompt)
        payload: dict[str, object] = {
            "model": self.config.model,
            "instructions": prompt.stable,
            "input": _serialize_input(prompt.environment, messages),
            "stream": True,
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        if self.config.thinking:
            payload["reasoning"] = {"effort": "high"}
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "name": item.name,
                    "description": item.description,
                    "parameters": item.input_schema,
                }
                for item in tools
            ]

        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        pending: dict[str, dict[str, str]] = {}
        emitted_calls: set[str] = set()
        emitted_items: set[str] = set()
        saw_output = False
        try:
            async with self.client.stream(
                "POST", _endpoint(self.config.base_url), headers=headers, json=payload
            ) as response:
                await self.check_response(response)
                async for event in iter_sse(response.aiter_lines()):
                    if event.data == "[DONE]":
                        break
                    try:
                        data = json.loads(event.data)
                        event_type = str(data.get("type") or event.event or "")
                        if event_type in {"error", "response.failed"}:
                            raise ProtocolError(_response_error(self.config, data))
                        if event_type == "response.incomplete":
                            raise ProtocolError(
                                f"{self.config.name} 的 Responses 输出不完整："
                                f"{_incomplete_reason(data)}"
                            )
                        if event_type in {"response.output_text.delta", "response.refusal.delta"}:
                            delta = data.get("delta")
                            if isinstance(delta, str) and delta:
                                saw_output = True
                                yield ProviderEvent(ProviderEventType.TEXT_DELTA, text=delta)
                            continue
                        if event_type == "response.output_item.added":
                            item = data.get("item") or {}
                            if isinstance(item, dict) and item.get("type") == "function_call":
                                _merge_call(pending, item, data)
                            continue
                        if event_type == "response.function_call_arguments.delta":
                            key = _item_key(data)
                            call = pending.setdefault(key, {"id": "", "name": "", "arguments": ""})
                            call["arguments"] += str(data.get("delta") or "")
                            continue
                        if event_type == "response.function_call_arguments.done":
                            key = _item_key(data)
                            call = pending.setdefault(key, {"id": "", "name": "", "arguments": ""})
                            call["id"] = str(data.get("call_id") or call["id"] or key)
                            call["name"] = str(data.get("name") or call["name"])
                            call["arguments"] = str(data.get("arguments") or call["arguments"])
                            continue
                        if event_type == "response.output_item.done":
                            item = data.get("item") or {}
                            if not isinstance(item, dict):
                                continue
                            if item.get("type") == "function_call":
                                key = _merge_call(pending, item, data)
                                call = _tool_call(pending[key])
                                if call.id not in emitted_calls:
                                    emitted_calls.add(call.id)
                                    saw_output = True
                                    yield ProviderEvent(ProviderEventType.TOOL_CALL, tool_call=call)
                            elif item.get("type") == "reasoning":
                                identifier = str(item.get("id") or _item_key(data))
                                if identifier not in emitted_items:
                                    emitted_items.add(identifier)
                                    yield ProviderEvent(
                                        ProviderEventType.PROVIDER_ITEM,
                                        provider_item=deepcopy(item),
                                    )
                            continue
                        if event_type == "response.completed":
                            completed = data.get("response") or {}
                            if isinstance(completed, dict):
                                for item in completed.get("output") or []:
                                    if not isinstance(item, dict):
                                        continue
                                    if item.get("type") == "function_call":
                                        key = _merge_call(pending, item, data)
                                        call = _tool_call(pending[key])
                                        if call.id not in emitted_calls:
                                            emitted_calls.add(call.id)
                                            saw_output = True
                                            yield ProviderEvent(
                                                ProviderEventType.TOOL_CALL, tool_call=call
                                            )
                                    elif item.get("type") == "reasoning":
                                        identifier = str(item.get("id") or len(emitted_items))
                                        if identifier not in emitted_items:
                                            emitted_items.add(identifier)
                                            yield ProviderEvent(
                                                ProviderEventType.PROVIDER_ITEM,
                                                provider_item=deepcopy(item),
                                            )
                                usage_event = _usage(completed.get("usage"))
                                if usage_event is not None:
                                    yield usage_event
                    except (json.JSONDecodeError, AttributeError, TypeError) as exc:
                        raise ProtocolError(
                            f"{self.config.name} 返回了无效的 Responses SSE 事件"
                        ) from exc

                for call_data in pending.values():
                    call = _tool_call(call_data)
                    if call.id not in emitted_calls:
                        emitted_calls.add(call.id)
                        saw_output = True
                        yield ProviderEvent(ProviderEventType.TOOL_CALL, tool_call=call)
                if not saw_output:
                    raise ProtocolError(f"{self.config.name} 返回了空的 Responses 流")
        except Exception as exc:
            raise normalize_http_error(exc, self.config) from exc


def _normalize_prompt(prompt: SystemPrompt | str) -> SystemPrompt:
    return prompt if isinstance(prompt, SystemPrompt) else SystemPrompt(prompt)


def _endpoint(base_url: str | None) -> str:
    if not base_url:
        return DEFAULT_URL
    value = base_url.rstrip("/")
    path = urlsplit(value).path.rstrip("/")
    if path.endswith("/responses"):
        return value
    suffix = "/responses" if path.endswith("/v1") else "/v1/responses"
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + suffix, "", ""))


def _serialize_input(environment: str, messages: Sequence[ChatMessage]) -> list[dict[str, object]]:
    serialized: list[dict[str, object]] = []
    if environment:
        serialized.append({"role": "system", "content": environment})
    for message in messages:
        if message.role == "assistant":
            serialized.extend(deepcopy(list(message.provider_items)))
            if message.content:
                serialized.append({"role": "assistant", "content": message.content})
            serialized.extend(
                {
                    "type": "function_call",
                    "call_id": call.id,
                    "name": call.name,
                    "arguments": call.raw_arguments
                    or json.dumps(call.arguments, ensure_ascii=False),
                }
                for call in message.tool_calls
            )
        elif message.role == "tool":
            serialized.extend(
                {
                    "type": "function_call_output",
                    "call_id": result.tool_call_id,
                    "output": result.model_content(),
                }
                for result in message.tool_results
            )
        else:
            serialized.append({"role": "user", "content": message.content})
    return serialized


def _item_key(data: dict[str, object]) -> str:
    return str(data.get("item_id") or data.get("output_index") or "0")


def _merge_call(
    pending: dict[str, dict[str, str]], item: dict[str, object], event: dict[str, object]
) -> str:
    key = str(item.get("id") or _item_key(event))
    current = pending.setdefault(key, {"id": "", "name": "", "arguments": ""})
    current["id"] = str(item.get("call_id") or current["id"] or key)
    current["name"] = str(item.get("name") or current["name"])
    arguments = item.get("arguments")
    if isinstance(arguments, str) and arguments:
        current["arguments"] = arguments
    return key


def _tool_call(pending: dict[str, str]) -> ToolCall:
    raw = pending["arguments"] or "{}"
    try:
        arguments = json.loads(raw)
        if not isinstance(arguments, dict):
            raise TypeError("参数必须是 JSON 对象")
        return ToolCall(pending["id"], pending["name"], arguments, raw)
    except (json.JSONDecodeError, TypeError) as exc:
        return ToolCall(pending["id"], pending["name"], {}, raw, str(exc))


def _usage(raw: object) -> ProviderEvent | None:
    if not isinstance(raw, dict):
        return None
    details = raw.get("input_tokens_details") or {}
    if not isinstance(details, dict):
        details = {}
    input_tokens = int(raw.get("input_tokens") or 0)
    output_tokens = int(raw.get("output_tokens") or 0)
    cache_read = int(details.get("cached_tokens") or 0)
    cache_creation = int(details.get("cache_write_tokens") or 0)
    if not any((input_tokens, output_tokens, cache_read, cache_creation)):
        return None
    return ProviderEvent(
        ProviderEventType.USAGE,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_creation,
    )


def _response_error(config: ProviderConfig, data: dict[str, object]) -> str:
    response = data.get("response") or data
    error = response.get("error") if isinstance(response, dict) else response
    if isinstance(error, dict):
        detail = str(error.get("message") or error.get("code") or "未知错误")
    else:
        detail = str(error or "未知错误")
    detail = detail.replace(config.api_key, "***").replace("\r", " ").replace("\n", " ")[:200]
    return f"{config.name} 返回 Responses 流式错误：{detail}"


def _incomplete_reason(data: dict[str, object]) -> str:
    response = data.get("response") or {}
    details = response.get("incomplete_details") if isinstance(response, dict) else {}
    return str(details.get("reason") if isinstance(details, dict) else details or "未知原因")
