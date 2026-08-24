from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import tuicodingagent.context.manager as context_module
from tuicodingagent.context import BOUNDARY_NOTICE, ContextController
from tuicodingagent.errors import ProviderError
from tuicodingagent.models import (
    ChatMessage,
    ProviderConfig,
    ProviderEvent,
    ProviderEventType,
    SystemPrompt,
    ToolCall,
    ToolDefinition,
    ToolResult,
)

SUMMARY = """<analysis>草稿应丢弃</analysis><summary>
1. 主要请求和意图\n2. 关键技术概念\n3. 文件和代码段\n4. 错误和修复\n5. 问题解决过程
6. 所有用户消息原文\n7. 待办任务\n8. 当前工作\n9. 可能的下一步
</summary>"""


class SummaryProvider:
    def __init__(self, failures: int = 0) -> None:
        self.config = ProviderConfig("fake", "openai", "model", "secret", context_window=100_000)
        self.failures = failures
        self.requests: list[tuple[object, Sequence[ChatMessage], Sequence[ToolDefinition]]] = []

    async def stream(self, system_prompt, messages, tools=()) -> AsyncIterator[ProviderEvent]:
        self.requests.append((system_prompt, messages, tools))
        if self.failures:
            self.failures -= 1
            raise ProviderError("prompt_too_long")
        yield ProviderEvent(ProviderEventType.TEXT_DELTA, text=SUMMARY)

    async def close(self) -> None:
        return None


async def test_compact_has_no_tools_discards_draft_and_restores_state(tmp_path: Path) -> None:
    controller = ContextController(tmp_path, 100_000)
    file_path = tmp_path / "tracked.py"
    file_path.write_text("print('tracked')\n", encoding="utf-8")
    await controller.record_read(
        ToolCall("read", "read_file", {"path": "tracked.py"}),
        ToolResult("read", "read_file", True, output="带行号结果"),
    )
    messages = tuple(
        ChatMessage("user" if index % 2 == 0 else "assistant", f"消息 {index}")
        for index in range(8)
    )
    definitions = (
        ToolDefinition("read_file", "读取", {"type": "object"}),
        ToolDefinition("grep", "搜索", {"type": "object"}),
    )
    provider = SummaryProvider()
    result = await controller.compact(provider, messages, definitions, "manual")
    assert result.success
    assert provider.requests[0][2] == ()
    assert isinstance(provider.requests[0][0], SystemPrompt)
    assert "严禁调用任何工具" in str(provider.requests[0][0])
    assert "草稿应丢弃" not in result.messages[0].content
    assert "1. 主要请求和意图" in result.messages[0].content
    recovery = result.messages[1].content
    assert "tracked.py" in recovery and "print('tracked')" in recovery
    assert "read_file" in recovery and "grep" in recovery
    assert BOUNDARY_NOTICE in recovery


async def test_summary_ptl_drops_oldest_groups_until_success(tmp_path: Path) -> None:
    controller = ContextController(tmp_path, 100_000)
    provider = SummaryProvider(failures=2)
    messages = tuple(
        item
        for index in range(5)
        for item in (ChatMessage("user", f"用户 {index}"), ChatMessage("assistant", "回复"))
    )
    result = await controller.compact(provider, messages, (), "manual")
    assert result.success and len(provider.requests) == 3
    assert (
        len(provider.requests[0][1]) > len(provider.requests[1][1]) > len(provider.requests[2][1])
    )


async def test_auto_breaker_trips_at_three_and_manual_bypasses(tmp_path: Path) -> None:
    controller = ContextController(tmp_path, 100_000)
    messages = (ChatMessage("user", "保留"),)
    for _ in range(3):
        result = await controller.compact(SummaryProvider(failures=99), messages, (), "auto")
        assert not result.success
    assert not await controller.auto_enabled()
    manual = await controller.compact(SummaryProvider(), messages, (), "manual")
    assert manual.success
    assert not await controller.auto_enabled()


async def test_recent_file_recovery_keeps_latest_five_and_truncates(tmp_path: Path) -> None:
    controller = ContextController(tmp_path, 100_000)
    for index in range(7):
        path = tmp_path / f"file-{index}.txt"
        path.write_text(str(index) * 20_000, encoding="utf-8")
        await controller.record_read(
            ToolCall(str(index), "read_file", {"path": str(path)}),
            ToolResult(str(index), "read_file", True, output="ok"),
        )
    recovery = await controller._recovery_message(())
    assert "file-6.txt" in recovery.content and "file-2.txt" in recovery.content
    assert "file-1.txt" not in recovery.content and "file-0.txt" not in recovery.content
    assert recovery.content.index("file-6.txt") < recovery.content.index("file-5.txt")
    assert "(content truncated)" in recovery.content


def test_recent_boundary_satisfies_both_minimums_and_keeps_tool_pair() -> None:
    messages = (
        ChatMessage("user", "旧请求"),
        ChatMessage("assistant", "a" * 8_000),
        ChatMessage("user", "新请求"),
        ChatMessage("assistant", "b" * 8_000),
        ChatMessage("assistant", tool_calls=(ToolCall("x", "read_file", {"path": "x"}),)),
        ChatMessage(
            "tool", tool_results=(ToolResult("x", "read_file", True, output="c" * 35_000),)
        ),
        ChatMessage("assistant", "完成"),
    )
    recent = context_module._recent_messages(messages)
    assert len(recent) >= 5
    assert recent[0].role != "tool"
    tool_index = next(index for index, item in enumerate(recent) if item.role == "tool")
    assert recent[tool_index - 1].tool_calls[0].id == "x"
