"""冻结场景的生产 Harness 集成实验；确定性驱动不代表模型能力。"""
# ruff: noqa: ASYNC240

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import platform
import subprocess
import sys
import time
from pathlib import Path

from ..context import ContextController
from ..memory import MemoryManager
from ..models import (
    ChatMessage,
    PermissionMode,
    ProviderConfig,
    ProviderEvent,
    ProviderEventType,
    ToolCall,
    ToolResult,
)
from ..permissions import PermissionManager
from ..runtime import ConversationRuntime
from ..session import SessionContext, Writer, restore_session, scan_sessions
from ..tools.registry import create_default_registry


class ScriptedProvider:
    """执行固定策略，只测真实 Runtime 的合同与文件操作。"""

    def __init__(self, replies=(), *, window=80_000) -> None:
        self.config = ProviderConfig(
            "contract-local", "openai", "script-v1", "unused", context_window=window
        )
        self.replies = list(replies)
        self.requests = []

    async def stream(self, system_prompt, messages, tools=()):
        self.requests.append((str(system_prompt), tuple(messages), tuple(tools)))
        if "九个编号部分" in str(system_prompt):
            # 摘要保留明确标记；此规则策略不测真实模型摘要质量。
            facts = [m.content for m in messages if "PINNED:" in m.content]
            text = "<summary>" + "\n".join(f"{i}. 状态" for i in range(1, 10))
            text += "\n" + "\n".join(facts) + "</summary>"
            yield ProviderEvent(ProviderEventType.TEXT_DELTA, text=text)
        else:
            reply = self.replies.pop(0) if self.replies else "完成"
            if isinstance(reply, ToolCall):
                yield ProviderEvent(ProviderEventType.TOOL_CALL, tool_call=reply)
            elif isinstance(reply, tuple):
                for call in reply:
                    yield ProviderEvent(ProviderEventType.TOOL_CALL, tool_call=call)
            else:
                yield ProviderEvent(ProviderEventType.TEXT_DELTA, text=reply)

    async def close(self):
        return None


def _runtime(root, provider, *, controller=None, writer=None, memory=None, iterations=6):
    return ConversationRuntime(
        provider,
        workspace=root,
        tool_registry=create_default_registry(root),
        permission_manager=PermissionManager(root, mode=PermissionMode.BYPASS),
        context_controller=controller,
        session_writer=writer,
        memory_manager=memory,
        max_iterations=iterations,
    )


