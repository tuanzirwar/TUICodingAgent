"""真实模型 MCP 全量与按需 Schema 选择对照。"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import time
from pathlib import Path

from ..config import ConfigLoader
from ..models import ChatMessage, ProviderEventType, SystemPrompt
from ..providers import create_provider
from ..tools.lazy import LazyToolCatalog
from ..trace import token_estimate
from .mcp_lazy_benchmark import COUNTS, _definition, _serialized


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, int(len(ordered) * fraction) - 1)], 3)


def _write(rows: list[dict], output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "result.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if not rows:
        return
    fields = list(rows[0])
    with (output / "result.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    lines = ["# Live MCP Schema Benchmark", "", "| " + " | ".join(fields) + " |"]
    lines.append("|" + "---|" * len(fields))
    lines.extend("| " + " | ".join(str(row[key]) for key in fields) + " |" for row in rows)
    (output / "result.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def run(
    config: Path, output: Path, provider_index: int = 0, *, max_requests: int
) -> list[dict]:
    if max_requests < 1:
        raise ValueError("max_requests 必须为正数")
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=True)
    results_path = output / "result.json"
    usage_path = output / "api_usage.json"
    rows: list[dict] = (
        json.loads(await asyncio.to_thread(results_path.read_text, encoding="utf-8"))
        if results_path.is_file()
        else []
    )
    usage = (
        json.loads(await asyncio.to_thread(usage_path.read_text, encoding="utf-8"))
        if usage_path.is_file()
        else {"attempted": len(rows), "completed": len(rows), "uncertain_inflight": 0}
    )
    attempted = max(int(usage["attempted"]), len(rows))
    completed_cases = {(row["tool_count"], row["mode"], row["target"]) for row in rows}
    selected = ConfigLoader.load(config).providers[provider_index]
    provider = create_provider(selected)
    try:
        for count in COUNTS:
            definitions = tuple(_definition(index) for index in range(count))
            catalog = LazyToolCatalog(definitions)
            for mode in ("all_schema", "lazy_schema"):
                for target in (0, count // 2, count - 1):
                    if (count, mode, target) in completed_cases:
                        continue
                    if attempted >= max_requests:
                        return rows
                    # 请求发出前先记账，崩溃后也不会意外超出调用上限。
                    attempted += 1
                    usage["attempted"] = attempted
                    usage["max_requests"] = max_requests
                    await asyncio.to_thread(
                        usage_path.write_text, json.dumps(usage, indent=2), encoding="utf-8"
                    )
                    query = f"Process resource_{target:03d} and verify the result."
                    started = time.perf_counter()
                    visible = (
                        definitions
                        if mode == "all_schema"
                        else catalog.select(query, limit=5)
                    )
                    first_ms = None
                    calls = []
                    input_tokens = 0
                    output_tokens = 0
                    error = ""
                    try:
                        async for event in provider.stream(
                            SystemPrompt(
                                "Call exactly one tool for the requested resource. "
                                "Set resource to the requested identifier and verify to true."
                            ),
                            (ChatMessage("user", query),),
                            visible,
                        ):
                            if first_ms is None:
                                first_ms = (time.perf_counter() - started) * 1000
                            if event.type == ProviderEventType.TOOL_CALL and event.tool_call:
                                calls.append(event.tool_call)
                            if event.type == ProviderEventType.USAGE:
                                input_tokens += event.input_tokens
                                output_tokens += event.output_tokens
                    except Exception as exc:
                        error = type(exc).__name__ + ": " + str(exc)[:160]
                    total_ms = (time.perf_counter() - started) * 1000
                    expected_name = f"mcp_action_{target:03d}"
                    expected_resource = f"resource_{target:03d}"
                    correct = len(calls) == 1 and calls[0].name == expected_name
                    valid = (
                        correct
                        and calls[0].arguments.get("resource") == expected_resource
                        and calls[0].arguments.get("verify") is True
                    )
                    rows.append(
                        {
                            "tool_count": count,
                            "mode": mode,
                            "target": target,
                            "provider": selected.name,
                            "model": selected.model,
                            "visible_schema_count": len(visible),
                            "schema_tokens_estimated": token_estimate(_serialized(visible)),
                            "prompt_tokens_estimated": token_estimate(query)
                            + token_estimate(_serialized(visible)),
                            "input_tokens_reported": input_tokens,
                            "output_tokens_reported": output_tokens,
                            "ttft_ms": round(first_ms, 3) if first_ms is not None else None,
                            "total_ms": round(total_ms, 3),
                            "selected_tool": calls[0].name if len(calls) == 1 else "",
                            "selection_correct": correct,
                            "valid_tool_call": valid,
                            "error": error,
                        }
                    )
                    _write(rows, output)
                    completed_cases.add((count, mode, target))
                    usage["completed"] = len(rows)
                    await asyncio.to_thread(
                        usage_path.write_text, json.dumps(usage, indent=2), encoding="utf-8"
                    )
    finally:
        await provider.close()
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provider-index", type=int, default=0)
    parser.add_argument("--max-requests", type=int, required=True)
    args = parser.parse_args()
    asyncio.run(
        run(args.config, args.output, args.provider_index, max_requests=args.max_requests)
    )


if __name__ == "__main__":
    main()
