"""SWE-bench Live 双代理、隔离、可恢复评测 harness。"""

from __future__ import annotations

import csv
import json
import platform
import shutil
import subprocess
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .adapters import (
    EXPECTED_MODEL,
    AdapterResult,
    CodexCLIAdapter,
    TUICodingAgentAdapter,
)
from .runner import _git_diff
from .swebench import SWEbenchInstance, load_instances, select_instances


@dataclass(frozen=True, slots=True)
class TestSummary:
    """一次基线或修复后测试的结果。"""

    command: str
    returncode: int | None
    passed: bool | None
    timed_out: bool = False
    per_test: dict[str, bool] | None = None
    output: str = ""


@dataclass(frozen=True, slots=True)
class InstanceAgentResult:
    """单实例、单代理的机器可解析结果。"""

    benchmark: str
    version: str
    split: str
    instance_id: str
    agent: str
    model: str
    status: str
    resolved: bool | None
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
    error: str = ""

    @classmethod
    def from_json(cls, path: Path) -> InstanceAgentResult:
        """读取并校验 resume 所需的完整结果。"""

        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("instance_id") is None:
            raise ValueError("result.json 不是完整实例结果")
        return cls(**{field: value[field] for field in cls.__dataclass_fields__})


@dataclass(frozen=True, slots=True)
class ExperimentOptions:
    """双代理公平约束和运行参数。"""

    config_path: Path
    tuicodingagent_provider: str = "CloudVisit"
    agents: tuple[str, ...] = ("tuicodingagent", "codex")
    max_iterations: int = 12
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    timeout_seconds: float = 1800.0
    test_command: str = ""
    concurrency: int = 1
    resume: bool = False
    dry_run: bool = False


