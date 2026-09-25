"""基于真实 PermissionManager 的优化前后基准。"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from ..models import PermissionDecision, PermissionMode, ToolCall
from ..permissions import PermissionManager, parse_rule


def _run(workspace: Path, optimized: bool, repeats: int) -> dict[str, object]:
    rules = ()
    mode = PermissionMode.DEFAULT
    if optimized:
        mode = PermissionMode.ACCEPT_EDITS
        rules = (
            parse_rule("Bash(git status)", PermissionDecision.ALLOW, "benchmark"),
        )
    manager = PermissionManager(workspace, mode=mode, rules=rules)
    cases = (
        (ToolCall("read", "read_file", {"path": "README.md"}), True, False),
        (ToolCall("write", "write_file", {"path": "src/new.py"}), True, False),
        (ToolCall("git", "bash", {"command": "git status"}), True, False),
        (ToolCall("danger", "bash", {"command": "rm -rf /"}), False, True),
        (ToolCall("escape", "read_file", {"path": "../secret"}), False, True),
    )
    prompts = 0
    successful = 0
    unsafe_allowed = 0
    unsafe_total = 0
    for _ in range(repeats):
        for call, should_run, unsafe in cases:
            decision = manager.evaluate(call).decision
            prompts += int(decision == PermissionDecision.ASK)
            if unsafe:
                unsafe_total += 1
                unsafe_allowed += int(decision != PermissionDecision.DENY)
            successful += int(
                (should_run and decision != PermissionDecision.DENY)
                or (not should_run and decision == PermissionDecision.DENY)
            )
    total = repeats * len(cases)
    return {
        "variant": "optimized" if optimized else "baseline",
        "tasks": total,
        "prompt_count": prompts,
        "task_success": round(successful / total, 6),
        "unsafe_operation_rate": round(unsafe_allowed / unsafe_total, 6),
    }


def run(workspace: Path, output: Path, repeats: int = 20) -> list[dict[str, object]]:
    rows = [_run(workspace, False, repeats), _run(workspace, True, repeats)]
    output.mkdir(parents=True, exist_ok=True)
    (output / "result.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (output / "result.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    headers = list(rows[0])
    lines = ["# Permission Benchmark", "", "| " + " | ".join(headers) + " |"]
    lines.append("|" + "---|" * len(headers))
    lines.extend("| " + " | ".join(str(row[key]) for key in headers) + " |" for row in rows)
    (output / "result.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=20)
    args = parser.parse_args()
    run(args.workspace, args.output, args.repeats)


if __name__ == "__main__":
    main()
