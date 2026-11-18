"""独立进程争抢和真实 Textual 界面的消息交接实验。"""
# ruff: noqa: ASYNC240, ASYNC110

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

from ..errors import ProviderError
from ..models import AppConfig, ProviderConfig, ProviderEvent, ProviderEventType, SessionState
from ..session import restore_session, scan_sessions
from ..team import Manager
from ..team.mailbox import Mailbox, Message
from ..team.tasks import TaskStore
from ..tui.app import TUICodingAgentApp
from .harness_regression import ScriptedProvider


async def contender(root: Path, actor: str, task_id: str) -> None:
    (root / (actor + ".ready")).write_text("ready", encoding="utf-8")
    async with asyncio.timeout(10):
        while not (root / "go").exists():
            await asyncio.sleep(0.01)
    try:
        task = await TaskStore(root / "tasks.json").update(
            task_id, actor=actor, assignee=actor, status="in_progress", expected_revision=0
        )
        result = {"actor": actor, "accepted": True, "revision": task.revision}
    except ValueError as exc:
        result = {"actor": actor, "accepted": False, "reason": str(exc)}
    (root / (actor + ".json")).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


async def process_case(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    store = TaskStore(root / "tasks.json")
    task = await store.create("同一任务只能被一个进程领取")
    processes = []
    try:
        for actor in ("alice", "bob"):
            processes.append(
                await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "tuicodingagent.eval.harness_coordination_extended",
                    "--child",
                    str(root),
                    "--actor",
                    actor,
                    "--task-id",
                    task.id,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            )
        async with asyncio.timeout(15):
            while not all((root / (actor + ".ready")).exists() for actor in ("alice", "bob")):
                if any(process.returncode is not None for process in processes):
                    raise RuntimeError("子进程在并发屏障之前退出")
                await asyncio.sleep(0.02)
            (root / "go").write_text("go", encoding="utf-8")
            outputs = await asyncio.gather(*(process.communicate() for process in processes))
        if any(process.returncode != 0 for process in processes):
            raise RuntimeError(str([error.decode(errors="replace") for _, error in outputs]))
        rows = [
            json.loads((root / (actor + ".json")).read_text(encoding="utf-8"))
            for actor in ("alice", "bob")
        ]
        current = store.get(task.id)
        accepted = [row for row in rows if row["accepted"]]
        return {
            "scenario": "cross_process_exclusive_claim",
            "workers": rows,
            "exit_codes": [process.returncode for process in processes],
            "owner": current.assignee,
            "revision": current.revision,
            "passed": len(accepted) == 1
            and current.assignee == accepted[0]["actor"]
            and current.revision == 1,
        }
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()


async def tui_case(root: Path, *, fail: bool) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    started, finish = asyncio.Event(), asyncio.Event()
    text = "队员完整证据：" + "中文长消息" * 2400 + "\nPINNED-END-37"

    class Provider(ScriptedProvider):
        async def stream(self, system_prompt, messages, tools=()):
            self.requests.append((system_prompt, list(messages), list(tools)))
            started.set()
            await finish.wait()
            if fail:
                raise ProviderError("可控的模型服务失败")
            yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="已处理完整队员消息")

    provider = Provider()
    config = AppConfig((ProviderConfig("fixture", "openai", "fixture", "fake"),))
    app = TUICodingAgentApp(config, workspace=root, provider_factory=lambda _: provider)
    # 界面照常创建 TeamManager，但运行状态限定在本实验目录。
    with patch("tuicodingagent.tui.app.TeamManager", lambda: Manager(root / "teams")):
        async with app.run_test(size=(100, 28)) as pilot:
            await pilot.pause()
            team = await app.team_manager.create("ui-delivery")
            mailbox = Mailbox(team.config_dir / "mailbox")
            identifier = await mailbox.write(team.lead_agent_id, Message("alice", text))
            await asyncio.wait_for(started.wait(), 8)
            # 停止下一轮轮询，冻结一次模型失败的观测，防止自动重试混入样本。
            app._lead_mail_task.cancel()
            await asyncio.gather(app._lead_mail_task, return_exceptions=True)
            with mailbox._connect() as connection:
                before = connection.execute(
                    "SELECT status FROM messages WHERE id=?", (identifier,)
                ).fetchone()[0]
            finish.set()
            async with asyncio.timeout(8):
                while app.session_state != SessionState.IDLE:
                    await pilot.pause(0.02)
            with mailbox._connect() as connection:
                after = connection.execute(
                    "SELECT status FROM messages WHERE id=?", (identifier,)
                ).fetchone()[0]
            input_complete = any(
                text in message.content
                for _, messages, _ in provider.requests
                for message in messages
            )
    restored = restore_session(scan_sessions(root)[0])
    durable = any(text in message.content for message in restored.messages)
    return {
        "scenario": "tui_model_error_retains_mail" if fail else "tui_long_mail_success",
        "input_characters": len(text),
        "model_fixture_requests": len(provider.requests),
        "status_during_model": before,
        "status_after_model": after,
        "full_model_input": input_complete,
        "full_durable_input": durable,
        "passed": before == "inflight"
        and after == ("pending" if fail else "acked")
        and input_complete
        and durable
        and len(provider.requests) == 1,
    }


async def run(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    if (output / "result.json").exists():
        raise ValueError("使用新目录保留原始证据")
    cases = [await process_case(output / "workspace" / "process")]
    for fail in (False, True):
        cases.append(
            await tui_case(output / "workspace" / ("failure" if fail else "success"), fail=fail)
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
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", type=Path)
    parser.add_argument("--actor")
    parser.add_argument("--task-id")
    args = parser.parse_args()
    asyncio.run(
        contender(args.child, args.actor, args.task_id)
        if args.child
        else run(args.output.resolve())
    )


if __name__ == "__main__":
    main()
