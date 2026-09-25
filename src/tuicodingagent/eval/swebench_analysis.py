"""汇总官方评分与 Agent 轨迹，生成逐题失败归因。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class FailureAnalysis:
    """单个 Agent 在单个实例上的可审计归因。"""

    instance_id: str
    agent: str
    resolved: bool | None
    reason: str
    fail_to_pass_failures: tuple[str, ...]
    pass_to_pass_failures: tuple[str, ...]
    suspicious_trajectory: str


def analyze_run(
    run_dir: Path, agents: tuple[str, ...] = ("tuicodingagent", "codex")
) -> list[FailureAnalysis]:
    """读取本地运行产物和官方 report.json，写出 failures.json/Markdown。"""

    rows: list[FailureAnalysis] = []
    for agent in agents:
        local_root = run_dir / agent
        if not local_root.is_dir():
            continue
        for artifact in sorted(path for path in local_root.iterdir() if path.is_dir()):
            result = _read_json(artifact / "result.json")
            instance_id = str(result.get("instance_id", artifact.name))
            report = _read_json(run_dir / f"eval-{agent}" / instance_id / "report.json")
            f2p = _failures(report, "FAIL_TO_PASS")
            p2p = _failures(report, "PASS_TO_PASS")
            resolved = report.get("resolved") if isinstance(report.get("resolved"), bool) else None
            patch = (
                (artifact / "final.patch").read_text(encoding="utf-8")
                if (artifact / "final.patch").is_file()
                else ""
            )
            reason = _reason(result, report, patch, f2p, p2p)
            rows.append(
                FailureAnalysis(
                    instance_id=instance_id,
                    agent=agent,
                    resolved=resolved,
                    reason=reason,
                    fail_to_pass_failures=tuple(f2p),
                    pass_to_pass_failures=tuple(p2p),
                    suspicious_trajectory=_trajectory_hint(artifact / "trajectory.jsonl"),
                )
            )
    (run_dir / "failure-analysis.json").write_text(
        json.dumps([asdict(item) for item in rows], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (run_dir / "failure-analysis.md").write_text(_markdown(rows), encoding="utf-8")
    return rows


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _failures(report: dict[str, Any], group: str) -> list[str]:
    value = report.get(group)
    if not isinstance(value, dict):
        return []
    failures = value.get("failure", [])
    return [str(item) for item in failures] if isinstance(failures, list) else []


def _reason(
    result: dict[str, Any], report: dict[str, Any], patch: str, f2p: list[str], p2p: list[str]
) -> str:
    if not patch.strip():
        return "空补丁：Agent 未留下可评分的生产代码改动"
    status = str(result.get("status", ""))
    if status == "timeout":
        return "Agent 超时"
    if status and status != "completed":
        return f"Agent 启动或执行失败：{result.get('error', status)}"
    if not report:
        return "官方 harness 未生成 report.json"
    if f2p and p2p:
        return "目标测试未通过，且引入回归"
    if f2p:
        return "目标测试未通过（未修对）"
    if p2p:
        return "原有测试回归"
    if report.get("resolved") is False:
        return "官方 harness 未解析成功，但未提供逐测试失败列表"
    return "通过"


def _trajectory_hint(path: Path) -> str:
    if not path.is_file():
        return "无轨迹文件"
    failures: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        tool_result = event.get("tool_result")
        if isinstance(tool_result, dict) and tool_result.get("success") is False:
            failures.append(
                str(tool_result.get("error") or tool_result.get("output") or "工具失败")
            )
        item = event.get("item")
        if isinstance(item, dict) and item.get("status") in {"failed", "error"}:
            failures.append(str(item.get("error") or item.get("output") or "Codex 工具失败"))
    return failures[-1][:500] if failures else "未发现工具失败"


def _markdown(rows: list[FailureAnalysis]) -> str:
    lines = [
        "# 逐题失败分析",
        "",
        "| 题号 | Agent | resolved | 失败原因 | 轨迹可疑点 |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        trajectory = row.suspicious_trajectory.replace("|", "/")
        lines.append(
            f"| {row.instance_id} | {row.agent} | {row.resolved} | {row.reason} | {trajectory} |"
        )
    return "\n".join(lines) + "\n"


__all__ = ["FailureAnalysis", "analyze_run"]
