"""统一运行轨迹与 benchmark 报告导出。"""

from __future__ import annotations

import csv
import json
import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import ProviderEventType
from .redaction import redact

TRACE_FIELDS = (
    "event_type",
    "request_id",
    "session_id",
    "agent_id",
    "model",
    "input_tokens",
    "output_tokens",
    "context_tokens",
    "request_bytes",
    "response_bytes",
    "tool_name",
    "tool_latency_ms",
    "tool_output_bytes",
    "tool_output_tokens",
    "success",
    "error_type",
    "retry_count",
    "start_time",
    "end_time",
    "metadata",
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def token_estimate(value: str | bytes) -> int:
    data = value if isinstance(value, bytes) else value.encode("utf-8")
    return (len(data) + 3) // 4


@dataclass(slots=True)
class TraceEvent:
    event_type: str
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    session_id: str = ""
    agent_id: str = "lead"
    model: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    context_tokens: int | None = None
    request_bytes: int = 0
    response_bytes: int = 0
    tool_name: str = ""
    tool_latency_ms: float | None = None
    tool_output_bytes: int = 0
    tool_output_tokens: int = 0
    success: bool = True
    error_type: str = ""
    retry_count: int = 0
    start_time: str = field(default_factory=utc_now)
    end_time: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def finish(self, *, success: bool = True, error: BaseException | None = None) -> TraceEvent:
        self.success = success
        self.error_type = type(error).__name__ if error is not None else self.error_type
        self.end_time = utc_now()
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TraceRecorder:
    """线程安全的追加式 JSONL recorder。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    @classmethod
    def for_workspace(cls, workspace: Path) -> TraceRecorder:
        return cls(workspace / ".tuicodingagent" / "traces" / "events.jsonl")

    def record(self, event: TraceEvent) -> None:
        if not event.end_time:
            event.end_time = utc_now()
        line = json.dumps(
            redact(event.to_dict(), personal_data=True), ensure_ascii=False, separators=(",", ":")
        )
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line + "\n")


class TracingProvider:
    """透明包装 Provider，覆盖主对话、压缩和记忆更新请求。"""

    def __init__(self, provider: Any, recorder: TraceRecorder) -> None:
        self._provider = provider
        self.config = provider.config
        self.recorder = recorder
        self.session_id = ""
        self.agent_id = "lead"

    def set_context(self, session_id: str, agent_id: str = "lead") -> None:
        self.session_id = session_id
        self.agent_id = agent_id

    async def stream(self, system_prompt: Any, messages: Any, tools: Any = ()):
        request = repr((system_prompt, messages, tools)).encode("utf-8")
        event = TraceEvent(
            "llm_request",
            session_id=self.session_id,
            agent_id=self.agent_id,
            model=self.config.model,
            context_tokens=token_estimate(request),
            request_bytes=len(request),
        )
        started = time.perf_counter()
        first_event = True
        response_bytes = 0
        try:
            async for item in self._provider.stream(system_prompt, messages, tools):
                if first_event:
                    event.metadata["ttft_ms"] = round((time.perf_counter() - started) * 1000, 3)
                    first_event = False
                if item.type == ProviderEventType.TEXT_DELTA:
                    response_bytes += len(item.text.encode("utf-8"))
                elif item.type == ProviderEventType.TOOL_CALL and item.tool_call is not None:
                    response_bytes += len(repr(item.tool_call).encode("utf-8"))
                elif item.type == ProviderEventType.USAGE:
                    event.input_tokens = item.input_tokens
                    event.output_tokens = item.output_tokens
                yield item
        except BaseException as exc:
            event.response_bytes = response_bytes
            event.metadata["total_latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
            self.recorder.record(event.finish(success=False, error=exc))
            raise
        event.response_bytes = response_bytes
        event.metadata["total_latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
        self.recorder.record(event.finish())

    async def close(self) -> None:
        await self._provider.close()


def load_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def export_events(events: Iterable[dict[str, Any]], output: Path) -> tuple[Path, Path, Path]:
    rows = [redact(event, personal_data=True) for event in events]
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / "benchmark.json"
    csv_path = output / "benchmark.csv"
    markdown_path = output / "benchmark.md"
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=TRACE_FIELDS)
        writer.writeheader()
        for row in rows:
            item = {name: row.get(name, "") for name in TRACE_FIELDS}
            item["metadata"] = json.dumps(item["metadata"], ensure_ascii=False)
            writer.writerow(item)
    lines = [
        "# Benchmark Trace",
        "",
        "| event | success | latency_ms | input | output | tool |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row.get('event_type', '')} | {row.get('success', '')} | "
            f"{row.get('tool_latency_ms', '')} | {row.get('input_tokens', '')} | "
            f"{row.get('output_tokens', '')} | {row.get('tool_name', '')} |"
        )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, csv_path, markdown_path


__all__ = [
    "TRACE_FIELDS",
    "TraceEvent",
    "TraceRecorder",
    "TracingProvider",
    "export_events",
    "load_events",
    "token_estimate",
    "utc_now",
]
