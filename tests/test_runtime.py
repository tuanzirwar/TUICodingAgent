from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from tuicodingagent.errors import AuthenticationError
from tuicodingagent.memory import MemoryManager
from tuicodingagent.models import (
    ChatMessage,
    ProviderConfig,
    ProviderEvent,
    ProviderEventType,
    RuntimeEventType,
    ToolDefinition,
)
from tuicodingagent.runtime import ConversationRuntime
from tuicodingagent.session import SessionContext, Writer


class FakeProvider:
    config = ProviderConfig("fake", "openai", "model", "secret")

    def __init__(self, replies: list[list[str]]) -> None:
        self.replies = replies
        self.requests: list[tuple[str, tuple[ChatMessage, ...]]] = []
        self.closed = False

    async def stream(
        self,
        system_prompt: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        self.requests.append((system_prompt, tuple(messages)))
        for chunk in self.replies.pop(0):
            yield ProviderEvent(ProviderEventType.TEXT_DELTA, text=chunk)

    async def close(self) -> None:
        self.closed = True


class ErrorProvider(FakeProvider):
    async def stream(
        self,
        system_prompt: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        self.requests.append((system_prompt, tuple(messages)))
        raise AuthenticationError("认证失败")
        yield ""  # pragma: no cover


@pytest.mark.asyncio
async def test_runtime_streams_and_carries_full_history() -> None:
    provider = FakeProvider([["第", "一"], ["第", "二"]])
    runtime = ConversationRuntime(provider, "系统提示")

    first = [event async for event in runtime.chat("问题一")]
    _ = [event async for event in runtime.chat("问题二")]

    assert [event.type for event in first] == [
        RuntimeEventType.STARTED,
        RuntimeEventType.ITERATION_STARTED,
        RuntimeEventType.TEXT_DELTA,
        RuntimeEventType.TEXT_DELTA,
        RuntimeEventType.COMPLETED,
    ]
    assert first[-1].text == "第一"
    assert provider.requests[0][1][0].content.startswith("问题一\n\n<system-reminder>")
    assert [message.role for message in provider.requests[1][1]] == [
        "user",
        "assistant",
        "user",
    ]
    assert provider.requests[1][1][-1].content.startswith("问题二\n\n<system-reminder>")
    assert all("system-reminder" not in message.content for message in runtime.history)
    assert len(runtime.history) == 4
    await runtime.close()
    assert provider.closed


@pytest.mark.asyncio
async def test_runtime_preserves_provider_items_without_rendering_them() -> None:
    class OpaqueProvider(FakeProvider):
        async def stream(
            self,
            system_prompt: str,
            messages: Sequence[ChatMessage],
            tools: Sequence[ToolDefinition] = (),
        ) -> AsyncIterator[ProviderEvent]:
            self.requests.append((system_prompt, tuple(messages)))
            yield ProviderEvent(
                ProviderEventType.PROVIDER_ITEM,
                provider_item={
                    "type": "reasoning",
                    "id": "rs_1",
                    "encrypted_content": "opaque",
                },
            )
            yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="完成")

    provider = OpaqueProvider([])
    runtime = ConversationRuntime(provider)
    events = [event async for event in runtime.chat("任务")]

    assert events[-1].text == "完成"
    assistant = runtime.history[-1]
    assert assistant.content == "完成"
    assert assistant.provider_items == (
        {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"},
    )


@pytest.mark.asyncio
async def test_failure_does_not_pollute_history_and_can_continue() -> None:
    runtime = ConversationRuntime(ErrorProvider([]))
    events = [event async for event in runtime.chat("失败请求")]
    assert events[-1].type == RuntimeEventType.FAILED
    assert runtime.history == ()
    events = [event async for event in runtime.chat("再次请求")]
    assert events[-1].type == RuntimeEventType.FAILED


@pytest.mark.asyncio
async def test_runtime_rejects_empty_and_concurrent_requests() -> None:
    gate = asyncio.Event()

    class WaitingProvider(FakeProvider):
        async def stream(
            self,
            system_prompt: str,
            messages: Sequence[ChatMessage],
            tools: Sequence[ToolDefinition] = (),
        ) -> AsyncIterator[ProviderEvent]:
            await gate.wait()
            yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="ok")

    runtime = ConversationRuntime(WaitingProvider([]))
    with pytest.raises(ValueError, match="不能为空"):
        _ = [event async for event in runtime.chat("  ")]

    first = runtime.chat("first")
    assert (await anext(first)).type == RuntimeEventType.STARTED
    with pytest.raises(RuntimeError, match="仍在生成"):
        _ = [event async for event in runtime.chat("second")]
    gate.set()
    _ = [event async for event in first]


@pytest.mark.asyncio
async def test_runtime_persists_messages_and_runs_explicit_memory_update(
    tmp_path: Path,
) -> None:
    operation = [
        {
            "action": "create",
            "level": "user",
            "type": "user_preference",
            "title": "中文回复",
            "slug": "chinese_replies",
            "content": "用户偏好使用中文回复。",
        }
    ]
    provider = FakeProvider([["已记住"], [json.dumps(operation, ensure_ascii=False)]])
    context = SessionContext.create(tmp_path / "project")
    writer = Writer(context, "model")
    memory = MemoryManager(tmp_path / "project", tmp_path / "home")
    runtime = ConversationRuntime(provider, session_writer=writer, memory_manager=memory)

    _ = [event async for event in runtime.chat("请记住使用中文回复")]
    await runtime.close()

    records = [
        json.loads(line)
        for line in context.conversation_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [record["role"] for record in records] == ["user", "assistant"]
    assert records[0]["model"] == "model"
    note = tmp_path / "home" / ".tuicodingagent" / "memory" / "user_preference_chinese_replies.md"
    assert note.is_file()
    assert provider.requests[1][0] and provider.requests[1][1][0].role == "user"