def run_experiment(
    *,
    dataset: str | Path,
    output_dir: Path,
    options: ExperimentOptions,
    sample: int | None = None,
    instance_ids: set[str] | None = None,
) -> list[InstanceAgentResult]:
    """运行或规划一组实例；默认串行，resume 只接受完整结果。"""

    if options.concurrency <= 0:
        raise ValueError("并发度必须大于 0")
    if options.max_iterations <= 0 or options.timeout_seconds <= 0:
        raise ValueError("轮次和超时必须大于 0")
    instances = select_instances(load_instances(dataset), instance_ids=instance_ids, sample=sample)
    adapters = _build_adapters(options)
    for adapter in adapters.values():
        adapter.validate()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    plan = _plan(instances, adapters, output_dir, options)
    (output_dir / "experiment-manifest.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if options.dry_run:
        return []

    tasks = [
        (instance, agent_name, adapters[agent_name])
        for instance in instances
        for agent_name in options.agents
    ]
    if options.concurrency == 1:
        results = [
            _run_one(instance, agent, adapter, output_dir, options)
            for instance, agent, adapter in tasks
        ]
    else:
        with ThreadPoolExecutor(max_workers=options.concurrency) as executor:
            results = list(
                executor.map(
                    lambda task: _run_one(*task, output_dir, options),
                    tasks,
                )
            )
    write_reports(results, output_dir)
    return results


def _build_adapters(options: ExperimentOptions) -> dict[str, Any]:
    available: dict[str, Any] = {}
    if "tuicodingagent" in options.agents:
        available["tuicodingagent"] = TUICodingAgentAdapter(
            options.config_path, options.tuicodingagent_provider
        )
    if "codex" in options.agents:
        available["codex"] = CodexCLIAdapter()
    if set(options.agents) - set(available):
        raise ValueError("仅支持代理：tuicodingagent、codex")
    if not available:
        raise ValueError("至少选择一个代理")
    return available


def _plan(
    instances: list[SWEbenchInstance],
    adapters: dict[str, Any],
    output_dir: Path,
    options: ExperimentOptions,
) -> dict[str, object]:
    return {
        "created_at": datetime.now(UTC).isoformat(),
        "dataset": "由命令行 source 指定，详见实例 manifest",
        "agents": list(adapters),
        "model": EXPECTED_MODEL,
        "max_iterations": options.max_iterations,
        "max_input_tokens": options.max_input_tokens,
        "max_output_tokens": options.max_output_tokens,
        "timeout_seconds": options.timeout_seconds,
        "concurrency": options.concurrency,
        "output_dir": str(output_dir),
        "instances": [item.as_manifest() for item in instances],
    }


def _run_one(
    instance: SWEbenchInstance,
    agent_name: str,
    adapter: Any,
    output_dir: Path,
    options: ExperimentOptions,
) -> InstanceAgentResult:
    artifact_dir = output_dir / agent_name / _safe_name(instance.instance_id)
    result_path = artifact_dir / "result.json"
    if options.resume and result_path.is_file():
        try:
            return InstanceAgentResult.from_json(result_path)
        except (OSError, TypeError, ValueError, json.JSONDecodeError, KeyError):
            pass
    artifact_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    workspace: Path | None = None
    baseline = TestSummary("", None, None, output="未执行")
    after = TestSummary("", None, None, output="未执行")
    adapter_result = AdapterResult(
        status="blocked", model=EXPECTED_MODEL, elapsed_seconds=0, error="未启动"
    )
    error = ""
    try:
        source_repo = _resolve_repo(instance.repo, output_dir / "_repos")
        _verify_commit(source_repo, instance.base_commit)
        workspace = output_dir / "_checkouts" / _safe_name(instance.instance_id) / agent_name
        _create_checkout(source_repo, workspace, instance.base_commit)
        baseline = _run_tests(workspace, instance, options.test_command, options.timeout_seconds)
        prompt = build_prompt(instance, options)
        if agent_name == "tuicodingagent":
            adapter_result = adapter.run(
                workspace=workspace,
                prompt=prompt,
                output_dir=artifact_dir,
                max_iterations=options.max_iterations,
                timeout_seconds=options.timeout_seconds,
            )
        else:
            adapter_result = adapter.run(
                workspace=workspace, prompt=prompt, timeout_seconds=options.timeout_seconds
            )
        after = _run_tests(workspace, instance, options.test_command, options.timeout_seconds)
        error = adapter_result.error
    except (OSError, RuntimeError, ValueError) as exc:
        error = f"{type(exc).__name__}: {exc}"
        adapter_result = AdapterResult(
            status="blocked", model=EXPECTED_MODEL, elapsed_seconds=0, error=error
        )
    finally:
        if workspace is not None:
            _remove_checkout(workspace)

    patch = _git_diff(workspace) if workspace is not None and workspace.exists() else ""
    # Worktree 删除前获取补丁；正常路径在 finally 后 workspace 已不存在，因此重算改为缓存。
    if not patch and workspace is not None:
        patch = _read_existing_patch(artifact_dir / "final.patch")
    f2p_passed = _count_passed(instance.fail_to_pass, after.per_test)
    p2p_passed = _count_passed(instance.pass_to_pass, after.per_test)
    resolved = _resolved(adapter_result, instance, after)
    status = adapter_result.status
    if status == "blocked" and not error:
        error = adapter_result.error
    result = InstanceAgentResult(
        benchmark=instance.benchmark,
        version=instance.version,
        split=instance.split,
        instance_id=instance.instance_id,
        agent=agent_name,
        model=adapter_result.model,
        status=status,
        resolved=resolved,
        baseline_passed=baseline.passed,
        after_passed=after.passed,
        fail_to_pass_total=len(instance.fail_to_pass),
        fail_to_pass_passed=f2p_passed,
        pass_to_pass_total=len(instance.pass_to_pass),
        pass_to_pass_passed=p2p_passed,
        input_tokens=adapter_result.input_tokens,
        output_tokens=adapter_result.output_tokens,
        cached_input_tokens=adapter_result.cached_input_tokens,
        total_tokens=adapter_result.input_tokens + adapter_result.output_tokens,
        elapsed_seconds=round(max(time.monotonic() - started, adapter_result.elapsed_seconds), 3),
        tool_calls=adapter_result.tool_calls,
        tool_failures=adapter_result.tool_failures,
        retries=adapter_result.retries,
        timeout=adapter_result.status == "timeout",
        error=error,
    )
    _write_artifacts(
        artifact_dir, instance, options, adapter_result, baseline, after, patch, result
    )
    return result


def build_prompt(instance: SWEbenchInstance, options: ExperimentOptions) -> str:
    """为两个代理生成完全相同的任务提示。"""

    budget = f"最多 {options.max_iterations} 轮；超时 {options.timeout_seconds:g} 秒。"
    return (
        "你正在独立 checkout 中修复一个 SWE-bench Live issue。\n"
        "只修改完成任务所需的仓库文件，先理解现有实现，再运行相关测试；不要修改评测产物目录。\n"
        f"实例：{instance.instance_id}\n"
        f"问题描述：\n{instance.problem_statement}\n"
        f"FAIL_TO_PASS：{json.dumps(instance.fail_to_pass, ensure_ascii=False)}\n"
        f"PASS_TO_PASS：{json.dumps(instance.pass_to_pass, ensure_ascii=False)}\n"
        f"共同预算：{budget}\n"
        "完成后保留代码修改并用简短文字总结修改和测试结果。"
    )


def _resolve_repo(repo: str, cache_dir: Path) -> Path:
    candidate = Path(repo).expanduser()
    if candidate.is_dir() and (candidate / ".git").exists():
        return candidate.resolve()
    if not repo.startswith(("https://", "http://", "git@", "ssh://")):
        raise ValueError(f"repo 不是本地 Git 仓库或可 clone URL：{repo}")
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / _safe_name(repo.rsplit("/", 1)[-1].removesuffix(".git"))
    if not target.exists():
        _run_git(["clone", "--no-tags", "--filter=blob:none", repo, str(target)], Path.cwd())
    return target


def _verify_commit(repo: Path, commit: str) -> None:
    result = _run_git(["cat-file", "-e", f"{commit}^{{commit}}"], repo, check=False)
    if result.returncode != 0:
        raise ValueError(f"base_commit 不存在或无法解析：{commit}")


def _create_checkout(repo: Path, path: Path, commit: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise RuntimeError(f"隔离 checkout 已存在，拒绝覆盖：{path}")
    result = _run_git(["worktree", "add", "--detach", str(path), commit], repo, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git worktree add 失败")


def _remove_checkout(path: Path) -> None:
    if not path.exists():
        return
    result = _run_git(
        ["worktree", "remove", "--force", str(path)], path.parent.parent.parent, check=False
    )
    if result.returncode != 0:
        shutil.rmtree(path, ignore_errors=True)


def _run_tests(
    workspace: Path, instance: SWEbenchInstance, override: str, timeout_seconds: float
) -> TestSummary:
    command = override or instance.test_command
    tests = (*instance.fail_to_pass, *instance.pass_to_pass)
    if command:
        return _run_command(command, workspace, timeout_seconds, {test: True for test in tests})
    if not tests:
        return TestSummary(
            "", None, None, output="缺少 test_command 且没有 FAIL_TO_PASS/PASS_TO_PASS"
        )
    per_test: dict[str, bool] = {}
    output: list[str] = []
    timed_out = False
    for test in tests:
        summary = _run_command(f"python -m pytest -q {test}", workspace, timeout_seconds)
        per_test[test] = summary.passed is True
        output.append(f"$ {summary.command}\n{summary.output}")
        timed_out = timed_out or summary.timed_out
    passed = all(per_test.values()) if per_test else None
    return TestSummary(
        "python -m pytest -q <per-test>",
        0 if passed else 1,
        passed,
        timed_out,
        per_test,
        "\n".join(output),
    )


def _run_command(
    command: str,
    workspace: Path,
    timeout_seconds: float,
    per_test: dict[str, bool] | None = None,
) -> TestSummary:
    try:
        completed = subprocess.run(
            command,
            cwd=workspace,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return TestSummary(
            command, None, False, True, per_test, _text(exc.stdout) + _text(exc.stderr)
        )
    output = completed.stdout + completed.stderr
    passed = completed.returncode == 0
    if per_test is not None:
        per_test = {test: passed for test in per_test}
    return TestSummary(command, completed.returncode, passed, False, per_test, output)


def _resolved(
    adapter: AdapterResult, instance: SWEbenchInstance, after: TestSummary
) -> bool | None:
    if adapter.status != "completed" or after.passed is None:
        return None
    if after.per_test is None:
        return after.passed
    return all(
        after.per_test.get(test, False) for test in (*instance.fail_to_pass, *instance.pass_to_pass)
    )


def _count_passed(tests: tuple[str, ...], values: dict[str, bool] | None) -> int:
    return sum(1 for test in tests if values and values.get(test) is True)


def _write_artifacts(
    artifact_dir: Path,
    instance: SWEbenchInstance,
    options: ExperimentOptions,
    adapter: AdapterResult,
    baseline: TestSummary,
    after: TestSummary,
    patch: str,
    result: InstanceAgentResult,
) -> None:
    manifest = {
        **instance.as_manifest(),
        "agent": result.agent,
        "model": result.model,
        "config_path": str(options.config_path),
        "max_iterations": options.max_iterations,
        "max_input_tokens": options.max_input_tokens,
        "max_output_tokens": options.max_output_tokens,
        "timeout_seconds": options.timeout_seconds,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cwd": str(Path.cwd()),
        "created_at": datetime.now(UTC).isoformat(),
    }
    (artifact_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    events = adapter.events or ({"type": "adapter_summary", **asdict(adapter)},)
    (artifact_dir / "trajectory.jsonl").write_text(
        "".join(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )
    (artifact_dir / "stdout.log").write_text(adapter.stdout, encoding="utf-8")
    (artifact_dir / "stderr.log").write_text(adapter.stderr, encoding="utf-8")
    (artifact_dir / "baseline-test-output.txt").write_text(baseline.output, encoding="utf-8")
    (artifact_dir / "test-output.txt").write_text(after.output, encoding="utf-8")
    (artifact_dir / "final.patch").write_text(patch, encoding="utf-8")
    (artifact_dir / "result.json").write_text(
        json.dumps(asdict(result), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_reports(results: list[InstanceAgentResult], output_dir: Path) -> None:
    """写 JSON、CSV 和含五项对比表的 Markdown 汇总。"""

    rows = [asdict(result) for result in results]
    (output_dir / "summary.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if rows:
        with (output_dir / "summary.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    (output_dir / "report.md").write_text(_markdown_report(results), encoding="utf-8")


def _markdown_report(results: list[InstanceAgentResult]) -> str:
    agents = sorted({item.agent for item in results})
    lines = [
        "# SWE-bench Live 双代理评测报告",
        "",
        "本报告只汇总 harness 实际执行并落盘的结果，不代表官方 leaderboard 或可比公开分数。",
        "",
        "## 五项对比表",
        "",
        "| 指标 | " + " | ".join(agents) + " |",
        "|---|" + "---|" * len(agents),
    ]
    metrics: list[tuple[str, Callable[[str], str]]] = [
        ("Resolved Rate / Pass@1", lambda agent: _rate(results, agent, "resolved")),
        (
            "F2P Rate",
            lambda agent: _test_rate(results, agent, "fail_to_pass_passed", "fail_to_pass_total"),
        ),
        (
            "P2P Rate",
            lambda agent: _test_rate(results, agent, "pass_to_pass_passed", "pass_to_pass_total"),
        ),
        ("Token / Resolved", lambda agent: _per_resolved(results, agent, "total_tokens")),
        (
            "Time / Resolved",
            lambda agent: _per_resolved(results, agent, "elapsed_seconds", suffix="s"),
        ),
    ]
    for label, value in metrics:
        lines.append("| " + label + " | " + " | ".join(value(agent) for agent in agents) + " |")
    lines.extend(
        [
            "",
            "## 口径",
            "",
            "- Resolved/Pass@1 仅对 adapter 成功且修复后测试全部通过的实例计数。",
            "- F2P/P2P 分母分别是该代理实际运行实例中对应测试总数；分母为零显示 `N/A`。",
            "- 缺失 usage 按 0 记录，但不会把 adapter 失败算作 resolved。",
            "- Token/Resolved 与 Time/Resolved 在 resolved=0 时显示 `N/A`。",
            "- 另记录 timeout rate、tool failure rate、calls/resolved、失败数和重试数。",
            "",
            "## 详细统计",
            "",
        ]
    )
    for agent in agents:
        subset = [item for item in results if item.agent == agent]
        resolved = sum(item.resolved is True for item in subset)
        tool_calls = sum(item.tool_calls for item in subset)
        lines.append(
            f"- `{agent}`：实例 {len(subset)}，"
            f"成功 {sum(item.status == 'completed' for item in subset)}，"
            f"失败 {sum(item.status == 'failed' for item in subset)}，"
            f"超时 {sum(item.timeout for item in subset)}，"
            f"tool failure rate "
            f"{_safe_ratio(sum(item.tool_failures for item in subset), tool_calls)}，"
            f"calls/resolved {_safe_div(tool_calls, resolved)}，"
            f"重试 {sum(item.retries for item in subset)}。"
        )
    return "\n".join(lines) + "\n"


def _rate(results: list[InstanceAgentResult], agent: str, field: str) -> str:
    subset = [item for item in results if item.agent == agent]
    denominator = len(subset)
    numerator = sum(getattr(item, field) is True for item in subset)
    return _ratio(numerator, denominator)


def _test_rate(
    results: list[InstanceAgentResult], agent: str, numerator: str, denominator: str
) -> str:
    subset = [item for item in results if item.agent == agent]
    return _ratio(
        sum(getattr(item, numerator) for item in subset),
        sum(getattr(item, denominator) for item in subset),
    )


def _per_resolved(
    results: list[InstanceAgentResult], agent: str, field: str, suffix: str = ""
) -> str:
    subset = [item for item in results if item.agent == agent and item.resolved is True]
    if not subset:
        return "N/A"
    return f"{sum(getattr(item, field) for item in subset) / len(subset):.1f}{suffix}"


def _ratio(numerator: int, denominator: int) -> str:
    return (
        "N/A" if denominator == 0 else f"{numerator / denominator:.1%} ({numerator}/{denominator})"
    )


def _safe_ratio(numerator: int, denominator: int) -> str:
    return _ratio(numerator, denominator)


def _safe_div(numerator: int, denominator: int) -> str:
    return "N/A" if denominator == 0 else f"{numerator / denominator:.1f}"


def _safe_name(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in "._-" else "_" for character in value
    )


def _run_git(args: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git 命令失败：{' '.join(args)}")
    return result


def _read_existing_patch(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


__all__ = [
    "ExperimentOptions",
    "InstanceAgentResult",
    "TestSummary",
    "build_prompt",
    "run_experiment",
    "write_reports",
]
