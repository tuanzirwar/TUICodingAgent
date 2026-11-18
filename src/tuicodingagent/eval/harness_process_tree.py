"""真实 shell/验证父子进程取消后的延迟写入实验。"""
# ruff: noqa: ASYNC240, ASYNC110

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from ..models import PermissionMode, RuntimeEventType, ToolCall
from ..permissions import PermissionManager
from ..runtime import ConversationRuntime
from ..team.delivery import MailboxDelivery
from ..team.mailbox import Mailbox, Message
from ..tools.registry import create_default_registry
from ..tools.shell import BashTool
from ..verification import VerificationRunner
from .harness_regression import ScriptedProvider


async def process_case(root: Path, index: int) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    child = root / "descendant.py"
    parent = root / "parent.py"
    child.write_text(
        "from pathlib import Path\nimport os, time\n"
        "Path('descendant.ready').write_text(str(os.getpid()))\n"
        "time.sleep(0.8)\nPath('late-effect.txt').write_text('after cancellation')\n"
        "time.sleep(0.2)\n",
        encoding="utf-8",
    )
    parent.write_text(
        "from pathlib import Path\nimport os, subprocess, sys\n"
        "Path('parent.ready').write_text(str(os.getpid()))\n"
        "process = subprocess.Popen([sys.executable, 'descendant.py'])\nprocess.wait()\n",
        encoding="utf-8",
    )
    command = (
        subprocess.list2cmdline([sys.executable, str(parent)])
        if os.name == "nt"
        else shlex.join([sys.executable, str(parent)])
    )
    if index == 6:
        return await lost_lease_case(root, command)
    if index in {7, 8}:
        return await runtime_cancel_case(root, command, repeated=index == 8)
    if index == 9:
        return await release_cancel_case(root)
    shell = index in {0, 1, 5}
    cancel = index in {0, 2}
    total = index == 4
    timeout = 0.4 if index in {1, 3} else 10
    if shell:
        task = asyncio.create_task(
            BashTool(root, timeout=timeout).execute(ToolCall("shell", "bash", {"command": command}))
        )
    else:
        runner = VerificationRunner(root, step_timeout=timeout)

        async def verify():
            if total:
                async with asyncio.timeout(0.4):
                    return await runner._command("fixture", [sys.executable, str(parent)])
            return await runner._command("fixture", [sys.executable, str(parent)])

        task = asyncio.create_task(verify())
    try:
        async with asyncio.timeout(5):
            while not (root / "descendant.ready").exists():
                if task.done():
                    raise RuntimeError("进程在子进程 ready 之前结束")
                await asyncio.sleep(0.01)
        if cancel:
            task.cancel()
        outcomes = await asyncio.gather(task, return_exceptions=True)
        # 保留足够的观察窗，直接验证后代进程是否越过取消继续产生副作用。
        await asyncio.sleep(1)
        wrote = (root / "late-effect.txt").exists()
        outcome = outcomes[0]
        reported = (
            isinstance(outcome, asyncio.CancelledError)
            if cancel
            else isinstance(outcome, TimeoutError)
            if total
            else (
                outcome.success
                if index == 5
                else (outcome.metadata.get("timed_out") if shell else outcome[1] == 124)
            )
        )
        return {
            "scenario": (
                "shell_cancel_descendant",
                "shell_timeout_descendant",
                "verification_cancel_descendant",
                "verification_step_timeout_descendant",
                "verification_outer_timeout_descendant",
                "healthy_shell_descendant",
            )[index],
            "parent_pid": int((root / "parent.ready").read_text()),
            "descendant_pid": int((root / "descendant.ready").read_text()),
            "late_write_observed": wrote,
            "caller_outcome_expected": bool(reported),
            "passed": bool(reported) and (wrote if index == 5 else not wrote),
        }
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        # 夹具后代在 1 秒后自然退出，失败基线也不留下常驻进程。


