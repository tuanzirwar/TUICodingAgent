"""租约转移后的工具副作用，以及验证子进程的取消清理。"""
# ruff: noqa: ASYNC240, ASYNC110

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

from ..models import (
    PermissionChoice,
    PermissionMode,
    ProviderEvent,
    ProviderEventType,
    RuntimeEventType,
    ToolCall,
)
from ..permissions import PermissionManager
from ..runtime import ConversationRuntime
from ..team.delivery import MailboxDelivery
from ..team.mailbox import Mailbox, Message
from ..tools.registry import create_default_registry
from ..verification import VerificationRunner
from .harness_regression import ScriptedProvider


async def lease_case(root: Path, index: int) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    started, finish = asyncio.Event(), asyncio.Event()
    call = ToolCall("write", "write_file", {"path": "effect.txt", "content": "old consumer"})

    class LateProvider(ScriptedProvider):
        async def stream(self, *args, **kwargs):
            if not started.is_set():
                started.set()
                if index != 1:
                    await finish.wait()
                yield ProviderEvent(ProviderEventType.TOOL_CALL, tool_call=call)
            else:
                yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="完成")

    mailbox = Mailbox(root / "mailbox")
    identifier = await mailbox.write("alice", Message("lead", "只由当前租约持有者执行"))
    runtime = ConversationRuntime(
        LateProvider(),
        workspace=root,
        tool_registry=create_default_registry(root),
        permission_manager=PermissionManager(
            root, mode=PermissionMode.DEFAULT if index == 1 else PermissionMode.BYPASS
        ),
    )
    delivery = MailboxDelivery(runtime, mailbox, "alice", ack_timeout=0.3)
    delivery.bind()
    events = []
    approved = asyncio.Event()

    async def consume():
        async for event in runtime.chat("执行队员消息"):
            events.append(event)
            if event.type == RuntimeEventType.PERMISSION_REQUESTED:
                approved.set()

    task = asyncio.create_task(consume())
    successor = []
    try:
        await asyncio.wait_for(started.wait(), 5)
        if index == 1:
            await asyncio.wait_for(approved.wait(), 5)
        if index != 2:
            await delivery._stop_heartbeat()
            await asyncio.sleep(0.4)
            successor = await Mailbox(mailbox.root).claim("alice", ack_timeout=10)
            if [message.message_id for message in successor] != [identifier]:
                raise RuntimeError("实验没有完成真实租约转移")
        finish.set()
        if index == 1:
            runtime.resolve_permission(call.id, PermissionChoice.ALLOW_ONCE)
        await asyncio.wait_for(task, 10)
        wrote = (root / "effect.txt").exists()
        with mailbox._connect() as connection:
            row = connection.execute(
                "SELECT status,ack_deadline FROM messages WHERE id=?", (identifier,)
            ).fetchone()
        owns_new = not successor or (
            row["status"] == "inflight" and row["ack_deadline"] == successor[0].ack_deadline
        )
        final = events[-1].type
        return {
            "scenario": (
                "late_model_after_takeover",
                "approval_wait_takeover",
                "healthy_owner_can_write",
            )[index],
            "successor_claimed_same_id": bool(successor),
            "file_written": wrote,
            "final_event": final.value,
            "successor_lease_unchanged": owns_new,
            "runtime_released": not runtime._busy,
            "heartbeat_stopped": delivery._heartbeat is None,
            "passed": (wrote and final == RuntimeEventType.COMPLETED and row["status"] == "acked")
            if index == 2
            else not wrote and final == RuntimeEventType.FAILED and owns_new and not runtime._busy,
        }
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()
        if successor:
            await mailbox.settle_claims("alice", successor, "release")


async def verification_case(root: Path, *, total_timeout: bool) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    runner = VerificationRunner(root, step_timeout=60, total_timeout=0.7)
    real_spawn = asyncio.create_subprocess_exec
    processes = []
    ready = root / "child.ready"
    code = (
        "from pathlib import Path; import time; Path("
        + repr(str(ready))
        + ").write_text('ready'); time.sleep(60)"
    )

    async def spawn(*args, **kwargs):
        if args and str(args[0]).lower().endswith("taskkill.exe"):
            return await real_spawn(*args, **kwargs)
        process = await real_spawn(sys.executable, "-c", code, **kwargs)
        processes.append(process)
        return process

    if total_timeout:
        (root / ".git").mkdir()
    with patch("tuicodingagent.verification.asyncio.create_subprocess_exec", spawn):
        task = asyncio.create_task(
            runner.run({root / "artifact.txt"})
            if total_timeout
            else runner._command("fixture", ["unused"])
        )
        try:
            async with asyncio.timeout(5):
                while not ready.exists():
                    await asyncio.sleep(0.01)
            if not total_timeout:
                task.cancel()
            outcomes = await asyncio.gather(task, return_exceptions=True)
            alive = processes[0].returncode is None
            output = outcomes[0]
            return {
                "scenario": "verification_total_timeout_cleanup"
                if total_timeout
                else "verification_cancel_cleanup",
                "child_started": ready.exists(),
                "alive_after_return": alive,
                "observed_exit_code": processes[0].returncode,
                "caller_cancelled": isinstance(output, asyncio.CancelledError),
                "caller_timeout_reported": total_timeout
                and not output.success
                and "总超时" in output.output,
                "passed": not alive
                and (
                    not output.success
                    if total_timeout
                    else isinstance(output, asyncio.CancelledError)
                ),
            }
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            for process in processes:
                if process.returncode is None:
                    process.kill()
                    await process.wait()


async def run(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    if (output / "result.json").exists():
        raise ValueError("使用新目录保留基线")
    cases = []
    for index in range(3):
        cases.append(await lease_case(output / "workspace" / str(index), index))
    for total in (False, True):
        cases.append(
            await verification_case(
                output / "workspace" / ("total" if total else "cancel"), total_timeout=total
            )
        )
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
    asyncio.run(run(parser.parse_args().output.resolve()))


if __name__ == "__main__":
    main()
