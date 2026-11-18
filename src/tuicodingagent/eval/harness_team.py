"""冻结的 Team 消息与依赖合同，真实持久状态、零模型请求。"""
# ruff: noqa: ASYNC240

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from ..errors import ProviderError
from ..models import ProviderEvent, ProviderEventType, ToolCall
from ..runtime import ConversationRuntime
from ..session import SessionContext, Writer, restore_session, scan_sessions
from ..team import Manager, TeammateInfo
from ..team.mailbox import Mailbox, Message
from ..team.service import TeamService
from ..team.tasks import TaskStore
from ..tools.registry import create_default_registry
from .harness_regression import ScriptedProvider


class UnavailableProvider(ScriptedProvider):
    async def stream(self, *args, **kwargs):
        raise ProviderError("人工模拟消息送达后模型不可用")
        yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="")  # pragma: no cover


async def team_case(root: Path, index: int) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    if index <= 2:
        manager = Manager(root / "teams")
        team = await manager.create("delivery")
        member = TeammateInfo("alice", "agent-a")
        await team.add_member(member)
        mailbox = Mailbox(team.config_dir / "mailbox")
        message_id = await mailbox.write(
            member.agent_id, Message("lead", "PINNED-MAIL: service-timeout=37")
        )
        provider = (
            UnavailableProvider()
            if index == 0
            else ScriptedProvider(
                [ToolCall("read", "read_file", {"path": "input.txt"})] if index == 2 else ["已处理"]
            )
        )
        (root / "input.txt").write_text("input", encoding="utf-8")
        context = SessionContext.create(root)
        runtime = ConversationRuntime(
            provider,
            workspace=root,
            tool_registry=create_default_registry(root),
            session_writer=Writer(context, "fixture"),
            max_iterations=1,
        )
        service = object.__new__(TeamService)
        service._attach_mailbox(runtime, team, member)
        try:
            _ = [event async for event in runtime.chat("处理团队消息")]
        finally:
            await runtime.close()
        pending = await Mailbox(mailbox.root).read(member.agent_id)
        restored = restore_session(scan_sessions(root)[0])
        persisted = any("PINNED-MAIL:" in m.content for m in restored.messages)
        if index == 1:
            passed = not pending and persisted
        else:
            passed = [m.message_id for m in pending] == [message_id]
        return {
            "scenario": (
                "model_error_keeps_message",
                "success_ack_and_durable_input",
                "iteration_limit_keeps_message",
            )[index],
            "pending_messages": len(pending),
            "message_persisted": persisted,
            "passed": passed,
        }
    store = TaskStore(root / "tasks.json")
    upstream = await store.create("生成接口", assignee="alice")
    downstream = await store.create("使用接口", assignee="bob", blocked_by=[upstream.id])
    rejected = False
    if index == 3:
        try:
            await store.update(downstream.id, status="in_progress")
        except ValueError:
            rejected = True
        passed = rejected and store.get(downstream.id).status == "pending"
    elif index == 4:
        try:
            await store.update(upstream.id, add_blocked_by=[downstream.id])
        except ValueError:
            rejected = True
        passed = rejected and not store.get(upstream.id).blocked_by
    elif index == 5:
        try:
            await store.update(downstream.id, status="completed")
        except ValueError:
            rejected = True
        passed = rejected and store.get(downstream.id).status == "pending"
    else:
        before = next(task for task in store.list() if task["id"] == downstream.id)["is_ready"]
        await store.update(upstream.id, status="in_progress")
        await store.update(upstream.id, status="completed")
        await store.update(downstream.id, status="in_progress")
        await store.update(downstream.id, status="completed")
        reloaded = TaskStore(store.path)
        passed = not before and all(task["status"] == "completed" for task in reloaded.list())
    return {
        "scenario": (
            "",
            "",
            "",
            "blocked_task_cannot_start",
            "dependency_cycle_rejected",
            "blocked_task_cannot_complete",
            "ready_dependency_pipeline",
        )[index],
        "rejected": rejected,
        "tasks": store.list(),
        "passed": passed,
    }


async def run(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    if (output / "result.json").exists():
        raise ValueError("请使用新目录以保留失败证据")
    cases = []
    for index in range(7):
        try:
            cases.append(await team_case(output / "workspace" / str(index), index))
        except Exception as exc:
            cases.append({"case": index, "passed": False, "error": f"{type(exc).__name__}: {exc}"})
    result = {
        "real_model_requests": 0,
        "total": 7,
        "passed": sum(c["passed"] for c in cases),
        "suite_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "cases": cases,
    }
    (output / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"total": 7, "passed": result["passed"]}))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args().output.resolve()))


if __name__ == "__main__":
    main()
