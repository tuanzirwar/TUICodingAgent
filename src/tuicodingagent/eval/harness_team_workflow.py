"""真实 Git Worktree 中的双队员依赖编码流程；固定策略，零真实模型请求。"""
# ruff: noqa: ASYNC240

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from ..models import ProviderEvent, ProviderEventType, ToolCall
from ..runtime import ConversationRuntime
from ..subagent import Catalog
from ..subagent import Manager as SubAgentManager
from ..subagent import Service as SubAgentService
from ..team import Manager, TaskStore
from ..team.service import TeamService
from ..tools.registry import create_default_registry
from ..tools.team import TaskUpdateTool, TeamTaskListTool
from ..trace import load_events
from ..worktree import Manager as WorktreeManager
from .harness_regression import ScriptedProvider

PARSER = (
    "def parse_record(text):\n"
    "    pairs = [piece.split('=', 1) for piece in text.split(';') if piece.strip()]\n"
    "    return {key.strip(): value.strip() for key, value in pairs}\n"
)
FORMATTER = (
    "from parser import parse_record\n\ndef format_record(text):\n"
    "    data = parse_record(text)\n    return f\"{data['name']} ({data['age']})\"\n"
)
TESTS = (
    "from parser import parse_record\n\ndef test_trim():\n"
    "    assert parse_record(' name= Alice ; age=20 ') == {'name': 'Alice', 'age': '20'}\n"
    "\ndef test_empty():\n    assert parse_record('') == {}\n"
    "\ndef test_value_equals():\n    assert parse_record('token=x=y')['token'] == 'x=y'\n"
)
GOLD = (
    "from formatter import format_record\n\ndef test_format():\n"
    "    assert format_record(' name= Alice ; age=20 ') == 'Alice (20)'\n"
    "\ndef test_other_record():\n"
    "    assert format_record('age=31; name= Bob') == 'Bob (31)'\n"
)


async def git(root: Path, *args: str) -> str:
    result = await asyncio.to_thread(
        subprocess.run,
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout.strip()


async def pytest_verify(root: Path) -> dict:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:randomly",
        "-p",
        "no:cacheprovider",
        cwd=root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 30)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    return {"exit_code": process.returncode, "output": output.decode("utf-8", errors="replace")}


