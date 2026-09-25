"""MCP 全量 Schema 与 lazy Schema 的可重复本地基准。"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import time
from pathlib import Path

from ..models import ToolDefinition
from ..tools.lazy import LazyToolCatalog
from ..trace import token_estimate

COUNTS = (10, 50, 100, 200)


def _definition(index: int) -> ToolDefinition:
    return ToolDefinition(
        name=f"mcp_action_{index:03d}",
        description=f"处理资源 resource_{index:03d} 的专用 MCP 操作，并返回验证结果。",
        input_schema={
            "type": "object",
            "properties": {
                "resource": {"type": "string", "description": "目标资源标识"},
                "verify": {"type": "boolean", "description": "是否验证结果"},
            },
            "required": ["resource"],
            "additionalProperties": False,
        },
    )


def _serialized(definitions: tuple[ToolDefinition, ...]) -> str:
    return json.dumps(
        [
            {
                "name": item.name,
                "description": item.description,
                "input_schema": item.input_schema,
            }
            for item in definitions
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _run_case(count: int, mode: str, repeats: int) -> dict[str, object]:
    definitions = tuple(_definition(index) for index in range(count))
    catalog = LazyToolCatalog(definitions)
    latencies: list[float] = []
    schema_tokens: list[int] = []
    prompt_tokens: list[int] = []
    correct = 0
    success = 0
    for repeat in range(repeats):
        for index, expected in enumerate(definitions):
            query = f"请处理 resource_{index:03d}，完成后验证。第 {repeat + 1} 次。"
            started = time.perf_counter()
            visible = definitions if mode == "all_schema" else catalog.select(query, limit=5)
            schema = _serialized(visible)
            selected = LazyToolCatalog(visible).select(query, limit=1)[0]
            latencies.append((time.perf_counter() - started) * 1000)
            schema_tokens.append(token_estimate(schema))
            prompt_tokens.append(token_estimate(query) + schema_tokens[-1])
            correct += int(selected.name == expected.name)
            success += int(selected.name == expected.name and expected in visible)
    total = count * repeats
    ordered = sorted(latencies)
    return {
        "tool_count": count,
        "mode": mode,
        "tasks": total,
        "schema_tokens": round(statistics.mean(schema_tokens), 3),
        "prompt_tokens": round(statistics.mean(prompt_tokens), 3),
        "ttft_ms_p50": round(statistics.median(ordered), 6),
        "ttft_ms_p95": round(ordered[max(0, int(total * 0.95) - 1)], 6),
        "tool_selection_accuracy": round(correct / total, 6),
        "task_success": round(success / total, 6),
        "execution_mode": "local-deterministic-selector",
    }


def run(output: Path, repeats: int = 5) -> list[dict[str, object]]:
    rows = [
        _run_case(count, mode, repeats)
        for count in COUNTS
        for mode in ("all_schema", "lazy_schema")
    ]
    output.mkdir(parents=True, exist_ok=True)
    (output / "result.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (output / "result.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    headers = list(rows[0])
    lines = ["# MCP Lazy Loading Benchmark", "", "| " + " | ".join(headers) + " |"]
    lines.append("|" + "---|" * len(headers))
    lines.extend("| " + " | ".join(str(row[key]) for key in headers) + " |" for row in rows)
    (output / "result.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats 必须为正数")
    run(args.output, args.repeats)


if __name__ == "__main__":
    main()
