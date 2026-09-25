"""长会话 benchmark runner 与统一指标聚合。"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import ConfigLoader
from .context import ContextController
from .memory import MemoryManager
from .models import ProviderConfig, ProviderEvent, ProviderEventType, ToolCall
from .providers import create_provider
from .runtime import ConversationRuntime
from .session import SessionContext, Writer, restore_session, scan_sessions
from .tools import create_default_registry
from .trace import load_events


class SyntheticProvider:
    """无有效凭据时用于工程稳定性长跑的确定性 provider。"""

    def __init__(self) -> None:
        self.config = ProviderConfig(
            "synthetic-deterministic",
            "openai",
            "synthetic-v1",
            "unused",
            context_window=80_000,
        )
        self.calls = 0

    async def stream(self, system_prompt, messages, tools=()):
        self.calls += 1
        input_tokens = max(1, len(repr((system_prompt, messages, tools)).encode("utf-8")) // 4)
        latest = messages[-1] if messages else None
        if "长期记忆" in str(system_prompt):
            text = "[]"
            yield ProviderEvent(ProviderEventType.TEXT_DELTA, text=text)
        elif "九个编号部分" in str(system_prompt):
            text = (
                "<summary>1. 请求\n2. 概念\n3. 文件\n4. 错误\n5. 过程\n"
                "6. 用户消息\n7. 待办\n8. 当前工作\n9. 下一步</summary>"
            )
            yield ProviderEvent(ProviderEventType.TEXT_DELTA, text=text)
        elif latest is not None and latest.role == "user" and "read_file" in latest.content:
            text = ""
            yield ProviderEvent(
                ProviderEventType.TOOL_CALL,
                tool_call=ToolCall(f"read-{self.calls}", "read_file", {"path": "payload.txt"}),
            )
        else:
            text = f"synthetic turn {self.calls}"
            yield ProviderEvent(ProviderEventType.TEXT_DELTA, text=text)
        yield ProviderEvent(
            ProviderEventType.USAGE,
            input_tokens=input_tokens,
            output_tokens=max(1, len(text.encode("utf-8")) // 4),
        )

    async def close(self) -> None:
        return None


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[max(0, int(len(ordered) * percentile) - 1)], 3)


def _storage(root: Path) -> dict[str, int]:
    groups = {"jsonl": 0, "artifact": 0, "summary": 0, "memory": 0}
    if not root.exists():
        return groups
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        size = path.stat().st_size
        normalized = str(path).replace("\\", "/").casefold()
        if path.suffix == ".jsonl":
            groups["jsonl"] += size
        if "/tool-results/" in normalized or "/artifacts/" in normalized:
            groups["artifact"] += size
        if "summary" in path.name.casefold():
            groups["summary"] += size
        if "/memory/" in normalized:
            groups["memory"] += size
    return groups


def aggregate(
    events: list[dict[str, Any]], workspace: Path, started: float, storage_before: dict[str, int]
) -> dict[str, Any]:
    llm = [row for row in events if row.get("event_type") == "llm_request"]
    turns = [row for row in events if row.get("event_type") == "turn"]
    tools = [row for row in events if row.get("event_type") == "tool_call"]
    compression = [row for row in events if row.get("event_type") == "compression"]
    inputs = [int(row["input_tokens"]) for row in llm if row.get("input_tokens") is not None]
    outputs = [int(row["output_tokens"]) for row in llm if row.get("output_tokens") is not None]
    contexts = [int(row["context_tokens"]) for row in llm if row.get("context_tokens") is not None]
    turn_latencies = [
        float(row["tool_latency_ms"]) for row in turns if row.get("tool_latency_ms") is not None
    ]
    after = _storage(workspace)
    raw_tool_tokens = sum(int(row.get("tool_output_tokens") or 0) for row in tools)
    offloaded_bytes = max(0, after["artifact"] - storage_before["artifact"])
    offloaded_tokens = (offloaded_bytes + 3) // 4
    before_tokens = [int(row.get("input_tokens") or 0) for row in compression]
    after_tokens = [int(row.get("output_tokens") or 0) for row in compression]
    summary_tokens: list[int] = []
    summary_bytes: list[int] = []
    for index, row in enumerate(events):
        if row.get("event_type") != "compression":
            continue
        metadata = row.get("metadata") or {}
        # 旧版压缩 trace 未存 summary 大小；紧邻的 LLM 请求记录了实际用量。
        compression_llm = next(
            (
                previous
                for previous in reversed(events[:index])
                if previous.get("event_type") == "llm_request"
            ),
            {},
        )
        summary_tokens.append(
            int(
                metadata.get("summary_tokens")
                or compression_llm.get("output_tokens")
                or row.get("output_tokens")
                or 0
            )
        )
        summary_bytes.append(
            int(
                metadata.get("summary_bytes")
                or compression_llm.get("response_bytes")
                or row.get("response_bytes")
                or int(row.get("output_tokens") or 0) * 4
            )
        )
    duration = time.time() - started
    return {
        "duration": round(duration, 3),
        "turn_count": len(turns),
        "input_tokens_total": sum(inputs),
        "output_tokens_total": sum(outputs),
        "input_tokens_per_turn": round(sum(inputs) / max(1, len(turns)), 3),
        "max_input_tokens": max(inputs, default=0),
        "p50_input_tokens": _percentile([float(x) for x in inputs], 0.50),
        "p95_input_tokens": _percentile([float(x) for x in inputs], 0.95),
        "context_tokens_before_compaction": sum(before_tokens),
        "context_tokens_after_compaction": sum(after_tokens),
        "peak_context_tokens": max(contexts, default=0),
        "compaction_count": len(compression),
        "summary_tokens_total": sum(summary_tokens),
        "tool_call_count": len(tools),
        "tool_output_raw_tokens": raw_tool_tokens,
        "tool_output_retained_tokens": max(0, raw_tool_tokens - offloaded_tokens),
        "tool_output_offloaded_tokens": offloaded_tokens,
        "jsonl_bytes_written": max(0, after["jsonl"] - storage_before["jsonl"]),
        "artifact_bytes_written": offloaded_bytes,
        "summary_bytes_written": max(
            sum(summary_bytes), max(0, after["summary"] - storage_before["summary"])
        ),
        "memory_bytes_written": max(0, after["memory"] - storage_before["memory"]),
        "estimated_llm_request_bytes": sum(int(row.get("request_bytes") or 0) for row in llm),
        "estimated_llm_response_bytes": sum(int(row.get("response_bytes") or 0) for row in llm),
        "p50_turn_latency": _percentile(turn_latencies, 0.50),
        "p95_turn_latency": _percentile(turn_latencies, 0.95),
        "llm_request_count": len(llm),
        "failed_llm_request_count": sum(not row.get("success", False) for row in llm),
        "p50_ttft_ms": _percentile(
            [float(row.get("metadata", {}).get("ttft_ms", 0)) for row in llm], 0.50
        ),
        "p95_ttft_ms": _percentile(
            [float(row.get("metadata", {}).get("ttft_ms", 0)) for row in llm], 0.95
        ),
    }


def write_report(metrics: dict[str, Any], output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "result.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (output / "result.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("metric", "value"))
        writer.writerows(metrics.items())
    lines = ["# Continuous Session Benchmark", "", "| metric | value |", "|---|---:|"]
    lines.extend(f"| {key} | {value} |" for key, value in metrics.items())
    (output / "result.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def run_continuous(config_path: Path, output: Path, duration: float, interval: float,
                         provider_index: int = 0, *, synthetic: bool = False,
                         resume: bool = False) -> None:
    workspace = output / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "payload.txt").write_text(
        "continuous benchmark payload\n" + "x" * 120_000, encoding="utf-8"
    )
    config = ConfigLoader.load(config_path)
    provider = (
        SyntheticProvider()
        if synthetic
        else create_provider(config.providers[provider_index])
    )
    previous: dict[str, Any] = {}
    resumed_at: str | None = None
    if resume:
        result_path = output / "result.json"
        if not result_path.is_file():
            raise ValueError("续跑需要已有 result.json")
        previous = json.loads(result_path.read_text(encoding="utf-8"))
        if (
            previous.get("provider") != provider.config.name
            or previous.get("model") != provider.config.model
        ):
            raise ValueError("续跑 provider/model 与旧实验不一致")
        if float(previous.get("target_duration_seconds", 0)) != duration:
            raise ValueError("续跑目标时长与旧实验不一致")
        sessions = scan_sessions(workspace)
        if len(sessions) != 1:
            raise ValueError("续跑需要且仅需要一个可恢复会话")
        context = sessions[0].context
        if not (workspace / ".tuicodingagent" / "traces" / "events.jsonl").is_file():
            raise ValueError("续跑缺少原始 trace")
    else:
        context = SessionContext.create(workspace)
    writer = Writer(context, provider.config.model)
    runtime = ConversationRuntime(
        provider,
        workspace=workspace,
        tool_registry=create_default_registry(workspace),
        context_controller=ContextController.for_provider(workspace, provider, context),
        memory_manager=MemoryManager(workspace, workspace / "home"),
        session_writer=None if resume else writer,
    )
    if resume:
        # 恢复前先沿用原会话 ID，确保启动时的压缩请求也写入同一 trace。
        runtime.trace_session_id = context.session_id
        runtime.provider.set_context(context.session_id, runtime.agent_id)
        # 基准暂停不应在消息历史中插入普通产品会话的暂停提示。
        restored = restore_session(sessions[0], now=1)
        await runtime.restore(restored, writer)
        runtime.turn_count = int(previous.get("turn_count", 0))
        if isinstance(provider, SyntheticProvider):
            provider.calls = int(previous.get("llm_request_count", 0))
        resumed_at = datetime.now(UTC).isoformat()
    prior_duration = float(previous.get("duration", 0))
    started = time.time() - prior_duration
    original_started_at = previous.get("started_at") or datetime.fromtimestamp(
        started, UTC
    ).isoformat()
    before = {key: 0 for key in _storage(workspace)} if resume else _storage(workspace)
    deadline = started + duration
    turn = int(previous.get("turn_count", 0))
    try:
        while time.time() < deadline:
            turn += 1
            prompt = (
                "请用 read_file 读取 payload.txt，只回复第一行。"
                if turn % 5 == 0
                else f"这是连续会话第 {turn} 轮。只用一句话回复轮次编号。"
            )
            with suppress(Exception):
                _ = [event async for event in runtime.chat(prompt)]
            events = load_events(runtime.trace_recorder.path)
            metrics = aggregate(events, workspace, started, before)
            metrics.update(
                {
                    "provider": provider.config.name,
                    "model": provider.config.model,
                    "started_at": original_started_at,
                    "resumed_at": resumed_at,
                    "target_duration_seconds": duration,
                }
            )
            write_report(metrics, output)
            await asyncio.sleep(max(0.0, min(interval, deadline - time.time())))
    finally:
        await runtime.close()
        writer.close()
        events = load_events(runtime.trace_recorder.path)
        metrics = aggregate(events, workspace, started, before)
        metrics.update(
            {
                "provider": provider.config.name,
                "model": provider.config.model,
                "benchmark_type": "synthetic-deterministic" if synthetic else "live-provider",
                "started_at": original_started_at,
                    "resumed_at": resumed_at,
                "target_duration_seconds": duration,
                "run_status": "completed" if time.time() >= deadline else "interrupted",
            }
        )
        write_report(metrics, output)


def normalize_legacy_trace(output: Path, session_id: str, model: str) -> dict[str, Any]:
    """保留原始事件，另存一份补齐旧版缺失关联字段的可审计 trace。"""

    source = output / "workspace" / ".tuicodingagent" / "traces" / "events.jsonl"
    raw = source.read_bytes()
    events = load_events(source)
    corrections = {"session_id": 0, "agent_id": 0, "model": 0}
    for event in events:
        for key, value in (("session_id", session_id), ("agent_id", "lead"), ("model", model)):
            if event.get(key) != value:
                event[key] = value
                corrections[key] += 1
    destination = output / "events_normalized.jsonl"
    destination.write_text(
        "".join(
            json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
            for event in events
        ),
        encoding="utf-8",
    )
    manifest = {
        "source": str(source),
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "normalized_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        "event_count": len(events),
        "canonical_session_id": session_id,
        "corrections": corrections,
        "method": "single-session legacy trace field normalization; raw trace preserved",
    }
    (output / "trace_normalization.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def reaggregate(output: Path) -> dict[str, Any]:
    """从保留的原始 trace 重新生成报告，不再启动模型或会话。"""

    old = json.loads((output / "result.json").read_text(encoding="utf-8"))
    workspace = output / "workspace"
    trace = workspace / ".tuicodingagent" / "traces" / "events.jsonl"
    events = load_events(trace)
    before = {key: 0 for key in _storage(workspace)}
    metrics = aggregate(events, workspace, time.time() - float(old["duration"]), before)
    sessions = scan_sessions(workspace)
    if len(sessions) == 1:
        normalize_legacy_trace(output, sessions[0].context.session_id, str(old["model"]))
    metrics["duration"] = old["duration"]
    for key in (
        "provider", "model", "benchmark_type", "started_at", "resumed_at",
        "target_duration_seconds", "run_status",
    ):
        if key in old:
            metrics[key] = old[key]
    write_report(metrics, output)
    return metrics


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=28_800)
    parser.add_argument("--interval", type=float, default=60)
    parser.add_argument("--provider-index", type=int, default=0)
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--reaggregate", action="store_true")
    args = parser.parse_args()
    if args.reaggregate:
        reaggregate(args.output)
        return
    asyncio.run(
        run_continuous(
            args.config,
            args.output,
            args.duration,
            args.interval,
            args.provider_index,
            synthetic=args.synthetic,
            resume=args.resume,
        )
    )


if __name__ == "__main__":
    main()