async def lost_lease_case(root: Path, command: str) -> dict:
    mailbox = Mailbox(root / "mailbox")
    identifier = await mailbox.write("alice", Message("lead", "续租失败时停止运行中的命令"))
    runtime = ConversationRuntime(
        ScriptedProvider([ToolCall("shell", "bash", {"command": command}), "完成"]),
        workspace=root,
        tool_registry=create_default_registry(root),
        permission_manager=PermissionManager(root, mode=PermissionMode.BYPASS),
    )
    delivery = MailboxDelivery(runtime, mailbox, "alice", ack_timeout=0.3)
    delivery.bind()
    settle = mailbox.settle_claims
    failed = asyncio.Event()

    async def unavailable(agent_id, claims, action, **kwargs):
        if action == "renew" and (root / "descendant.ready").exists():
            failed.set()
            raise OSError("夹具：工具运行中数据库续租不可用")
        return await settle(agent_id, claims, action, **kwargs)

    async def consume():
        return [event async for event in runtime.chat("执行队员命令")]

    with patch.object(mailbox, "settle_claims", unavailable):
        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(failed.wait(), 5)
            await asyncio.sleep(0.4)
            successor = await Mailbox(mailbox.root).claim("alice", ack_timeout=10)
            events = await asyncio.wait_for(task, 5)
            await asyncio.sleep(1)
            wrote = (root / "late-effect.txt").exists()
            with mailbox._connect() as connection:
                row = connection.execute(
                    "SELECT status,ack_deadline FROM messages WHERE id=?", (identifier,)
                ).fetchone()
            claimed = [message.message_id for message in successor] == [identifier]
            successor_unchanged = (
                claimed
                and row["status"] == "inflight"
                and row["ack_deadline"] == successor[0].ack_deadline
            )
            return {
                "scenario": "inflight_command_lease_loss",
                "renewal_failure_injected": True,
                "late_write_observed": wrote,
                "successor_claimed_same_id": claimed,
                "successor_lease_unchanged": successor_unchanged,
                "final_event": events[-1].type.value,
                "passed": not wrote
                and successor_unchanged
                and events[-1].type == RuntimeEventType.FAILED,
            }
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await runtime.close()


async def runtime_cancel_case(root: Path, command: str, *, repeated: bool) -> dict:
    runtime = ConversationRuntime(
        ScriptedProvider([ToolCall("shell", "bash", {"command": command}), "完成"]),
        workspace=root,
        tool_registry=create_default_registry(root),
        permission_manager=PermissionManager(root, mode=PermissionMode.BYPASS),
    )
    events = []

    async def consume():
        async for event in runtime.chat("取消正在执行的命令"):
            events.append(event)

    task = asyncio.create_task(consume())
    try:
        async with asyncio.timeout(5):
            while not (root / "descendant.ready").exists():
                await asyncio.sleep(0.01)
        if repeated:
            task.cancel()
            await asyncio.sleep(0.005)
            task.cancel()
        else:
            runtime.request_cancel()
        outcomes = await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(1)
        wrote = (root / "late-effect.txt").exists()
        reported = (
            isinstance(outcomes[0], asyncio.CancelledError)
            if repeated
            else events[-1].type == RuntimeEventType.CANCELLED
        )
        return {
            "scenario": "runtime_repeated_cancel" if repeated else "runtime_request_cancel",
            "late_write_observed": wrote,
            "caller_outcome_expected": reported,
            "runtime_released": not runtime._busy,
            "passed": not wrote and reported and not runtime._busy,
        }
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


async def release_cancel_case(root: Path) -> dict:
    entered, releasing, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class WaitingProvider(ScriptedProvider):
        async def stream(self, *args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
            yield  # pragma: no cover

    mailbox = Mailbox(root / "mailbox")
    await mailbox.write("alice", Message("lead", "取消后归还消息"))
    runtime = ConversationRuntime(WaitingProvider(), workspace=root)
    delivery = MailboxDelivery(runtime, mailbox, "alice", ack_timeout=0.3)
    delivery.bind()

    async def slow_release():
        releasing.set()
        await finish.wait()
        await delivery.release()

    runtime.set_delivery_callbacks(
        delivery.commit, slow_release, validate=delivery.validate, watch=delivery.wait_lost
    )

    async def consume():
        return [event async for event in runtime.chat("取消")]

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.wait_for(releasing.wait(), 5)
        task.cancel()
        finish.set()
        outcome = (await asyncio.gather(task, return_exceptions=True))[0]
        pending = len(await mailbox.read("alice"))
        released = not runtime._busy and not runtime._session_lock.locked()
        heartbeat_stopped = delivery._heartbeat is None
        return {
            "scenario": "repeated_cancel_during_mail_release",
            "runtime_released": released,
            "heartbeat_stopped": heartbeat_stopped,
            "pending": pending,
            "caller_cancelled": isinstance(outcome, asyncio.CancelledError),
            "passed": released
            and heartbeat_stopped
            and pending == 1
            and isinstance(outcome, asyncio.CancelledError),
        }
    finally:
        finish.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await delivery.release()
        await runtime.close()


async def run(output: Path, selected: int | None = None) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    if (output / "result.json").exists():
        raise ValueError("使用新的实验目录")
    cases = [
        await process_case(output / "workspace" / str(index), index)
        for index in (range(10) if selected is None else [selected])
    ]
    result = {
        "real_model_requests": 0,
        "passed": sum(case["passed"] for case in cases),
        "total": len(cases),
        "cases": cases,
    }
    (output / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"passed": result["passed"], "total": result["total"]}))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", type=int, choices=range(10))
    args = parser.parse_args()
    asyncio.run(run(args.output.resolve(), args.case))


if __name__ == "__main__":
    main()