async def run(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    if (output / "result.json").exists():
        raise ValueError("使用新的输出目录")
    root = output / "workspace/repository"
    root.mkdir(parents=True)
    await git(root, "init", "-q")
    await git(root, "config", "user.name", "Harness Fixture")
    await git(root, "config", "user.email", "harness@example.invalid")
    (root / ".gitignore").write_text(
        ".tuicodingagent/\n__pycache__/\n.pytest_cache/\n", encoding="utf-8"
    )
    (root / "parser.py").write_text(
        "def parse_record(text):\n    return dict(piece.split('=') for piece in text.split(';'))\n",
        encoding="utf-8",
    )
    (root / "tests").mkdir()
    (root / "tests/test_parser.py").write_text(TESTS, encoding="utf-8")
    await git(root, "add", ".")
    await git(root, "commit", "-qm", "frozen fixture")
    baseline = await pytest_verify(root)
    teams = Manager(root / ".tuicodingagent/teams")
    team = await teams.create("dependency-workflow")
    store = TaskStore(team.config_dir / "tasks.json")
    upstream = await store.create("修复解析接口", assignee="alice")
    downstream = await store.create("基于接口实现格式化", assignee="bob", blocked_by=[upstream.id])
    ready = asyncio.Event()

    class Producer(ScriptedProvider):
        def __init__(self):
            super().__init__(
                [
                    ToolCall(
                        "a-start", "TaskUpdate", {"task_id": upstream.id, "status": "in_progress"}
                    ),
                    ToolCall("a-read", "read_file", {"path": "parser.py"}),
                    ToolCall("a-edit", "write_file", {"path": "parser.py", "content": PARSER}),
                    ToolCall(
                        "a-tests",
                        "bash",
                        {
                            "command": f'"{sys.executable}" -m pytest -q '
                            "-p no:randomly -p no:cacheprovider"
                        },
                    ),
                    ToolCall(
                        "a-complete", "TaskUpdate", {"task_id": upstream.id, "status": "completed"}
                    ),
                    "接口实现完成",
                ]
            )

    class Consumer(ScriptedProvider):
        def __init__(self):
            super().__init__()
            self.step = 0
            self.commit_from_message = ""
            self.corrected = False
            self.completion_retried = False

        async def stream(self, prompt, messages, tools=()):
            self.requests.append((str(prompt), tuple(messages), tuple(tools)))
            step = self.step
            self.step += 1
            if step == 0:
                call = ToolCall(
                    "b-too-early", "TaskUpdate", {"task_id": downstream.id, "status": "in_progress"}
                )
            elif step == 1:
                await asyncio.wait_for(ready.wait(), 25)
                call = ToolCall("b-refresh", "TaskList", {})
            elif step == 2:
                match = re.search(
                    r"artifact_commit=([0-9a-f]{40})",
                    "\n".join(message.content for message in messages),
                )
                if match is None:
                    raise ValueError("没有收到已验证的提交证据")
                self.commit_from_message = match[1]
                call = ToolCall("b-import", "bash", {"command": "git cherry-pick " + match[1]})
            elif step == 3:
                call = ToolCall(
                    "b-start", "TaskUpdate", {"task_id": downstream.id, "status": "in_progress"}
                )
            elif step == 4:
                call = ToolCall("b-read", "read_file", {"path": "parser.py"})
            elif step == 5:
                call = ToolCall(
                    "b-write", "write_file", {"path": "formatter.py", "content": FORMATTER}
                )
            elif step == 6:
                call = ToolCall(
                    "b-complete", "TaskUpdate", {"task_id": downstream.id, "status": "completed"}
                )
            else:
                feedback = any("verification-failed" in message.content for message in messages)
                if feedback and not self.corrected:
                    self.corrected = True
                    call = ToolCall(
                        "b-fix-lint",
                        "write_file",
                        {
                            "path": "formatter.py",
                            "content": FORMATTER.replace(
                                "parse_record\n\n", "parse_record\n\n\n", 1
                            ),
                        },
                    )
                    yield ProviderEvent(ProviderEventType.TOOL_CALL, tool_call=call)
                    return
                if self.corrected and not self.completion_retried:
                    self.completion_retried = True
                    yield ProviderEvent(
                        ProviderEventType.TOOL_CALL,
                        tool_call=ToolCall(
                            "b-complete-retry",
                            "TaskUpdate",
                            {"task_id": downstream.id, "status": "completed"},
                        ),
                    )
                    return
                yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="下游实现完成")
                return
            yield ProviderEvent(ProviderEventType.TOOL_CALL, tool_call=call)

    consumer = Consumer()
    providers = iter((consumer, Producer()))
    parent = ConversationRuntime(
        ScriptedProvider(), workspace=root, tool_registry=create_default_registry(root)
    )
    parent.tool_registry.register(TaskUpdateTool(teams))
    parent.tool_registry.register(TeamTaskListTool(teams))
    catalog = Catalog(root, user_root=output / "isolated-agents")
    catalog.load_all()
    tasks = SubAgentManager()
    subagents = SubAgentService(
        workspace=root,
        registry=parent.tool_registry,
        catalog=catalog,
        manager=tasks,
        parent_runtime=parent,
        provider_factory=lambda _config: next(providers),
        providers=(parent.provider.config,),
    )
    worktrees = WorktreeManager(root)
    service = TeamService(teams, subagents, worktrees, root)
    teams.cleanup_member = service.cleanup_member
    started = time.perf_counter()
    result = {
        "real_model_requests": 0,
        "mode": "scripted strategy with production TeamService and actual Git",
        "baseline": baseline,
    }
    try:
        launched_b = await service.spawn(
            team_name=team.name, name="bob", prompt="等待上游接口后实现格式化"
        )
        launched_a = await service.spawn(
            team_name=team.name, name="alice", prompt="修复 parser 并验证"
        )
        task_a = tasks.get(launched_a["task_id"])
        task_b = tasks.get(launched_b["task_id"])
        await asyncio.wait_for(task_a.handle, 25)
        path_a = Path(team.find_member("alice").worktree_path)
        verifier_a = await pytest_verify(path_a)
        if verifier_a["exit_code"] != 0 or task_a.status != "completed":
            raise RuntimeError("上游独立验证未通过")
        await git(path_a, "add", "parser.py")
        await git(path_a, "commit", "-qm", "verified parser artifact")
        commit = await git(path_a, "rev-parse", "HEAD")
        await service.send_from_lead(
            "bob", "上游已独立验证，artifact_commit=" + commit, team_name=team.name
        )
        ready.set()
        await asyncio.wait_for(task_b.handle, 25)
        path_b = Path(team.find_member("bob").worktree_path)
        unchanged_tests = (path_b / "tests/test_parser.py").read_text(encoding="utf-8") == TESTS
        (path_b / "tests/test_format_gold.py").write_text(GOLD, encoding="utf-8")
        verifier_b = await pytest_verify(path_b)
        await git(path_b, "add", "-N", "formatter.py")
        (output / "consumer.patch").write_text(
            await git(path_b, "diff", "HEAD", "--", "formatter.py"), encoding="utf-8"
        )
        (output / "producer.patch").write_text(
            await git(path_a, "show", "--format=", "HEAD", "--", "parser.py"), encoding="utf-8"
        )
        b_results = [r for m in task_b.runtime.history for r in m.tool_results]
        early = next((r for r in b_results if r.tool_call_id == "b-too-early"), None)
        early_complete = next((r for r in b_results if r.tool_call_id == "b-complete"), None)
        tool_events = load_events(parent.tool_registry.trace_recorder.path)
        a_id = team.find_member("alice").agent_id
        b_id = team.find_member("bob").agent_id
        trace_correct = all(
            row["agent_id"] == (a_id if row["request_id"].startswith("a-") else b_id)
            for row in tool_events
            if row["request_id"].startswith(("a-", "b-"))
        )
        result.update(
            {
                "upstream_verifier": verifier_a,
                "downstream_verifier": verifier_b,
                "early_start_rejected": early is not None
                and not early.success
                and "依赖" in early.error,
                "commit_used_from_message": consumer.commit_from_message == commit,
                "upstream_tests_unchanged": unchanged_tests,
                "main_repository_unchanged": not (root / "formatter.py").exists(),
                "task_states": store.list(),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "worker_status": {"alice": task_a.status, "bob": task_b.status},
                "worker_errors": {"alice": str(task_a.err), "bob": str(task_b.err)},
                "error_then_adjusted": consumer.corrected,
                "premature_completion_rejected": early_complete is not None
                and not early_complete.success
                and early_complete.metadata.get("verification_failed") is True,
                "trace_attribution_correct": trace_correct,
                "final_completion_retried": consumer.completion_retried,
                "verification_feedback": [
                    m.content for m in task_b.runtime.history if "verification-failed" in m.content
                ],
                "passed": baseline["exit_code"] != 0
                and verifier_a["exit_code"] == 0
                and verifier_b["exit_code"] == 0
                and early is not None
                and not early.success
                and unchanged_tests
                and consumer.commit_from_message == commit
                and task_b.status == "completed",
            }
        )
    except Exception as exc:
        result.update({"passed": False, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        for task in tasks.tasks.values():
            if task.runtime.trace_recorder.path.exists():
                (output / (task.name + "-trace.json")).write_text(
                    json.dumps(
                        load_events(task.runtime.trace_recorder.path), ensure_ascii=False, indent=2
                    ),
                    encoding="utf-8",
                )
            (output / (task.name + "-history.json")).write_text(
                json.dumps(
                    [
                        {
                            "role": m.role,
                            "content": m.content,
                            "tool_results": [
                                {
                                    "id": r.tool_call_id,
                                    "name": r.name,
                                    "success": r.success,
                                    "output": r.output,
                                    "error": r.error,
                                }
                                for r in m.tool_results
                            ],
                        }
                        for m in task.runtime.history
                    ],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        for task in tasks.tasks.values():
            if task.handle is not None and not task.handle.done():
                task.handle.cancel()
        await asyncio.gather(
            *(t.handle for t in tasks.tasks.values() if t.handle is not None),
            return_exceptions=True,
        )
        await teams.delete(team.name, force=True)
        for task in tasks.tasks.values():
            await task.runtime.close()
        await parent.close()
    result["worktrees_cleaned"] = not worktrees.list()
    result["gold_sha256"] = hashlib.sha256((TESTS + GOLD).encode()).hexdigest()
    (output / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps({"passed": result["passed"], "worktrees_cleaned": result["worktrees_cleaned"]})
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args().output.resolve()))


if __name__ == "__main__":
    main()
