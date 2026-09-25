"""工具故障注入与有界恢复 benchmark。"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import time
from pathlib import Path

from ..models import ToolCall, ToolDefinition, ToolResult
from ..tools.registry import ToolRegistry

FAILURES = (
    "timeout",
    "network_failure",
    "permission_denied",
    "file_not_found",
    "invalid_tool_call",
    "tool_crash",
)


class InjectedTool:
    read_only = True
    definition = ToolDefinition(
        "injected_tool",
        "故障注入测试工具",
        {"type": "object", "properties": {}, "additionalProperties": False},
    )

    def __init__(self, failure: str, timeout: float) -> None:
        self.failure = failure
        self.timeout = timeout
        self.calls = 0

    async def execute(self, call: ToolCall) -> ToolResult:
        self.calls += 1
        if self.calls > 1:
            return ToolResult(call.id, call.name, True, "recovered")
        if self.failure == "timeout":
            await asyncio.sleep(self.timeout * 2)
        elif self.failure == "network_failure":
            raise ConnectionError("injected network failure")
        elif self.failure == "permission_denied":
            raise PermissionError("injected permission denied")
        elif self.failure == "file_not_found":
            raise FileNotFoundError("injected missing file")
        elif self.failure == "tool_crash":
            raise RuntimeError("injected crash")
        return ToolResult(call.id, call.name, True, "unexpected")


async def _trial(
    failure: str, max_retry: int, max_steps: int, tool_timeout: float
) -> dict:
    tool = InjectedTool(failure, tool_timeout)
    registry = ToolRegistry((tool,), timeout=tool_timeout)
    retries = 0
    steps = 0
    final_success = False
    started = time.perf_counter()
    try:
        async with asyncio.timeout(tool_timeout * (max_steps + 2)):
            while steps < max_steps:
                steps += 1
                invalid = failure == "invalid_tool_call" and steps == 1
                call = ToolCall(
                    f"{failure}-{steps}",
                    "injected_tool",
                    {},
                    parse_error="invalid JSON" if invalid else "",
                )
                result = await registry.execute(call)
                if result.success:
                    final_success = True
                    break
                if retries >= max_retry:
                    break
                retries += 1
    except TimeoutError:
        pass
    return {
        "failure": failure,
        "recovered": final_success and retries > 0,
        "retry_count": retries,
        "final_success": final_success,
        "steps": steps,
        "infinite_loop": steps > max_steps,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
    }


async def run(output: Path, trials: int = 20) -> list[dict[str, object]]:
    details = [
        await _trial(failure, max_retry=2, max_steps=4, timeout=0.01)
        for failure in FAILURES
        for _ in range(trials)
    ]
    rows = []
    for failure in FAILURES:
        selected = [row for row in details if row["failure"] == failure]
        rows.append(
            {
                "failure": failure,
                "trials": trials,
                "recovery_rate": round(sum(row["recovered"] for row in selected) / trials, 6),
                "mean_retry": round(sum(row["retry_count"] for row in selected) / trials, 3),
                "final_success_rate": round(
                    sum(row["final_success"] for row in selected) / trials, 6
                ),
                "infinite_loop_rate": round(
                    sum(row["infinite_loop"] for row in selected) / trials, 6
                ),
                "max_retry": 2,
                "max_steps": 4,
                "timeout_ms": 10,
            }
        )
    await asyncio.to_thread(_write_reports, output, rows, details)
    return rows


def _write_reports(
    output: Path, rows: list[dict[str, object]], details: list[dict]
) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "result.json").write_text(
        json.dumps({"summary": rows, "trials": details}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with (output / "result.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    headers = list(rows[0])
    lines = ["# Tool Failure Injection", "", "| " + " | ".join(headers) + " |"]
    lines.append("|" + "---|" * len(headers))
    lines.extend("| " + " | ".join(str(row[key]) for key in headers) + " |" for row in rows)
    (output / "result.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trials", type=int, default=20)
    args = parser.parse_args()
    asyncio.run(run(args.output, args.trials))


if __name__ == "__main__":
    main()
