"""Lead 消息交接与并发任务归属的冻结集成实验。"""
# ruff: noqa: ASYNC240

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from ..models import RuntimeEventType, ToolCall
from ..runtime import ConversationRuntime
from ..session import SessionContext, Writer, restore_session, scan_sessions
from ..team import Manager, TeammateInfo
from ..team.context import TeammateContext, with_teammate
from ..team.mailbox import Mailbox, Message
from ..team.tasks import TaskStore
from ..tools.registry import ToolRegistry
from ..tools.team import TaskUpdateTool
from .harness_regression import ScriptedProvider
from .harness_team import UnavailableProvider
from .harness_team_workflow import git


async def coordination_case(root: Path, index: int) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    manager = Manager(root / "teams")
    team = await manager.create("coordination")
    mailbox = Mailbox(team.config_dir / "mailbox")
    if index <= 3:
        text = "PINNED-LEAD: result=37" if index < 3 else "内容" * 6000 + "PINNED-LONG-END"
        await mailbox.write("lead", Message("alice", text))
        if index == 0:
            polled = await manager.poll_lead_mailboxes()
            pending = await Mailbox(mailbox.root).read("lead")
            return {
                "scenario": "poll_without_consumption",
                "polled": len(polled),
                "pending": len(pending),
                "passed": len(polled) == 1 and len(pending) == 1,
            }
        provider = UnavailableProvider() if index == 2 else ScriptedProvider(["处理完成"])
        runtime = ConversationRuntime(
            provider, workspace=root, session_writer=Writer(SessionContext.create(root), "fixture")
        )
        if hasattr(manager, "attach_lead_runtime"):
            manager.attach_lead_runtime(runtime)
        else:
            # 原 TUI 的生产顺序：poll 提前确认，再把截断文本放入临时 reminder。
            polled = await manager.poll_lead_mailboxes()
            body = "\n".join(f"[{m.team_name}/{m.sender}] {m.text}" for m in polled)[:8000]
            runtime.queue_reminder("<team-update>\n" + body + "\n</team-update>")
        try:
            events = [event async for event in runtime.chat("处理队员报告")]
        finally:
            await runtime.close()
        pending = await Mailbox(mailbox.root).read("lead")
        restored = restore_session(scan_sessions(root)[0])
        durable = any(text in message.content for message in restored.messages)
        final_ok = events[-1].type == RuntimeEventType.COMPLETED
        passed = len(pending) == 1 if index == 2 else durable and not pending and final_ok
        return {
            "scenario": (
                "",
                "successful_durable_handoff",
                "model_error_no_message_loss",
                "long_message_end_preserved",
            )[index],
            "pending": len(pending),
            "durable_full_input": durable,
            "completed": final_ok,
            "passed": passed,
        }
    await team.add_member(TeammateInfo("alice", "agent-a"))
    await team.add_member(TeammateInfo("bob", "agent-b"))
    store = TaskStore(team.config_dir / "tasks.json")
    registry = ToolRegistry((TaskUpdateTool(manager),))

    async def update(actor, identifier, **arguments):
        agent = "agent-a" if actor == "alice" else "agent-b"
        with with_teammate(TeammateContext(team.name, agent, actor)):
            return await registry.execute(ToolCall(identifier, "TaskUpdate", arguments))

    if index == 4:
        task = await store.create("exclusive claim")
        results = await asyncio.gather(
            *(
                update(actor, actor, task_id=task.id, status="in_progress", assignee=actor)
                for actor in ("alice", "bob")
            )
        )
        accepted = sum(r.success for r in results)
        return {
            "scenario": "two_workers_one_task",
            "accepted": accepted,
            "owner": store.get(task.id).assignee,
            "passed": accepted == 1,
        }
    task = await store.create("owned task", assignee="alice")
    if index == 5:
        revision = getattr(task, "revision", 0)
        await update("alice", "fresh", task_id=task.id, title="new fact")
        result = await update(
            "alice", "stale", task_id=task.id, title="stale fact", expected_revision=revision
        )
        current = store.get(task.id)
        return {
            "scenario": "stale_snapshot_update",
            "stale_rejected": not result.success,
            "title": current.title,
            "passed": not result.success and current.title == "new fact",
        }
    if index == 6:
        result = await update("bob", "foreign", task_id=task.id, status="completed")
        return {
            "scenario": "foreign_worker_completion",
            "foreign_rejected": not result.success,
            "status": store.get(task.id).status,
            "passed": not result.success and store.get(task.id).status == "pending",
        }
    repo = root / "repository"
    repo.mkdir()
    await git(repo, "init", "-q")
    await git(repo, "config", "user.name", "Fixture")
    await git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "shared.py").write_text("VALUE = 0\n", encoding="utf-8")
    await git(repo, "add", "shared.py")
    await git(repo, "commit", "-qm", "base")
    base = await git(repo, "rev-parse", "HEAD")
    await git(repo, "checkout", "-qb", "producer")
    (repo / "shared.py").write_text("VALUE = 1\n", encoding="utf-8")
    await git(repo, "commit", "-qam", "producer")
    await git(repo, "checkout", "-qb", "consumer", base)
    (repo / "shared.py").write_text("VALUE = 2\n", encoding="utf-8")
    await git(repo, "commit", "-qam", "consumer")
    process = await asyncio.create_subprocess_exec(
        "git",
        "merge",
        "producer",
        cwd=repo,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await process.communicate()
    await team.remove_member("agent-a")
    await team.add_member(TeammateInfo("alice", "agent-a", worktree_path=str(repo)))
    result = await update("alice", "conflict", task_id=task.id, status="completed")
    return {
        "scenario": "merge_conflict_cannot_complete",
        "merge_exit_code": process.returncode,
        "completion_rejected": not result.success,
        "passed": process.returncode != 0
        and not result.success
        and result.metadata.get("verification_failed") is True
        and store.get(task.id).status != "completed",
    }


async def run(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    if (output / "result.json").exists():
        raise ValueError("使用新的目录以保留原始证据")
    cases = []
    for index in range(8):
        try:
            cases.append(await coordination_case(output / "workspace" / str(index), index))
        except Exception as exc:
            cases.append({"case": index, "passed": False, "error": f"{type(exc).__name__}: {exc}"})
    result = {
        "real_model_requests": 0,
        "passed": sum(c["passed"] for c in cases),
        "total": 8,
        "cases": cases,
    }
    (output / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"passed": result["passed"], "total": 8}))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args().output.resolve()))


if __name__ == "__main__":
    main()
