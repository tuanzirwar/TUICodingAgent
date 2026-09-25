"""带 setup 和基线有效性判定的 SWE-bench Live 评测层。"""

from __future__ import annotations

import csv
import json
import platform
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .adapters import EXPECTED_MODEL, AdapterResult, TUICodingAgentAdapter
from .adapters_live import CodexCLIAdapter
from .runner import _git_diff
from .swebench import SWEbenchInstance, load_instances, select_instances
from .swebench_system import SystemOptions, _git, _prompt, _repo, _safe


@dataclass(frozen=True, slots=True)
class ValidatedResult:
    """单实例结果；invalid 不进入 agent 性能分母。"""

    benchmark: str
    version: str
    split: str
    instance_id: str
    base_commit: str
    agent: str
    model: str
    status: str
    resolved: bool | None
    invalid: bool
    invalid_reason: str
    baseline_passed: bool | None
    after_passed: bool | None
    fail_to_pass_total: int
    fail_to_pass_passed: int
    pass_to_pass_total: int
    pass_to_pass_passed: int
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    total_tokens: int
    elapsed_seconds: float
    tool_calls: int
    tool_failures: int
    retries: int
    timeout: bool
    setup_elapsed_seconds: float
    setup_returncode: int | None
    error: str = ""


@dataclass(frozen=True, slots=True)
class CommandResult:
    """setup 或测试命令的可审计结果。"""

    command: str
    returncode: int | None
    passed: bool | None
    output: str
    elapsed_seconds: float
    per_test: dict[str, bool] | None = None
    timed_out: bool = False


