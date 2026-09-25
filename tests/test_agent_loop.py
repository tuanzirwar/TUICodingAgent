from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

import tuicodingagent.runtime as runtime_module
from tuicodingagent.errors import NetworkError
from tuicodingagent.models import (
    AgentMode,
    ChatMessage,
    PermissionChoice,
    PermissionDecision,
    PermissionMode,
    ProviderConfig,
    ProviderEvent,
    ProviderEventType,
    RuntimeEventType,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from tuicodingagent.permissions import PermissionManager
from tuicodingagent.runtime import ConversationRuntime
from tuicodingagent.tools import ToolRegistry, create_default_registry


def text(value: str) -> ProviderEvent:
    return ProviderEvent(ProviderEventType.TEXT_DELTA, text=value)


def call(identifier: str, name: str, **arguments: object) -> ProviderEvent:
    return ProviderEvent(
        ProviderEventType.TOOL_CALL,
        tool_call=ToolCall(identifier, name, arguments),
    )


class Provider:
    config = ProviderConfig("fake", "openai", "model", "secret")

    def __init__(self, replies: list[list[ProviderEvent] | Exception]) -> None:
        self.replies = replies
        self.requests: list[tuple[str, tuple[ChatMessage, ...], tuple[str, ...]]] = []

    async def stream(
        self,
        system_prompt: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        self.requests.append((system_prompt, tuple(messages), tuple(item.name for item in tools)))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        for event in reply:
            yield event

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_agent_runs_multiple_tool_rounds_until_natural_completion(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    target = tmp_path / "summary.txt"
    source.write_text("要点", encoding="utf-8")
    provider = Provider(
        [
            [call("r", "read_file", path="source.txt")],
            [call("w", "write_file", path="summary.txt", content="摘要：要点")],
            [call("v", "read_file", path="summary.txt")],
            [text("已完成并验证。")],
        ]
    )
    runtime = ConversationRuntime(provider, tool_registry=create_default_registry(tmp_path))
    events = [event async for event in runtime.chat("读取后写摘要并验证")]

    assert target.read_text(encoding="utf-8") == "摘要：要点"
    assert sum(event.type == RuntimeEventType.ITERATION_STARTED for event in events) == 4
    assert sum(event.type == RuntimeEventType.TOOL_FINISHED for event in events) == 3
    assert events[-1].type == RuntimeEventType.COMPLETED
    assert events[-1].text == "已完成并验证。"
    assert [message.role for message in runtime.history] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
    ]


@pytest.mark.asyncio
async def test_iteration_and_unknown_tool_stops_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_module, "MAX_AGENT_ITERATIONS", 2)
    repeated = Provider(
        [
            [call("r1", "read_file", path="missing")],
            [call("r2", "read_file", path="missing")],
        ]
    )
    runtime = ConversationRuntime(repeated, tool_registry=create_default_registry(tmp_path))
    events = [event async for event in runtime.chat("不要停")]
    assert any(event.type == RuntimeEventType.LIMIT_REACHED for event in events)
    assert "最大迭代次数 2" in events[-1].text
    assert runtime.history[-1].role == "assistant"

    monkeypatch.setattr(runtime_module, "MAX_AGENT_ITERATIONS", 12)
    unknown = Provider([[call(f"u{index}", "imaginary_tool")] for index in range(3)])
    runtime = ConversationRuntime(unknown, tool_registry=create_default_registry(tmp_path))
    events = [event async for event in runtime.chat("调用幻觉工具")]
    assert sum(event.type == RuntimeEventType.TOOL_FINISHED for event in events) == 3
    assert "连续请求未知工具" in events[-1].text
    assert runtime.history[-1].role == "assistant"


@pytest.mark.asyncio
async def test_usage_events_accumulate_and_provider_error_can_recover(tmp_path: Path) -> None:
    usage = ProviderEvent(
        ProviderEventType.USAGE,
        input_tokens=10,
        output_tokens=4,
        cache_creation_input_tokens=6,
        cache_read_input_tokens=3,
    )
    provider = Provider([[usage, text("第一轮")], NetworkError("断线"), [text("已恢复")]])
    runtime = ConversationRuntime(provider, tool_registry=create_default_registry(tmp_path))
    first = [event async for event in runtime.chat("一")]
    usage_event = next(event for event in first if event.type == RuntimeEventType.USAGE)
    assert (usage_event.total_input_tokens, usage_event.total_output_tokens) == (10, 4)
    assert (
        usage_event.total_cache_creation_input_tokens,
        usage_event.total_cache_read_input_tokens,
    ) == (6, 3)
    failed = [event async for event in runtime.chat("二")]
    assert failed[-1].type == RuntimeEventType.FAILED
    recovered = [event async for event in runtime.chat("三")]
    assert recovered[-1].text == "已恢复"


class TimedTool:
    def __init__(self, name: str, read_only: bool, log: list[str]) -> None:
        self.definition = ToolDefinition(name, name, {"type": "object", "properties": {}})
        self.read_only = read_only
        self.log = log

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        self.log.append(f"start:{tool_call.name}")
        await asyncio.sleep(0.03)
        self.log.append(f"end:{tool_call.name}")
        return ToolResult(tool_call.id, tool_call.name, True, tool_call.name)


class BlockingTool:
    read_only = False

    def __init__(self, started: asyncio.Event) -> None:
        self.definition = ToolDefinition(
            "blocking_tool", "等待取消", {"type": "object", "properties": {}}
        )
        self.started = started

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        self.started.set()
        await asyncio.Event().wait()
        return ToolResult(tool_call.id, tool_call.name, True, "不应完成")


@pytest.mark.asyncio
async def test_read_batches_run_concurrently_and_side_effects_stay_ordered() -> None:
    log: list[str] = []
    registry = ToolRegistry(
        [
            TimedTool("read_a", True, log),
            TimedTool("read_b", True, log),
            TimedTool("write_a", False, log),
            TimedTool("read_c", True, log),
        ]
    )
    provider = Provider(
        [
            [
                call("1", "read_a"),
                call("2", "read_b"),
                call("3", "write_a"),
                call("4", "read_c"),
            ],
            [text("完成")],
        ]
    )
    runtime = ConversationRuntime(provider, tool_registry=registry)
    _ = [event async for event in runtime.chat("并发")]

    assert log.index("start:read_b") < log.index("end:read_a")
    assert log.index("start:write_a") > log.index("end:read_b")
    assert log.index("start:read_c") > log.index("end:write_a")
    assert [item.name for item in provider.requests[1][1][-1].tool_results] == [
        "read_a",
        "read_b",
        "write_a",
        "read_c",
    ]


@pytest.mark.asyncio
async def test_plan_mode_exposes_only_read_tools_then_execute_restores_all(
    tmp_path: Path,
) -> None:
    provider = Provider([[text("计划")], [text("执行")]])
    runtime = ConversationRuntime(provider, tool_registry=create_default_registry(tmp_path))
    runtime.set_mode(AgentMode.PLAN)
    _ = [event async for event in runtime.chat("制定计划")]
    runtime.set_mode(AgentMode.EXECUTE)
    _ = [event async for event in runtime.chat("执行计划")]

    assert provider.requests[0][2] == (
        "read_file",
        "glob",
        "grep",
        "read_artifact",
        "search_artifact",
    )
    assert "当前处于 Plan Mode" in provider.requests[0][1][-1].content
    assert provider.requests[1][2] == (
        "read_file",
        "write_file",
        "edit_file",
        "bash",
        "glob",
        "grep",
        "read_artifact",
        "search_artifact",
    )


@pytest.mark.asyncio
async def test_plan_reminder_repeats_full_on_fixed_cadence_without_polluting_history(
    tmp_path: Path,
) -> None:
    provider = Provider(
        [[call(f"r{iteration}", "read_file", path="missing")] for iteration in range(1, 6)]
        + [[text("计划完成")]]
    )
    runtime = ConversationRuntime(
        provider,
        tool_registry=create_default_registry(tmp_path),
        workspace=tmp_path,
    )
    runtime.set_mode(AgentMode.PLAN)
    _ = [event async for event in runtime.chat("制定多轮计划")]

    reminders = [request[1][0].content for request in provider.requests]
    assert "等待用户输入 /do" in reminders[0]
    assert all("Plan Mode 仍然生效" in reminders[index] for index in (1, 2, 3))
    assert "等待用户输入 /do" in reminders[4]
    assert "Plan Mode 仍然生效" in reminders[5]
    assert all(
        request[2]
        == ("read_file", "glob", "grep", "read_artifact", "search_artifact")
        for request in provider.requests
    )
    assert all("system-reminder" not in message.content for message in runtime.history)
    stable_blocks = [request[0].stable for request in provider.requests]
    assert len(set(stable_blocks)) == 1


@pytest.mark.asyncio
async def test_cooperative_cancel_emits_event_and_keeps_legal_history(tmp_path: Path) -> None:
    provider = Provider([[text("部分"), text("不应出现")]])
    runtime = ConversationRuntime(provider, tool_registry=create_default_registry(tmp_path))
    stream = runtime.chat("取消我")
    assert (await anext(stream)).type == RuntimeEventType.STARTED
    assert (await anext(stream)).type == RuntimeEventType.ITERATION_STARTED
    assert (await anext(stream)).text == "部分"
    runtime.request_cancel()
    cancelled = await anext(stream)
    assert cancelled.type == RuntimeEventType.CANCELLED
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert [message.role for message in runtime.history] == ["user", "assistant"]
    assert "用户取消" in runtime.history[-1].content


@pytest.mark.asyncio
async def test_task_cancel_during_tool_keeps_tool_call_and_result_paired() -> None:
    started = asyncio.Event()
    provider = Provider([[call("blocked", "blocking_tool")]])
    runtime = ConversationRuntime(provider, tool_registry=ToolRegistry([BlockingTool(started)]))

    async def consume() -> list[object]:
        return [event async for event in runtime.chat("开始后取消")]

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [message.role for message in runtime.history] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    cancelled_result = runtime.history[2].tool_results[0]
    assert not cancelled_result.success
    assert cancelled_result.error == "工具调用已取消"


@pytest.mark.asyncio
async def test_permission_ask_allow_once_and_permanent_rule(tmp_path: Path) -> None:
    local = tmp_path / ".tuicodingagent" / "settings.local.yaml"
    manager = PermissionManager(tmp_path, local_path=local)
    provider = Provider(
        [
            [call("write", "write_file", path="approved.txt", content="允许")],
            [text("完成")],
        ]
    )
    runtime = ConversationRuntime(
        provider,
        tool_registry=create_default_registry(tmp_path),
        workspace=tmp_path,
        permission_manager=manager,
    )
    stream = runtime.chat("写文件")
    while (event := await anext(stream)).type != RuntimeEventType.PERMISSION_REQUESTED:
        pass
    assert event.tool_call and event.tool_call.name == "write_file"
    runtime.resolve_permission(event.tool_call.id, PermissionChoice.ALLOW_ALWAYS)
    remaining = [item async for item in stream]

    assert (tmp_path / "approved.txt").read_text(encoding="utf-8") == "允许"
    assert remaining[-1].text == "完成"
    assert "Write(=approved.txt)" in local.read_text(encoding="utf-8")
    repeated = call("again", "write_file", path="approved.txt", content="再写").tool_call
    assert repeated is not None
    assert manager.evaluate(repeated).decision == PermissionDecision.ALLOW


@pytest.mark.asyncio
async def test_permission_denial_is_ordered_tool_result_and_loop_continues(
    tmp_path: Path,
) -> None:
    (tmp_path / "inside.txt").write_text("内容", encoding="utf-8")
    provider = Provider(
        [
            [
                call("outside", "write_file", path="../outside.txt", content="危险"),
                call("inside", "read_file", path="inside.txt"),
            ],
            [text("已根据拒绝结果调整")],
        ]
    )
    manager = PermissionManager(tmp_path, mode=PermissionMode.BYPASS)
    runtime = ConversationRuntime(
        provider,
        tool_registry=create_default_registry(tmp_path),
        workspace=tmp_path,
        permission_manager=manager,
    )
    events = [event async for event in runtime.chat("混合调用")]

    finished = [
        event.tool_result for event in events if event.type == RuntimeEventType.TOOL_FINISHED
    ]
    assert [result.tool_call_id for result in finished if result] == ["outside", "inside"]
    assert finished[0] and not finished[0].success
    assert finished[0].metadata["permission_denied"] is True
    assert finished[1] and finished[1].success
    assert events[-1].text == "已根据拒绝结果调整"
    returned = provider.requests[1][1][-1].tool_results
    assert [result.tool_call_id for result in returned] == ["outside", "inside"]


@pytest.mark.asyncio
async def test_blacklist_denial_in_bypass_mode_is_returned_to_model(tmp_path: Path) -> None:
    provider = Provider([[call("danger", "bash", command="rm -rf /")], [text("已改用安全方案")]])
    runtime = ConversationRuntime(
        provider,
        tool_registry=create_default_registry(tmp_path),
        workspace=tmp_path,
        permission_manager=PermissionManager(tmp_path, mode=PermissionMode.BYPASS),
    )
    events = [event async for event in runtime.chat("危险命令")]
    denied = next(
        event.tool_result for event in events if event.type == RuntimeEventType.TOOL_FINISHED
    )
    assert denied and not denied.success and "黑名单" in denied.error
    assert events[-1].text == "已改用安全方案"