async def context_case(root: Path, index: int) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    window = (40_000, 64_000, 100_000)[index // 4]
    payload = (0, 60_000, 210_000, 400_000)[index % 4]
    provider = ScriptedProvider(window=window)
    controller = ContextController(root, window)
    call = ToolCall("payload", "read_file", {"path": "payload.txt"})
    history = tuple(
        m
        for _ in range(24)
        for m in (ChatMessage("user", "历史诊断" * 300), ChatMessage("assistant", "历史分析" * 300))
    )
    current = f"CURRENT-{index:02d}: 请仅修改当前模块，保留 Unicode 参数：青鸟。"
    messages = (
        *history,
        ChatMessage("user", "PINNED: contract=keep-api"),
        ChatMessage("assistant", tool_calls=(call,)),
        ChatMessage(
            "tool", tool_results=(ToolResult(call.id, call.name, True, "日志" * (payload // 6)),)
        ),
        ChatMessage("user", current),
    )
    before = controller.estimate_tokens(messages)
    managed, offloaded = await controller.offload_and_snip(messages)
    result = await controller.compact(provider, managed, (), "manual")
    content = "\n".join(m.content for m in result.messages)
    paired = all(
        i > 0 and result.messages[i - 1].role == "assistant"
        for i, m in enumerate(result.messages)
        if m.role == "tool"
    )
    return {
        "window": window,
        "payload_bytes": payload,
        "before_tokens_estimated": before,
        "after_tokens_estimated": result.after_tokens,
        "offloaded": offloaded,
        "current_request_exact": any(m.content == current for m in result.messages),
        "fact_preserved": "PINNED: contract=keep-api" in content,
        "tool_pair_preserved": paired,
        "within_budget": result.after_tokens < window - 3000,
        "passed": result.success
        and current in content
        and paired
        and "PINNED: contract=keep-api" in content
        and result.after_tokens < window - 3000,
    }


async def memory_case(root: Path, index: int) -> dict:
    """跨实例重建记忆并检查注入；读取收益由后续真实模型实验判定。"""
    root.mkdir(parents=True, exist_ok=True)
    expected = f"service_{index:02d}=PORT-{7100 + index}"
    home = root / "isolated-home"
    manager = MemoryManager(root, home)
    operation = [
        {
            "action": "create",
            "level": "project",
            "type": "project_knowledge",
            "title": f"配置 {index}",
            "slug": f"service_{index}",
            "content": expected,
        }
    ]
    saved = await manager.update(
        ScriptedProvider((json.dumps(operation),)), (ChatMessage("user", expected),)
    )
    fresh = MemoryManager(root, home)
    with_memory = ScriptedProvider()
    without_memory = ScriptedProvider()
    enabled = _runtime(root, with_memory, memory=fresh)
    disabled = _runtime(root / "control", without_memory)
    try:
        await enabled.run_to_completion("查询已记录的服务端口")
        await disabled.run_to_completion("查询已记录的服务端口")
    finally:
        await enabled.close()
        await disabled.close()
    return {
        "memory_saved": saved,
        "cross_instance_loaded": expected in fresh.index_text,
        "enabled_injection": expected in with_memory.requests[0][0],
        "control_injection": expected in without_memory.requests[0][0],
        "repeat_reads_saved": None,
        "answer_accuracy": None,
        "passed": saved
        and expected in with_memory.requests[0][0]
        and expected not in without_memory.requests[0][0],
    }


RECOVERY_CASES = (
    "complete",
    "bad_json",
    "scalar_json",
    "torn_compact",
    "orphan_call",
    "partial_results",
    "changed_file",
    "deleted_file",
    "outside_file",
    "large_output",
)


async def recovery_case(root: Path, index: int) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    kind = RECOVERY_CASES[index]
    context = SessionContext.create(root)
    file = root / "tracked.txt"
    file.write_text("old fact\n", encoding="utf-8")
    read = await create_default_registry(root).execute(
        ToolCall("read", "read_file", {"path": "tracked.txt"})
    )
    with Writer(context, "script-v1") as writer:
        writer.append(ChatMessage("user", "恢复任务"))
        if kind in {"changed_file", "deleted_file", "outside_file", "large_output"}:
            if kind == "outside_file":
                read = ToolResult(
                    "read",
                    "read_file",
                    True,
                    "old fact",
                    metadata={"path": str(root.parent / "foreign.txt"), "content_sha256": "fake"},
                )
            if kind == "large_output":
                read = ToolResult("read", "read_file", True, "x" * 120_000)
            writer.append(ChatMessage("assistant", tool_calls=(ToolCall("read", "read_file", {}),)))
            writer.append(ChatMessage("tool", tool_results=(read,)))
        writer.append(ChatMessage("assistant", "已完成已知步骤"))
        if kind in {"orphan_call", "partial_results"}:
            writer.append(
                ChatMessage(
                    "assistant",
                    tool_calls=(ToolCall("a", "read_file", {}), ToolCall("b", "write_file", {})),
                )
            )
            if kind == "partial_results":
                writer.append(
                    ChatMessage("tool", tool_results=(ToolResult("a", "read_file", True),))
                )
    with context.conversation_path.open("a", encoding="utf-8") as stream:
        if kind == "bad_json":
            stream.write("{broken\n")
        if kind == "scalar_json":
            stream.write("[]\nnull\n42\n")
        if kind == "torn_compact":
            stream.write('{"type":"compact","ts":1}\n{"role":')
    if kind == "changed_file":
        file.write_text("new fact\n", encoding="utf-8")
    if kind == "deleted_file":
        file.unlink()
    restored = restore_session(scan_sessions(root)[0])
    text = "\n".join(m.content for m in restored.messages)
    drift = "工作区变化" in text
    expected_drift = kind in {"changed_file", "deleted_file", "outside_file"}
    safe = bool(restored.messages)
    if kind in {"orphan_call", "partial_results"}:
        safe = safe and not any(c.id == "b" for m in restored.messages for c in m.tool_calls)
    if kind == "large_output":
        provider = ScriptedProvider()
        runtime = _runtime(root, provider, controller=ContextController(root, 80_000))
        await runtime.restore(restored, Writer(context, "script-v1"))
        safe = safe and all(len(r.output) < 5000 for m in runtime.history for r in m.tool_results)
        await runtime.close()
    return {
        "scenario": kind,
        "message_count": len(restored.messages),
        "workspace_drift_detected": drift,
        "expected_drift": expected_drift,
        "passed": safe and (drift == expected_drift),
    }


GOVERNANCE_CASES = (
    "nonzero_shell_evidence",
    "budget_exhaustion",
    "duplicate_artifact_id",
    "snapshot_changed",
    "snapshot_deleted",
    "syntax_verifier",
    "shell_edit_verifier",
)


async def governance_case(root: Path, index: int) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    kind = GOVERNANCE_CASES[index]
    registry = create_default_registry(root)
    observed = {}
    if kind == "nonzero_shell_evidence":
        command = f'"{sys.executable}" -c "print(\'diagnostic evidence\'); raise SystemExit(2)"'
        result = await registry.execute(ToolCall("shell", "bash", {"command": command}))
        passed = not result.success and "diagnostic evidence" in result.model_content()
        observed = {"exit_code": result.metadata.get("exit_code"), "evidence_visible": passed}
    elif kind == "budget_exhaustion":
        provider = ScriptedProvider((ToolCall("read", "read_file", {"path": "missing"}),))
        runtime = _runtime(root, provider, iterations=1)
        try:
            await runtime.run_to_completion("完成缺陷修复")
            passed = False
        except RuntimeError:
            passed = True
        finally:
            await runtime.close()
    elif kind == "duplicate_artifact_id":
        for session in ("one", "two"):
            artifact = root / ".tuicodingagent" / "sessions" / session / "tool-results" / "same"
            artifact.parent.mkdir(parents=True)
            artifact.write_text(session, encoding="utf-8")
        result = await registry.execute(ToolCall("read", "read_artifact", {"artifact": "same"}))
        passed = not result.success
        observed = {"ambiguous_id_rejected": passed}
    elif kind.startswith("snapshot_"):
        file = root / "file.txt"
        file.write_text("STALE_SNAPSHOT", encoding="utf-8")
        controller = ContextController(root, 80_000)
        await controller.record_read(
            ToolCall("read", "read_file", {"path": str(file)}),
            ToolResult("read", "read_file", True),
        )
        if kind == "snapshot_changed":
            file.write_text("FRESH_SNAPSHOT", encoding="utf-8")
        else:
            file.unlink()
        message = await controller._recovery_message(())
        passed = "STALE_SNAPSHOT" not in message.content and "工作区变化" in message.content
        observed = {"stale_content_removed": "STALE_SNAPSHOT" not in message.content}
    else:
        await asyncio.to_thread(subprocess.run, ["git", "init", "-q"], cwd=root, check=True)
        if kind == "syntax_verifier":
            call = ToolCall("write", "write_file", {"path": "broken.py", "content": "broken ="})
        else:
            command = (
                f'"{sys.executable}" -c '
                "\"from pathlib import Path; Path('broken.py').write_text('broken =')\""
            )
            call = ToolCall("shell", "bash", {"command": command})
        provider = ScriptedProvider((call, "完成", "完成", "完成"))
        runtime = _runtime(root, provider)
        events = [event async for event in runtime.chat("修改代码并验证")]
        await runtime.close()
        passed = "自动验证未通过" in events[-1].text
        observed = {"failed_verifier_disclosed": passed}
    return {"scenario": kind, **observed, "passed": passed}


async def process_case(root: Path, index: int) -> dict:
    """真实杀死工作进程，核验已 fsync 的状态与不确定副作用。"""
    root.mkdir(parents=True, exist_ok=True)
    script = root / "actor.py"
    source_root = str(Path(__file__).resolve().parents[2])
    source = """import asyncio, sys, time
from pathlib import Path
sys.path.insert(0, SOURCE_ROOT)
from tuicodingagent.session import SessionContext, Writer
from tuicodingagent.models import ChatMessage, ToolCall
from tuicodingagent.tools.registry import create_default_registry
root = Path.cwd()
context = SessionContext.create(root)
writer = Writer(context, "process-fixture")
writer.append(ChatMessage("user", "持久写入后模拟突然死亡"))
call = ToolCall("effect", "write_file", {"path":"effect.txt", "content":"exactly-once-observed"})
writer.append(ChatMessage("assistant", tool_calls=(call,)))
result = asyncio.run(create_default_registry(root).execute(call))
if SAVE_RESULT:
    writer.append(ChatMessage("tool", tool_results=(result,)))
(root / "ready").write_text(context.session_id, encoding="utf-8")
while True:
    time.sleep(1)
""".replace("SOURCE_ROOT", repr(source_root)).replace("SAVE_RESULT", repr(index == 1))
    script.write_text(source, encoding="utf-8")
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(script), cwd=root, stderr=asyncio.subprocess.PIPE
    )
    try:
        async with asyncio.timeout(10):
            while not (root / "ready").exists():
                if process.returncode is not None:
                    _, error = await process.communicate()
                    raise RuntimeError(error.decode("utf-8", errors="replace"))
                await asyncio.sleep(0.02)
        process.kill()
        await process.wait()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    restored = restore_session(scan_sessions(root)[0])
    uncertain = any("可能已经产生副作用" in m.content for m in restored.messages)
    effect = (root / "effect.txt").read_text(encoding="utf-8")
    results = [r for m in restored.messages for r in m.tool_results]
    return {
        "scenario": "kill_after_result" if index else "kill_before_result",
        "process_killed": process.returncode != 0,
        "effect_preserved": effect,
        "uncertain_effect_warning": uncertain,
        "persisted_results": len(results),
        "passed": process.returncode != 0
        and effect == "exactly-once-observed"
        and ((index == 0 and uncertain) or (index == 1 and len(results) == 1)),
    }


async def evidence_case(root: Path, index: int) -> dict:
    """固定文字检索与来源行号合同，不测语义 RAG。"""
    from ..tools.artifacts import ReadArtifactTool, SearchArtifactTool

    managed = root / ".tuicodingagent" / "artifacts"
    managed.mkdir(parents=True, exist_ok=True)
    target = managed / "source.txt"
    query = f"证据青鸟-{index}" if index % 2 else f"Service_TOKEN_{index}"
    evidence = f"{query}: timeout={17 + index}"
    content = f"标题\n{evidence}\n末尾\n"
    target.write_text(content, encoding="utf-8")
    (managed / "noise.txt").write_text("无关文本\n其他服务\n", encoding="utf-8")
    # 工作区普通文件不属于托管证据集合。
    (root / "unmanaged.txt").write_text(evidence, encoding="utf-8")
    lookup = "不存在的固定证据" if index == 11 else query.swapcase()
    search = await SearchArtifactTool(root).execute(
        ToolCall("search", "search_artifact", {"query": lookup})
    )
    expected = "未找到匹配内容" if index == 11 else f"{target.resolve()}:2:{evidence}"
    read = await ReadArtifactTool(root).execute(
        ToolCall(
            "read", "read_artifact", {"artifact": str(target.resolve()), "offset": 2, "limit": 1}
        )
    )
    return {
        "query": lookup,
        "corpus_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "expected_matches": 0 if index == 11 else 1,
        "actual_matches": search.metadata.get("matches"),
        "source_and_line_exact": search.output == expected,
        "read_back_exact": read.output == f"     2\t{evidence}",
        "passed": search.success
        and search.output == expected
        and read.success
        and read.output == f"     2\t{evidence}",
    }


async def run_suite(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    families = (
        ("context", context_case, 12),
        ("memory_contract", memory_case, 12),
        ("recovery", recovery_case, 10),
        ("governance", governance_case, 7),
        ("process_crash", process_case, 2),
        ("evidence_lookup", evidence_case, 12),
    )
    for family, function, count in families:
        for index in range(count):
            started = time.perf_counter()
            try:
                observation = await function(output / "workspace" / family / str(index), index)
            except Exception as exc:
                observation = {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
            row = {
                "family": family,
                "case": index,
                "execution_mode": "local-contract",
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                **observation,
            }
            rows.append(row)
            (output / "progress.json").write_text(
                json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    result = {"total": len(rows), "passed": sum(row["passed"] for row in rows), "cases": rows}
    (output / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with (output / "result.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    lines = [
        "# Harness 集成实验",
        "",
        "确定性驱动；不代表真实模型成功率。",
        "",
        "| 维度 | 通过 | 总数 |",
        "|---|---:|---:|",
    ]
    for family, _, count in families:
        passed = sum(row["passed"] for row in rows if row["family"] == family)
        lines.append(f"| {family} | {passed} | {count} |")
    lines += [
        "",
        "记忆重复读取收益与答案准确率为 null，必须由真实模型对照测量。",
        "",
        "## 未通过场景",
        "",
    ]
    lines += [
        f"- {r['family']}/{r['case']}: {r.get('scenario', '')} {r.get('error', '')}"
        for r in rows
        if not r["passed"]
    ]
    (output / "result.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    manifest = {
        "python": sys.version,
        "platform": platform.platform(),
        "suite_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "execution_mode": "local-contract",
        "real_model_requests": 0,
        "families": {name: count for name, _, count in families},
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"total": result["total"], "passed": result["passed"]}))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run_suite(args.output.resolve()))


if __name__ == "__main__":
    main()
