"""真实 Worktree 依赖编码、进程死亡、预算与并发归因。"""

import asyncio
from pathlib import Path

from tuicodingagent.eval.harness_regression import ScriptedProvider
from tuicodingagent.eval.harness_team_crash import run as crash_run
from tuicodingagent.eval.harness_team_workflow import run as workflow_run
from tuicodingagent.models import ToolCall
from tuicodingagent.runtime import ConversationRuntime
from tuicodingagent.subagent import Manager as SubAgentManager
from tuicodingagent.tools.registry import create_default_registry
from tuicodingagent.trace import load_events


async def test_real_worktree_dependency_and_failed_verification_recovery(tmp_path: Path) -> None:
    result = await workflow_run(tmp_path)
    assert result["passed"]
    assert result["premature_completion_rejected"]
    assert result["trace_attribution_correct"]
    assert result["error_then_adjusted"]
    assert result["worktrees_cleaned"]


async def test_real_process_death_preserves_claim_until_resume(tmp_path: Path) -> None:
    assert (await crash_run(tmp_path))["passed"]


async def test_background_iteration_limit_is_failure(tmp_path: Path) -> None:
    registry = create_default_registry(tmp_path)
    runtime = ConversationRuntime(
        ScriptedProvider([ToolCall("read", "read_file", {"path": "missing.txt"})]),
        workspace=tmp_path,
        tool_registry=registry,
        max_iterations=1,
    )
    manager = SubAgentManager()
    task = manager.launch(runtime, "budget test")
    await task.handle
    assert task.status == "failed"
    assert task.err is not None
    await runtime.close()


async def test_shared_registry_trace_context_is_task_local(tmp_path: Path) -> None:
    registry = create_default_registry(tmp_path)
    rendezvous = asyncio.Event()
    count = 0

    async def execute(agent: str):
        nonlocal count
        registry.set_trace_context("session-" + agent, agent, "fixture")
        count += 1
        if count == 2:
            rendezvous.set()
        await rendezvous.wait()
        return await registry.execute(ToolCall(agent, "read_file", {"path": "missing.txt"}))

    await asyncio.gather(execute("alice"), execute("bob"))
    rows = load_events(registry.trace_recorder.path)
    assert {row["request_id"]: row["agent_id"] for row in rows} == {"alice": "alice", "bob": "bob"}
