"""真实磁盘副作用计数实验，固定驱动且不消耗模型请求。"""
# ruff: noqa: ASYNC240

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from ..models import ChatMessage, RuntimeEventType, ToolCall, ToolDefinition, ToolResult
from ..runtime import ConversationRuntime
from ..session import SessionContext, Writer, restore_session, scan_sessions
from ..tools.registry import ToolRegistry
from .harness_regression import ScriptedProvider


class DiskCounter:
    """用磁盘计数检测重复执行，也可模拟副作用已发生但报告失败。"""

    read_only = False
    definition = ToolDefinition("disk_counter", "实验计数器", {"type": "object"})

    def __init__(self, path: Path) -> None:
        self.path = path

    async def execute(self, call: ToolCall) -> ToolResult:
        await asyncio.to_thread(self._increment)
        if call.arguments.get("partial_failure"):
            return ToolResult(call.id, call.name, False, error="副作用已经发生，后续步骤失败")
        return ToolResult(call.id, call.name, True, "计数已增加")

    def _increment(self) -> None:
        current = int(self.path.read_text()) if self.path.exists() else 0
        self.path.write_text(str(current + 1), encoding="utf-8")


async def side_effect_case(root: Path, index: int) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    first = ToolCall(
        "effect", "write_file" if index >= 6 else "disk_counter", {"partial_failure": index == 5}
    )
    second = ToolCall(
        "new-effect" if index == 3 else "effect",
        first.name,
        {"changed": True} if index == 1 else dict(first.arguments),
    )
    replies = [(first, second), "完成"] if index == 2 else [first, second, "完成"]
    if index == 4:
        replies = [first, "第一轮完成", second, "第二轮完成"]
    provider = ScriptedProvider(replies)
    path = root / "counter.txt"
    counter = DiskCounter(path)
    if index >= 6:
        counter.definition = ToolDefinition("write_file", "实验恢复写入", {"type": "object"})
    registry = ToolRegistry((counter,))
    runtime = ConversationRuntime(provider, tool_registry=registry, workspace=root)
    if index >= 6:
        context = SessionContext.create(root)
        result = await counter.execute(first)
        with Writer(context, "fixture") as writer:
            writer.append(ChatMessage("user", "恢复测试"))
            writer.append(ChatMessage("assistant", tool_calls=(first,)))
            if index == 6:
                writer.append(ChatMessage("tool", tool_results=(result,)))
        restored = restore_session(scan_sessions(root)[0])
        await runtime.restore(restored, Writer(context, "fixture"))
        provider.replies = [first, "完成"]
    try:
        events = [event async for event in runtime.chat("执行计数任务")]
        if index == 4:
            events.extend([event async for event in runtime.chat("新需求：再增加一次")])
    finally:
        await runtime.close()
    expected = 0 if index == 2 else 2 if index in (3, 4) else 1
    observed = int(path.read_text()) if path.exists() else 0
    results = [e.tool_result for e in events if e.type == RuntimeEventType.TOOL_FINISHED]
    failed = any(e.type == RuntimeEventType.FAILED for e in events)
    conflict_rejected = index != 1 or (len(results) >= 2 and not results[1].success)
    return {
        "scenario": (
            "same_id_replay",
            "same_id_changed_arguments",
            "duplicate_ids_in_one_round",
            "new_ids_are_distinct",
            "new_user_turn_is_distinct",
            "partial_failure_replay",
            "resume_durable_result_replay",
            "resume_uncertain_effect_replay",
        )[index],
        "expected_effects": expected,
        "observed_effects": observed,
        "runtime_failed": failed,
        "conflict_rejected": conflict_rejected,
        "results": [
            {"success": r.success, "error": r.error, "metadata": r.metadata} for r in results
        ],
        "passed": observed == expected and conflict_rejected and (index != 2 or failed),
    }


async def run(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    if (output / "result.json").exists():
        raise ValueError("使用新目录以保留原始证据")
    cases = [await side_effect_case(output / "workspace" / str(i), i) for i in range(8)]
    data = {
        "real_model_requests": 0,
        "passed": sum(c["passed"] for c in cases),
        "total": 8,
        "cases": cases,
    }
    (output / "result.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"passed": data["passed"], "total": 8}))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args().output.resolve()))


if __name__ == "__main__":
    main()
