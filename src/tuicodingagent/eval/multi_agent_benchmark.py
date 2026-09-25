"""1/2/4 Agent 调度、冲突与合并失败的本地基准。"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import statistics
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class WorkItem:
    index: int
    target: str
    duration: float
    tokens: int


async def _trial(agent_count: int) -> dict[str, object]:
    queue: asyncio.Queue[WorkItem] = asyncio.Queue()
    items = [
        WorkItem(index, f"module_{index % 8}.py", 0.002 + (index % 5) * 0.001, 120 + index)
        for index in range(32)
    ]
    for item in items:
        queue.put_nowait(item)
    active: dict[str, int] = {}
    active_lock = asyncio.Lock()
    conflicts = 0
    merge_failures = 0
    completed = 0

    async def worker() -> None:
        nonlocal conflicts, merge_failures, completed
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            async with active_lock:
                if active.get(item.target, 0):
                    conflicts += 1
                    merge_failures += 1
                active[item.target] = active.get(item.target, 0) + 1
            await asyncio.sleep(item.duration)
            async with active_lock:
                active[item.target] -= 1
            completed += 1
            queue.task_done()

    started = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(agent_count)))
    elapsed = time.perf_counter() - started
    return {
        "agents": agent_count,
        "wall_time_ms": round(elapsed * 1000, 3),
        "token_cost": sum(item.tokens for item in items) + agent_count * 40,
        "success_rate": round((completed - merge_failures) / len(items), 6),
        "conflict_count": conflicts,
        "merge_failure": merge_failures,
        "tool_calls": len(items) * 2,
        "tasks": len(items),
        "execution_mode": "local-async-scheduler",
    }


async def run(output: Path, repeats: int = 10) -> list[dict[str, object]]:
    rows = []
    for count in (1, 2, 4):
        trials = [await _trial(count) for _ in range(repeats)]
        rows.append(
            {
                "agents": count,
                "wall_time_ms": round(
                    statistics.mean(float(row["wall_time_ms"]) for row in trials), 3
                ),
                "token_cost": int(statistics.mean(int(row["token_cost"]) for row in trials)),
                "success_rate": round(
                    statistics.mean(float(row["success_rate"]) for row in trials), 6
                ),
                "conflict_count": int(
                    statistics.mean(int(row["conflict_count"]) for row in trials)
                ),
                "merge_failure": int(
                    statistics.mean(int(row["merge_failure"]) for row in trials)
                ),
                "tool_calls": int(statistics.mean(int(row["tool_calls"]) for row in trials)),
                "tasks": 32,
                "execution_mode": "local-async-scheduler",
            }
        )
    await asyncio.to_thread(_write, output, rows)
    return rows


def _write(output: Path, rows: list[dict[str, object]]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "result.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (output / "result.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    headers = list(rows[0])
    lines = ["# Multi Agent Benchmark", "", "| " + " | ".join(headers) + " |"]
    lines.append("|" + "---|" * len(headers))
    lines.extend("| " + " | ".join(str(row[key]) for key in headers) + " |" for row in rows)
    (output / "result.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=10)
    args = parser.parse_args()
    asyncio.run(run(args.output, args.repeats))


if __name__ == "__main__":
    main()
