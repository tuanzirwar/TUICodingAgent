from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from tuicodingagent.models import (
    ChatMessage,
    ProviderConfig,
    ProviderEvent,
    ProviderEventType,
    RuntimeEventType,
    ToolCall,
    ToolDefinition,
)
from tuicodingagent.runtime import ConversationRuntime
from tuicodingagent.tools import create_default_registry
from tuicodingagent.trace import load_events


class ScriptedProvider:
    def __init__(self, protocol: str, replies: list[list[ProviderEvent]]) -> None:
        self.config = ProviderConfig("fake", protocol, "model", "secret")  # type: ignore[arg-type]
        self.replies = replies
        self.requests: list[tuple[ChatMessage, ...]] = []
        self.tool_counts: list[int] = []

    async def stream(
        self,
        system_prompt: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        self.requests.append(tuple(messages))
        self.tool_counts.append(len(tools))
        for event in self.replies.pop(0):
            yield event

    async def close(self) -> None:
        return None


def text(value: str) -> ProviderEvent:
    return ProviderEvent(ProviderEventType.TEXT_DELTA, text=value)


def tool(identifier: str, name: str, **arguments: object) -> ProviderEvent:
    return ProviderEvent(
        ProviderEventType.TOOL_CALL,
        tool_call=ToolCall(identifier, name, arguments),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
async def test_two_stage_tool_execution_and_feedback_is_protocol_independent(
    tmp_path: Path, protocol: str
) -> None:
    (tmp_path / "README.md").write_text("# 标题\n正文", encoding="utf-8")
    provider = ScriptedProvider(
        protocol,
        [[text("我先读取。"), tool("read-1", "read_file", path="README.md")], [text("总结完成")]],
    )
    runtime = ConversationRuntime(provider, tool_registry=create_default_registry(tmp_path))

    events = [event async for event in runtime.chat("读取 README.md 后总结")]
    assert [event.type for event in events] == [
        RuntimeEventType.STARTED,
        RuntimeEventType.ITERATION_STARTED,
        RuntimeEventType.TEXT_DELTA,
        RuntimeEventType.TOOL_STARTED,
        RuntimeEventType.TOOL_FINISHED,
        RuntimeEventType.ITERATION_STARTED,
        RuntimeEventType.TEXT_DELTA,
        RuntimeEventType.COMPLETED,
    ]
    assert events[4].tool_result and events[4].tool_result.success
    assert "标题" in events[4].tool_result.output
    assert events[-1].text == "总结完成"
    assert provider.tool_counts == [8, 8]
    tool_traces = [
        row
        for row in load_events(tmp_path / ".tuicodingagent" / "traces" / "events.jsonl")
        if row["event_type"] == "tool_call"
    ]
    assert len(tool_traces) == 1
    assert tool_traces[0]["session_id"] == runtime.trace_session_id
    assert tool_traces[0]["agent_id"] == runtime.agent_id
    assert tool_traces[0]["model"] == "model"
    assert provider.requests[1][-2].tool_calls[0].id == "read-1"
    assert provider.requests[1][-1].tool_results[0].success
    assert len(runtime.history) == 4


@pytest.mark.asyncio
async def test_tool_failure_is_fed_back_and_conversation_can_continue(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        "openai",
        [
            [tool("missing", "read_file", path="missing.txt")],
            [text("文件不存在，请确认路径。")],
            [text("仍可继续")],
        ],
    )
    runtime = ConversationRuntime(provider, tool_registry=create_default_registry(tmp_path))
    first = [event async for event in runtime.chat("读取缺失文件")]
    result = next(
        event.tool_result for event in first if event.type == RuntimeEventType.TOOL_FINISHED
    )
    assert result and not result.success and "读取文件失败" in result.error
    assert provider.requests[1][-1].tool_results[0].success is False
    second = [event async for event in runtime.chat("继续")]
    assert second[-1].type == RuntimeEventType.COMPLETED
    assert second[-1].text == "仍可继续"


@pytest.mark.asyncio
async def test_later_tool_call_is_executed_by_agent_loop(tmp_path: Path) -> None:
    target = tmp_path / "created.txt"
    provider = ScriptedProvider(
        "openai",
        [
            [tool("write-1", "write_file", path=str(target), content="one")],
            [text("还想再写。"), tool("write-2", "write_file", path=str(target), content="two")],
            [text("写入完成")],
        ],
    )
    runtime = ConversationRuntime(provider, tool_registry=create_default_registry(tmp_path))
    events = [event async for event in runtime.chat("写文件")]
    assert target.read_text(encoding="utf-8") == "two"
    assert sum(event.type == RuntimeEventType.TOOL_STARTED for event in events) == 2
    assert not any(event.type == RuntimeEventType.LIMIT_REACHED for event in events)
    assert events[-1].text == "写入完成"
