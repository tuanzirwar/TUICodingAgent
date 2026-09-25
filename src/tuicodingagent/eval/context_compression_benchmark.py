"""三种上下文压缩策略的确定性事实保留基准。"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import time
from pathlib import Path

from ..trace import token_estimate

FACT = re.compile(r"FACT-(\d{3})=([^\s]+)")


def _dataset(turns: int = 240) -> tuple[str, dict[str, str]]:
    facts: dict[str, str] = {}
    messages = []
    for index in range(turns):
        key = f"{index:03d}"
        value = f"value_{index:03d}"
        facts[key] = value
        filler = "诊断过程包含日志、调用链和候选假设。" * 12
        messages.append(
            f"用户第 {index} 轮：FACT-{key}={value}；文件 src/module_{index:03d}.py。{filler}"
        )
        messages.append(f"助手第 {index} 轮：已记录 FACT-{key}，继续验证。{filler}")
    return "\n".join(messages), facts


def _raw_truncate(text: str, budget: int) -> str:
    return text[-budget:]


def _old_summary(text: str, budget: int) -> str:
    lines = []
    for line in text.splitlines():
        head = line.split("。", 1)[0]
        lines.append(head)
        if len("\n".join(lines)) >= budget:
            break
    return "\n".join(lines)[:budget]


def _structured_summary(text: str, budget: int) -> str:
    facts = [f"FACT-{key}={value}" for key, value in FACT.findall(text)]
    files = re.findall(r"src/module_\d{3}\.py", text)
    sections = [
        "1. 主要请求和意图\n持续诊断工程会话",
        "2. 关键事实\n" + "\n".join(dict.fromkeys(facts)),
        "3. 文件和代码段\n" + "\n".join(dict.fromkeys(files)),
        "4. 错误和修复\n暂无",
        "5. 问题解决过程\n按轮次验证",
        "6. 用户消息原文\n已由事实索引替代",
        "7. 待办任务\n继续验证",
        "8. 当前工作\n检查事实保留",
        "9. 可能的下一步\n恢复所需文件",
    ]
    return "\n".join(sections)[:budget]


def _evaluate(name: str, text: str, facts: dict[str, str], budget: int) -> dict[str, object]:
    compressor = {
        "raw_truncate": _raw_truncate,
        "old_summary": _old_summary,
        "new_structured_summary": _structured_summary,
    }[name]
    latencies = []
    outputs = []
    for _ in range(30):
        started = time.perf_counter()
        outputs.append(compressor(text, budget))
        latencies.append((time.perf_counter() - started) * 1000)
    output = outputs[-1]
    recalled = sum(f"FACT-{key}={value}" in output for key, value in facts.items())
    input_tokens = token_estimate(text)
    output_tokens = token_estimate(output)
    ordered = sorted(latencies)
    return {
        "strategy": name,
        "facts": len(facts),
        "fact_recall": round(recalled / len(facts), 6),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "token_reduction": round(1 - output_tokens / input_tokens, 6),
        "compression_cost_tokens": input_tokens + output_tokens,
        "latency_ms_p50": round(statistics.median(ordered), 6),
        "latency_ms_p95": round(ordered[27], 6),
        "execution_mode": "local-deterministic",
    }


def run(output: Path, budget: int = 12_000) -> list[dict[str, object]]:
    text, facts = _dataset()
    rows = [
        _evaluate(name, text, facts, budget)
        for name in ("raw_truncate", "old_summary", "new_structured_summary")
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
    lines = ["# Context Compression Benchmark", "", "| " + " | ".join(headers) + " |"]
    lines.append("|" + "---|" * len(headers))
    lines.extend("| " + " | ".join(str(row[key]) for key in headers) + " |" for row in rows)
    (output / "result.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget-chars", type=int, default=12_000)
    args = parser.parse_args()
    run(args.output, args.budget_chars)


if __name__ == "__main__":
    main()
