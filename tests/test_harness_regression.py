"""补齐生产 Harness 的场景边界，不将确定性驱动当模型能力。"""

from pathlib import Path

import pytest

from tuicodingagent.context import ContextController
from tuicodingagent.eval.harness_regression import (
    ScriptedProvider,
    context_case,
    evidence_case,
    governance_case,
    memory_case,
    process_case,
    recovery_case,
)
from tuicodingagent.models import ChatMessage, RuntimeEventType
from tuicodingagent.runtime import ConversationRuntime


@pytest.mark.parametrize("index", range(12))
async def test_context_matrix_preserves_request_and_fact(tmp_path: Path, index: int) -> None:
    assert (await context_case(tmp_path, index))["passed"]


@pytest.mark.parametrize("index", range(12))
async def test_memory_restart_injection_and_control(tmp_path: Path, index: int) -> None:
    assert (await memory_case(tmp_path, index))["passed"]


@pytest.mark.parametrize("index", range(10))
async def test_recovery_matrix(tmp_path: Path, index: int) -> None:
    assert (await recovery_case(tmp_path, index))["passed"]


@pytest.mark.parametrize("index", range(7))
async def test_governance_matrix(tmp_path: Path, index: int) -> None:
    assert (await governance_case(tmp_path, index))["passed"]


@pytest.mark.parametrize("index", range(2))
async def test_process_death_preserves_state_and_marks_uncertain_effect(
    tmp_path: Path, index: int
) -> None:
    assert (await process_case(tmp_path, index))["passed"]


@pytest.mark.parametrize("index", range(12))
async def test_evidence_lookup_preserves_source_and_line(tmp_path: Path, index: int) -> None:
    assert (await evidence_case(tmp_path, index))["passed"]


async def test_oversized_current_request_fails_without_sending_main_request(tmp_path: Path) -> None:
    provider = ScriptedProvider(window=40_000)
    controller = ContextController(tmp_path, 40_000)
    runtime = ConversationRuntime(provider, workspace=tmp_path, context_controller=controller)
    events = [event async for event in runtime.chat("保留原文" * 50_000)]
    assert events[-1].type == RuntimeEventType.FAILED
    assert all("九个编号部分" in prompt for prompt, _, _ in provider.requests)
    await runtime.close()


def test_recent_boundary_keeps_user_before_many_tool_pairs() -> None:
    from tuicodingagent.context.manager import _recent_messages
    from tuicodingagent.models import ToolCall, ToolResult

    messages = [ChatMessage("user", "当前请求不得丢弃")]
    for index in range(10):
        call = ToolCall(str(index), "read_file", {})
        messages.extend(
            (
                ChatMessage("assistant", tool_calls=(call,)),
                ChatMessage(
                    "tool", tool_results=(ToolResult(call.id, call.name, True, "x" * 10_000),)
                ),
            )
        )
    assert _recent_messages(tuple(messages))[0].content == "当前请求不得丢弃"
