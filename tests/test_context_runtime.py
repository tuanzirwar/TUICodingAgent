from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from tuicodingagent.context import ContextController
from tuicodingagent.errors import ProviderError
from tuicodingagent.models import (
    ChatMessage,
    ProviderConfig,
    ProviderEvent,
    ProviderEventType,
    RuntimeEventType,
    SystemPrompt,
    ToolDefinition,
)
from tuicodingagent.runtime import ConversationRuntime

SUMMARY = (
    "<analysis>草稿</analysis><summary>1. 请求 2. 概念 3. 文件 4. 错误 5. 过程 "
    "6. 原话 7. 待办 8. 当前 9. 下一步</summary>"
)


class ContextProvider:
    def __init__(self, *, main_failures: int = 0, context_window: int = 100_000) -> None:
        self.config = ProviderConfig(
            "fake", "openai", "model", "secret", context_window=context_window
        )
        self.main_failures = main_failures
        self.main_calls = 0
        self.summary_calls = 0
        self.requests: list[tuple[str, Sequence[ChatMessage], Sequence[ToolDefinition]]] = []

    async def stream(self, system_prompt, messages, tools=()) -> AsyncIterator[ProviderEvent]:
        is_summary = isinstance(system_prompt, SystemPrompt) and "九个编号部分" in str(
            system_prompt
        )
        self.requests.append(("summary" if is_summary else "main", messages, tools))
        if is_summary:
            self.summary_calls += 1
            yield ProviderEvent(ProviderEventType.TEXT_DELTA, text=SUMMARY)
            return
        self.main_calls += 1
        if self.main_calls <= self.main_failures:
            raise ProviderError("prompt_too_long")
        yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="正常完成")
        yield ProviderEvent(
            ProviderEventType.USAGE,
            input_tokens=50,
            output_tokens=10,
            cache_creation_input_tokens=5,
            cache_read_input_tokens=20,
        )

    async def close(self) -> None:
        return None


async def test_auto_compacts_before_normal_request(tmp_path: Path) -> None:
    provider = ContextProvider(context_window=33_001)
    controller = ContextController.for_provider(tmp_path, provider)
    runtime = ConversationRuntime(provider, workspace=tmp_path, context_controller=controller)
    events = [event async for event in runtime.chat("触发自动压缩")]
    statuses = [event.text for event in events if event.type == RuntimeEventType.CONTEXT_STATUS]
    assert provider.summary_calls == 1 and provider.main_calls == 1
    assert statuses[0] == "正在压缩上下文..."
    assert "已压缩，token 从" in statuses[1]
    assert provider.requests[0][0] == "summary" and provider.requests[0][2] == ()
    assert provider.requests[1][0] == "main"
    assert events[-1].type == RuntimeEventType.COMPLETED


async def test_prompt_too_long_emergency_compacts_and_retries_once(tmp_path: Path) -> None:
    provider = ContextProvider(main_failures=1)
    controller = ContextController.for_provider(tmp_path, provider)
    runtime = ConversationRuntime(provider, workspace=tmp_path, context_controller=controller)
    events = [event async for event in runtime.chat("紧急路径")]
    assert provider.main_calls == 2 and provider.summary_calls == 1
    statuses = [event.text for event in events if event.type == RuntimeEventType.CONTEXT_STATUS]
    assert statuses[0] == "上下文撞墙，自动压缩中..."
    assert "上下文撞墙后已压缩" in statuses[1]
    assert events[-1].type == RuntimeEventType.COMPLETED


async def test_emergency_retry_failure_does_not_compact_twice(tmp_path: Path) -> None:
    provider = ContextProvider(main_failures=2)
    controller = ContextController.for_provider(tmp_path, provider)
    runtime = ConversationRuntime(provider, workspace=tmp_path, context_controller=controller)
    events = [event async for event in runtime.chat("只允许一次紧急重试")]
    assert provider.main_calls == 2 and provider.summary_calls == 1
    assert events[-1].type == RuntimeEventType.FAILED


async def test_force_compact_ignores_auto_threshold(tmp_path: Path) -> None:
    provider = ContextProvider(context_window=1_000_000)
    controller = ContextController.for_provider(tmp_path, provider)
    runtime = ConversationRuntime(provider, workspace=tmp_path, context_controller=controller)
    runtime._history = [ChatMessage("user", "很短的历史")]
    result = await runtime.force_compact()
    assert result.success and provider.summary_calls == 1 and provider.main_calls == 0
