"""队员模型处理期间真进程死亡与租约恢复实验。"""
# ruff: noqa: ASYNC240

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from ..models import ProviderEvent, ProviderEventType, RuntimeEventType
from ..runtime import ConversationRuntime
from ..session import SessionContext, Writer, restore_session, scan_sessions
from ..team.delivery import MailboxDelivery
from ..team.mailbox import Mailbox, Message
from .harness_regression import ScriptedProvider


async def child(root: Path) -> None:
    class InterruptedProvider(ScriptedProvider):
        async def stream(self, *args, **kwargs):
            (root / "ready").write_text("model-started", encoding="utf-8")
            yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="处理中")
            await asyncio.Event().wait()

    runtime = ConversationRuntime(
        InterruptedProvider(),
        workspace=root,
        session_writer=Writer(SessionContext.create(root), "fixture"),
    )
    MailboxDelivery(runtime, Mailbox(root / "mailbox"), "alice", ack_timeout=0.3).bind()
    try:
        _ = [e async for e in runtime.chat("处理真实队员消息")]
    finally:
        await runtime.close()


async def run(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    if (output / "result.json").exists():
        raise ValueError("使用新的结果目录")
    root = output / "workspace"
    root.mkdir()
    mailbox = Mailbox(root / "mailbox")
    identifier = await mailbox.write("alice", Message("lead", "唯一依赖消息 37"))
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tuicodingagent.eval.harness_team_crash",
        "--child",
        str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(10):
            while not (root / "ready").exists():
                if process.returncode is not None:
                    _, error = await process.communicate()
                    raise RuntimeError(error.decode("utf-8", errors="replace"))
                await asyncio.sleep(0.02)
        with mailbox._connect() as connection:
            inflight = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE status='inflight'"
            ).fetchone()[0]
        process.kill()
        await process.wait()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    await asyncio.sleep(0.4)
    recovered = Mailbox(root / "mailbox")
    pending = await recovered.read("alice")
    runtime = ConversationRuntime(ScriptedProvider(["已处理恢复消息"]), workspace=root)
    restored = restore_session(scan_sessions(root)[0])
    await runtime.restore(restored, Writer(restored.info.context, "fixture"))
    MailboxDelivery(runtime, recovered, "alice").bind()
    try:
        events = [event async for event in runtime.chat("继续处理未确认消息")]
    finally:
        await runtime.close()
    after = await recovered.read("alice")
    result = {
        "real_model_requests": 0,
        "child_exit_code": process.returncode,
        "inflight_before_kill": inflight,
        "recovered_ids": [m.message_id for m in pending],
        "original_id": identifier,
        "retry_count": pending[0].retry_count if pending else None,
        "completed_after_resume": events[-1].type == RuntimeEventType.COMPLETED,
        "pending_after_success": len(after),
        "passed": process.returncode != 0
        and inflight == 1
        and [m.message_id for m in pending] == [identifier]
        and events[-1].type == RuntimeEventType.COMPLETED
        and not after,
    }
    (output / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"passed": result["passed"]}))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", type=Path)
    args = parser.parse_args()
    asyncio.run(child(args.child.resolve()) if args.child else run(args.output.resolve()))


if __name__ == "__main__":
    main()