def run_validated(
    *,
    dataset: str | Path,
    output_dir: Path,
    options: SystemOptions,
    setup_command: str,
    sample: int | None = None,
    instance_ids: set[str] | None = None,
    benchmark: str = "swebench_live",
) -> list[ValidatedResult]:
    """运行带环境准备和 baseline gate 的双代理实验。"""

    instances = select_instances(
        load_instances(dataset, default_benchmark=benchmark),
        instance_ids=instance_ids,
        sample=sample,
    )
    adapters = _adapter_map(options)
    for adapter in adapters.values():
        adapter.validate()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "experiment-manifest.json").write_text(
        json.dumps(
            {
                "created_at": datetime.now(UTC).isoformat(),
                "dataset": str(dataset),
                "model": EXPECTED_MODEL,
                "agents": list(options.agents),
                "setup_command": setup_command,
                "max_iterations": options.max_iterations,
                "timeout_seconds": options.timeout_seconds,
                "concurrency": options.concurrency,
                "instances": [asdict(item) for item in instances],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if options.dry_run:
        return []
    tasks = [(item, name, adapters[name]) for item in instances for name in options.agents]
    if options.concurrency == 1:
        results = [
            _run_one(item, name, adapter, output_dir, options, setup_command)
            for item, name, adapter in tasks
        ]
    else:
        with ThreadPoolExecutor(max_workers=options.concurrency) as pool:
            results = list(
                pool.map(lambda task: _run_one(*task, output_dir, options, setup_command), tasks)
            )
    write_validated_reports(results, output_dir)
    return results


def _adapter_map(options: SystemOptions) -> dict[str, Any]:
    if not options.agents or set(options.agents) - {"tuicodingagent", "codex"}:
        raise ValueError("代理只能是 tuicodingagent 或 codex")
    result: dict[str, Any] = {}
    if "tuicodingagent" in options.agents:
        result["tuicodingagent"] = TUICodingAgentAdapter(options.config_path, options.provider)
    if "codex" in options.agents:
        result["codex"] = CodexCLIAdapter()
    return result


def _run_one(
    instance: SWEbenchInstance,
    name: str,
    adapter: Any,
    output: Path,
    options: SystemOptions,
    setup_command: str,
) -> ValidatedResult:
    artifact = output / name / _safe(instance.instance_id)
    result_path = artifact / "result.json"
    if options.resume and result_path.is_file():
        try:
            data = json.loads(result_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("instance_id") == instance.instance_id:
                return ValidatedResult(
                    **{field: data[field] for field in ValidatedResult.__dataclass_fields__}
                )
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
            pass
    artifact.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    source: Path | None = None
    checkout: Path | None = None
    patch = ""
    setup = CommandResult(setup_command, None, None, "未执行", 0)
    baseline = CommandResult("", None, None, "未执行", 0)
    after = CommandResult("", None, None, "未执行", 0)
    adapter_result = AdapterResult("invalid", EXPECTED_MODEL, 0, error="未启动")
    invalid_reason = ""
    error = ""
    try:
        source = _repo(instance.repo, output / "_repos")
        _git(["cat-file", "-e", f"{instance.base_commit}^{{commit}}"], source)
        checkout = output / "_checkouts" / _safe(instance.instance_id) / name
        _git(["worktree", "add", "--detach", str(checkout), instance.base_commit], source)
        setup = (
            _run_command(setup_command, checkout, options.timeout_seconds)
            if setup_command
            else CommandResult("", 0, True, "未配置 setup command", 0)
        )
        if setup.passed is not True:
            invalid_reason = "setup_failed"
        else:
            baseline = _run_tests(checkout, instance, options.timeout_seconds)
            invalid_reason = _baseline_reason(instance, baseline)
        if not invalid_reason:
            prompt = _prompt(instance, options)
            if name == "tuicodingagent":
                adapter_result = adapter.run(
                    workspace=checkout,
                    prompt=prompt,
                    output_dir=artifact,
                    max_iterations=options.max_iterations,
                    timeout_seconds=options.timeout_seconds,
                )
            else:
                adapter_result = adapter.run(
                    workspace=checkout, prompt=prompt, timeout_seconds=options.timeout_seconds
                )
            after = _run_tests(checkout, instance, options.timeout_seconds)
            patch = _git_diff(checkout)
            error = adapter_result.error
    except (OSError, RuntimeError, ValueError) as exc:
        invalid_reason = "checkout_or_source_failed"
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if checkout is not None and source is not None:
            _remove_worktree(source, checkout)
    invalid = bool(invalid_reason)
    if invalid:
        status = "invalid"
        resolved = None
        adapter_result = AdapterResult(
            "invalid", EXPECTED_MODEL, adapter_result.elapsed_seconds, error=error or invalid_reason
        )
    else:
        status = adapter_result.status
        resolved = _resolved(adapter_result, instance, after)
    result = ValidatedResult(
        benchmark=instance.benchmark,
        version=instance.version,
        split=instance.split,
        instance_id=instance.instance_id,
        base_commit=instance.base_commit,
        agent=name,
        model=adapter_result.model,
        status=status,
        resolved=resolved,
        invalid=invalid,
        invalid_reason=invalid_reason,
        baseline_passed=baseline.passed,
        after_passed=after.passed,
        fail_to_pass_total=len(instance.fail_to_pass),
        fail_to_pass_passed=_passed(instance.fail_to_pass, after.per_test),
        pass_to_pass_total=len(instance.pass_to_pass),
        pass_to_pass_passed=_passed(instance.pass_to_pass, after.per_test),
        input_tokens=adapter_result.input_tokens,
        output_tokens=adapter_result.output_tokens,
        cached_input_tokens=adapter_result.cached_input_tokens,
        total_tokens=adapter_result.input_tokens + adapter_result.output_tokens,
        elapsed_seconds=round(max(time.monotonic() - started, adapter_result.elapsed_seconds), 3),
        tool_calls=adapter_result.tool_calls,
        tool_failures=adapter_result.tool_failures,
        retries=adapter_result.retries,
        timeout=adapter_result.status == "timeout",
        setup_elapsed_seconds=setup.elapsed_seconds,
        setup_returncode=setup.returncode,
        error=error,
    )
    _write_artifacts(artifact, instance, setup, baseline, after, adapter_result, patch, result)
    return result


def _run_tests(workspace: Path, instance: SWEbenchInstance, timeout: float) -> CommandResult:
    tests = (*instance.fail_to_pass, *instance.pass_to_pass)
    if not tests:
        return CommandResult("", None, None, "缺少 FAIL_TO_PASS/PASS_TO_PASS", 0)
    started = time.monotonic()
    values: dict[str, bool] = {}
    outputs: list[str] = []
    timed_out = False
    for test in tests:
        item = _run_command(f"pytest -q {test}", workspace, timeout)
        values[test] = item.passed is True
        outputs.append(f"$ {item.command}\n{item.output}")
        timed_out = timed_out or item.timed_out
    return CommandResult(
        "pytest -q <per-test>",
        0 if all(values.values()) else 1,
        all(values.values()),
        "\n".join(outputs),
        time.monotonic() - started,
        values,
        timed_out,
    )


def _baseline_reason(instance: SWEbenchInstance, baseline: CommandResult) -> str:
    if baseline.per_test is None:
        return "baseline_missing_tests"
    wrong_fail = [test for test in instance.fail_to_pass if baseline.per_test.get(test) is True]
    wrong_pass = [test for test in instance.pass_to_pass if baseline.per_test.get(test) is not True]
    if wrong_fail or wrong_pass:
        return "baseline_expected_state_mismatch:" + json.dumps(
            {"f2p_should_fail_but_passed": wrong_fail, "p2p_should_pass_but_failed": wrong_pass},
            ensure_ascii=False,
        )
    return ""


def _run_command(command: str, workspace: Path, timeout: float) -> CommandResult:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=workspace,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            command,
            None,
            False,
            _text(exc.stdout) + _text(exc.stderr),
            time.monotonic() - started,
            timed_out=True,
        )
    return CommandResult(
        command,
        completed.returncode,
        completed.returncode == 0,
        completed.stdout + completed.stderr,
        time.monotonic() - started,
    )


def _resolved(
    adapter: AdapterResult, instance: SWEbenchInstance, after: CommandResult
) -> bool | None:
    if adapter.status != "completed" or after.per_test is None:
        return None
    return all(
        after.per_test.get(test, False) for test in (*instance.fail_to_pass, *instance.pass_to_pass)
    )


def _passed(tests: tuple[str, ...], values: dict[str, bool] | None) -> int:
    return sum(values.get(test, False) for test in tests) if values else 0


def _write_artifacts(
    artifact: Path,
    instance: SWEbenchInstance,
    setup: CommandResult,
    baseline: CommandResult,
    after: CommandResult,
    adapter: AdapterResult,
    patch: str,
    result: ValidatedResult,
) -> None:
    (artifact / "manifest.json").write_text(
        json.dumps(
            {
                **instance.as_manifest(),
                "agent": result.agent,
                "model": result.model,
                "setup_command": setup.command,
                "platform": platform.platform(),
                "python": platform.python_version(),
                "created_at": datetime.now(UTC).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    events = adapter.events or ({"type": "adapter_summary", **asdict(adapter)},)
    (artifact / "trajectory.jsonl").write_text(
        "".join(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )
    (artifact / "stdout.log").write_text(adapter.stdout, encoding="utf-8")
    (artifact / "stderr.log").write_text(adapter.stderr, encoding="utf-8")
    (artifact / "setup.json").write_text(
        json.dumps(asdict(setup), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (artifact / "baseline-test-output.txt").write_text(baseline.output, encoding="utf-8")
    (artifact / "test-output.txt").write_text(after.output, encoding="utf-8")
    (artifact / "final.patch").write_text(patch, encoding="utf-8")
    (artifact / "result.json").write_text(
        json.dumps(asdict(result), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def write_validated_reports(results: list[ValidatedResult], output: Path) -> None:
    rows = [asdict(item) for item in results]
    (output / "summary.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if rows:
        with (output / "summary.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    (output / "report.md").write_text(_report(results), encoding="utf-8")


def _report(results: list[ValidatedResult]) -> str:
    agents = sorted({item.agent for item in results})
    lines = [
        "# SWE-bench Live 双代理评测报告",
        "",
        "invalid 实例从 Resolved/F2P/P2P、timeout 等性能分母排除；资源消耗仍保留。",
        "",
        "## 五项对比表",
        "",
        "| 指标 | " + " | ".join(agents) + " |",
        "|---|" + "---|" * len(agents),
    ]
    for label, fn in (
        ("Resolved Rate / Pass@1", lambda a: _metric(results, a, "resolved")),
        (
            "F2P Rate",
            lambda a: _test_metric(results, a, "fail_to_pass_passed", "fail_to_pass_total"),
        ),
        (
            "P2P Rate",
            lambda a: _test_metric(results, a, "pass_to_pass_passed", "pass_to_pass_total"),
        ),
        ("Token / Resolved", lambda a: _per_resolved(results, a, "total_tokens")),
        ("Time / Resolved", lambda a: _per_resolved(results, a, "elapsed_seconds", "s")),
    ):
        lines.append(f"| {label} | " + " | ".join(fn(agent) for agent in agents) + " |")
    lines += [
        "",
        "## 有效性和详细统计",
        "",
        "- invalid_count 不进入上述性能分母；每个 invalid 实例的 invalid_reason 写在 result.json。",
        "- 缺失 usage 按 0；resolved=0 时 Token/Resolved、Time/Resolved 为 `N/A`。",
        "",
    ]
    for agent in agents:
        all_rows = [item for item in results if item.agent == agent]
        valid = [item for item in all_rows if not item.invalid]
        invalid = [item for item in all_rows if item.invalid]
        lines.append(
            f"- `{agent}`：valid {len(valid)}，invalid_count {len(invalid)}，原因："
            + "; ".join(sorted({item.invalid_reason for item in invalid}))
            + "。"
        )
    return "\n".join(lines) + "\n"


def _metric(results: list[ValidatedResult], agent: str, field: str) -> str:
    rows = [item for item in results if item.agent == agent and not item.invalid]
    return _ratio(sum(getattr(item, field) is True for item in rows), len(rows))


def _test_metric(results: list[ValidatedResult], agent: str, num: str, den: str) -> str:
    rows = [item for item in results if item.agent == agent and not item.invalid]
    return _ratio(
        sum(getattr(item, num) for item in rows), sum(getattr(item, den) for item in rows)
    )


def _per_resolved(results: list[ValidatedResult], agent: str, field: str, suffix: str = "") -> str:
    rows = [
        item
        for item in results
        if item.agent == agent and not item.invalid and item.resolved is True
    ]
    return (
        "N/A"
        if not rows
        else f"{sum(getattr(item, field) for item in rows) / len(rows):.1f}{suffix}"
    )


def _ratio(num: int, den: int) -> str:
    return "N/A" if den == 0 else f"{num / den:.1%} ({num}/{den})"


def _remove_worktree(repo: Path, checkout: Path) -> None:
    if checkout.exists():
        result = _git(["worktree", "remove", "--force", str(checkout)], repo, check=False)
        if result.returncode != 0:
            shutil.rmtree(checkout, ignore_errors=True)


def _text(value: str | bytes | None) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else (value or "")


__all__ = ["ValidatedResult", "CommandResult", "run_validated", "write_validated_reports"]
