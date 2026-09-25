"""独立的 SWE-bench Live 双代理评测系统。"""

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

from .adapters import EXPECTED_MODEL, AdapterResult, CodexCLIAdapter, TUICodingAgentAdapter
from .runner import _git_diff
from .swebench import SWEbenchInstance, load_instances, select_instances


@dataclass(frozen=True, slots=True)
class SystemOptions:
    """所有代理共享的公平约束。"""

    config_path: Path
    provider: str = "CloudVisit"
    agents: tuple[str, ...] = ("tuicodingagent", "codex")
    max_iterations: int = 20
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    timeout_seconds: float = 1800.0
    test_command: str = ""
    concurrency: int = 1
    resume: bool = False
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class TestResult:
    """测试命令结果及可选的逐测试结果。"""

    command: str
    returncode: int | None
    passed: bool | None
    output: str
    per_test: dict[str, bool] | None = None
    timed_out: bool = False


@dataclass(frozen=True, slots=True)
class SystemResult:
    """一个实例和一个代理的最终结果。"""

    benchmark: str
    version: str
    split: str
    instance_id: str
    base_commit: str
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


def run_system(
    *,
    dataset: str | Path,
    output_dir: Path,
    options: SystemOptions,
    sample: int | None = None,
    instance_ids: set[str] | None = None,
    benchmark: str = "swebench_live",
) -> list[SystemResult]:
    """读取实例，先规划，再按实例和代理隔离运行。"""

    if options.concurrency < 1 or options.max_iterations < 1 or options.timeout_seconds <= 0:
        raise ValueError("concurrency、max_iterations、timeout 必须为正数")
    instances = select_instances(
        load_instances(dataset, default_benchmark=benchmark),
        instance_ids=instance_ids,
        sample=sample,
    )
    adapters = _adapters(options)
    for adapter in adapters.values():
        adapter.validate()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "experiment-manifest.json").write_text(
        json.dumps(_manifest(dataset, instances, options), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if options.dry_run:
        return []
    tasks = [(instance, name, adapters[name]) for instance in instances for name in options.agents]
    if options.concurrency == 1:
        results = [
            _run_one(instance, name, adapter, output_dir, options)
            for instance, name, adapter in tasks
        ]
    else:
        with ThreadPoolExecutor(max_workers=options.concurrency) as pool:
            results = list(pool.map(lambda task: _run_one(*task, output_dir, options), tasks))
    write_reports(results, output_dir)
    return results


def _adapters(options: SystemOptions) -> dict[str, Any]:
    allowed = {"tuicodingagent", "codex"}
    if not options.agents or set(options.agents) - allowed:
        raise ValueError("代理只能是 tuicodingagent 或 codex，且至少选择一个")
    result: dict[str, Any] = {}
    if "tuicodingagent" in options.agents:
        result["tuicodingagent"] = TUICodingAgentAdapter(options.config_path, options.provider)
    if "codex" in options.agents:
        result["codex"] = CodexCLIAdapter()
    return result


def _manifest(
    dataset: str | Path, instances: list[SWEbenchInstance], options: SystemOptions
) -> dict[str, object]:
    return {
        "created_at": datetime.now(UTC).isoformat(),
        "dataset": str(dataset),
        "model": EXPECTED_MODEL,
        "agents": list(options.agents),
        "max_iterations": options.max_iterations,
        "max_input_tokens": options.max_input_tokens,
        "max_output_tokens": options.max_output_tokens,
        "timeout_seconds": options.timeout_seconds,
        "concurrency": options.concurrency,
        "instances": [item.as_manifest() for item in instances],
    }


def _run_one(
    instance: SWEbenchInstance,
    agent_name: str,
    adapter: Any,
    output_dir: Path,
    options: SystemOptions,
) -> SystemResult:
    artifact = output_dir / agent_name / _safe(instance.instance_id)
    result_file = artifact / "result.json"
    if options.resume and result_file.is_file():
        try:
            data = json.loads(result_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("instance_id") == instance.instance_id:
                return SystemResult(
                    **{field: data[field] for field in SystemResult.__dataclass_fields__}
                )
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
            pass
    artifact.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    source: Path | None = None
    checkout: Path | None = None
    patch = ""
    baseline = TestResult("", None, None, "未执行")
    after = TestResult("", None, None, "未执行")
    adapter_result = AdapterResult("blocked", EXPECTED_MODEL, 0, error="未启动")
    error = ""
    try:
        source = _repo(instance.repo, output_dir / "_repos")
        _git(["cat-file", "-e", f"{instance.base_commit}^{{commit}}"], source)
        checkout = output_dir / "_checkouts" / _safe(instance.instance_id) / agent_name
        _create_sealed_snapshot(
            source, checkout, instance.base_commit, task_text=instance.problem_statement
        )
        baseline = _tests(checkout, instance, options.test_command, options.timeout_seconds)
        prompt = _prompt(instance, options)
        if agent_name == "tuicodingagent":
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
        after = _tests(checkout, instance, options.test_command, options.timeout_seconds)
        patch = _git_diff(checkout)
        error = adapter_result.error
    except (OSError, RuntimeError, ValueError) as exc:
        error = f"{type(exc).__name__}: {exc}"
        adapter_result = AdapterResult("blocked", EXPECTED_MODEL, 0, error=error)
    finally:
        # 保留封存副本，供补丁、轨迹与失败复盘审计；它不含原仓库提交历史。
        del source
    f2p_passed = _passed(instance.fail_to_pass, after.per_test)
    p2p_passed = _passed(instance.pass_to_pass, after.per_test)
    resolved = _resolved(adapter_result, instance, after)
    result = SystemResult(
        benchmark=instance.benchmark,
        version=instance.version,
        split=instance.split,
        instance_id=instance.instance_id,
        base_commit=instance.base_commit,
        agent=agent_name,
        model=adapter_result.model,
        status=adapter_result.status,
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
    _write_artifacts(artifact, instance, options, adapter_result, baseline, after, patch, result)
    return result


def _prompt(instance: SWEbenchInstance, options: SystemOptions) -> str:
    return (
        "你正在独立、无历史的 SWE-bench Live 工作副本中修复 issue。"
        "请先读取当前目录的 task.txt；它是唯一的任务描述。只修改必要的仓库文件，"
        "先理解现有实现，再运行相关测试；不要修改评测产物目录或测试文件。编辑前确认旧文本"
        "在目标位置唯一；若编辑工具提示匹配冲突，先读取更小范围上下文，再用更具体锚点重试，"
        "不要因一次工具调用失败而放弃修复。\n"
        f"实例：{instance.instance_id}\n"
        f"共同预算：最多 {options.max_iterations} 轮，超时 {options.timeout_seconds:g} 秒。\n"
        "完成后保留代码修改，并简短总结修改和测试结果。"
    )


def _tests(
    workspace: Path, instance: SWEbenchInstance, override: str, timeout: float
) -> TestResult:
    command = override or instance.test_command
    tests = (*instance.fail_to_pass, *instance.pass_to_pass)
    if command:
        return _run_command(command, workspace, timeout, {test: True for test in tests})
    # 多语言测试标识不能安全映射为本地 pytest 命令；正式结果由官方 Docker harness 提供。
    return TestResult("", None, None, "未执行本地测试；等待官方 harness 评分")


def _run_command(
    command: str, workspace: Path, timeout: float, per_test: dict[str, bool] | None = None
) -> TestResult:
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
        return TestResult(
            command, None, False, _text(exc.stdout) + _text(exc.stderr), per_test, True
        )
    passed = completed.returncode == 0
    return TestResult(
        command,
        completed.returncode,
        passed,
        completed.stdout + completed.stderr,
        {test: passed for test in per_test} if per_test is not None else None,
    )


def _resolved(adapter: AdapterResult, instance: SWEbenchInstance, after: TestResult) -> bool | None:
    if adapter.status != "completed" or after.passed is None:
        return None
    if after.per_test is None:
        return after.passed
    return all(
        after.per_test.get(test, False) for test in (*instance.fail_to_pass, *instance.pass_to_pass)
    )


def _passed(tests: tuple[str, ...], values: dict[str, bool] | None) -> int:
    return sum(values.get(test, False) for test in tests) if values else 0


def _write_artifacts(
    artifact: Path,
    instance: SWEbenchInstance,
    options: SystemOptions,
    adapter: AdapterResult,
    baseline: TestResult,
    after: TestResult,
    patch: str,
    result: SystemResult,
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
        "created_at": datetime.now(UTC).isoformat(),
    }
    (artifact / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    events = adapter.events or ({"type": "adapter_summary", **asdict(adapter)},)
    (artifact / "trajectory.jsonl").write_text(
        "".join(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )
    (artifact / "stdout.log").write_text(adapter.stdout, encoding="utf-8")
    (artifact / "stderr.log").write_text(adapter.stderr, encoding="utf-8")
    (artifact / "baseline-test-output.txt").write_text(baseline.output, encoding="utf-8")
    (artifact / "test-output.txt").write_text(after.output, encoding="utf-8")
    (artifact / "final.patch").write_text(patch, encoding="utf-8")
    (artifact / "result.json").write_text(
        json.dumps(asdict(result), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def write_reports(results: list[SystemResult], output_dir: Path) -> None:
    """写逐实例 JSON/CSV 和含五项表格的 Markdown。"""

    rows = [asdict(item) for item in results]
    (output_dir / "summary.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if rows:
        with (output_dir / "summary.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    (output_dir / "report.md").write_text(_report(results), encoding="utf-8")


def _report(results: list[SystemResult]) -> str:
    agents = sorted({item.agent for item in results})
    lines = [
        "# SWE-bench Live 双代理评测报告",
        "",
        "本报告只汇总实际运行结果，不代表官方 leaderboard 或官方可比结果。",
        "",
        "## 五项对比表",
        "",
        "| 指标 | " + " | ".join(agents) + " |",
        "|---|" + "---|" * len(agents),
    ]
    for label, fn in (
        ("Resolved Rate / Pass@1", lambda a: _rate(results, a, "resolved")),
        (
            "F2P Rate",
            lambda a: _tests_rate(results, a, "fail_to_pass_passed", "fail_to_pass_total"),
        ),
        (
            "P2P Rate",
            lambda a: _tests_rate(results, a, "pass_to_pass_passed", "pass_to_pass_total"),
        ),
        ("Token / Resolved", lambda a: _per_resolved(results, a, "total_tokens")),
        ("Time / Resolved", lambda a: _per_resolved(results, a, "elapsed_seconds", "s")),
    ):
        lines.append(f"| {label} | " + " | ".join(fn(agent) for agent in agents) + " |")
    lines += [
        "",
        "## 口径",
        "",
        "- Resolved/Pass@1 是 adapter 成功且修复后 FAIL_TO_PASS 与",
        "  PASS_TO_PASS 全通过的实例数除以运行实例数。",
        "- F2P/P2P 分母分别是实际记录的对应测试数；分母为零显示 `N/A`。",
        "- 缺失 usage 按 0 记录；resolved=0 时 Token/Resolved 与 Time/Resolved 显示 `N/A`。",
        "- timeout rate、tool failure rate、calls/resolved、失败数、重试数见下方。",
        "",
    ]
    for agent in agents:
        subset = [item for item in results if item.agent == agent]
        resolved = sum(item.resolved is True for item in subset)
        calls = sum(item.tool_calls for item in subset)
        elapsed = sorted(item.elapsed_seconds for item in subset)
        lines.append(
            f"- `{agent}`：成功 {sum(item.status == 'completed' for item in subset)}，"
            f"失败 {sum(item.status == 'failed' for item in subset)}，"
            f"超时率 {_ratio(sum(item.timeout for item in subset), len(subset))}，"
            f"tool failure rate {_ratio(sum(item.tool_failures for item in subset), calls)}，"
            f"calls/resolved {_div(calls, resolved)}，"
            f"重试 {sum(item.retries for item in subset)}，总耗时 {sum(elapsed):.1f}s，"
            f"中位数 {_percentile(elapsed, 50):.1f}s，P90 {_percentile(elapsed, 90):.1f}s。"
        )
    return "\n".join(lines) + "\n"


def _rate(results: list[SystemResult], agent: str, field: str) -> str:
    subset = [item for item in results if item.agent == agent]
    return _ratio(sum(getattr(item, field) is True for item in subset), len(subset))


def _tests_rate(results: list[SystemResult], agent: str, numerator: str, denominator: str) -> str:
    subset = [item for item in results if item.agent == agent]
    return _ratio(
        sum(getattr(item, numerator) for item in subset),
        sum(getattr(item, denominator) for item in subset),
    )


def _per_resolved(results: list[SystemResult], agent: str, field: str, suffix: str = "") -> str:
    subset = [item for item in results if item.agent == agent and item.resolved is True]
    return (
        "N/A"
        if not subset
        else f"{sum(getattr(item, field) for item in subset) / len(subset):.1f}{suffix}"
    )


def _ratio(numerator: int, denominator: int) -> str:
    return (
        "N/A" if denominator == 0 else f"{numerator / denominator:.1%} ({numerator}/{denominator})"
    )


def _div(numerator: int, denominator: int) -> str:
    return "N/A" if denominator == 0 else f"{numerator / denominator:.1f}"


def _percentile(values: list[float], percentile: int) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * percentile / 100
    lower, upper = int(position), min(int(position) + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def _repo(value: str, cache: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_dir() and (path / ".git").exists():
        return path.resolve()
    source = value
    if not source.startswith(("https://", "http://", "git@", "ssh://")):
        parts = source.split("/")
        if len(parts) == 2 and all(parts):
            # SWE-bench 数据集用 owner/repo 表示 GitHub 仓库。
            source = f"https://github.com/{source}.git"
        else:
            raise ValueError(f"repo 不是本地 Git 仓库或可 clone URL：{value}")
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / _safe(value.replace("/", "__").removesuffix(".git"))
    if not target.exists():
        _git(["clone", "--no-tags", "--filter=blob:none", source, str(target)], Path.cwd())
    return target


def _create_sealed_snapshot(
    source: Path, checkout: Path, commit: str, *, task_text: str | None = None
) -> None:
    """从 base commit 导出无历史的单提交工作副本。"""
    if checkout.exists():
        raise RuntimeError(f"封存 checkout 已存在，拒绝覆盖：{checkout}")
    checkout.mkdir(parents=True)
    archive = subprocess.Popen(
        ["git", "-C", str(source), "archive", commit],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    extract = subprocess.run(
        ["tar", "-x", "-C", str(checkout)], stdin=archive.stdout, capture_output=True, check=False
    )
    if archive.stdout is not None:
        archive.stdout.close()
    stderr_bytes = archive.stderr.read() if archive.stderr is not None else b""
    archive.wait()
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    if archive.returncode != 0:
        raise RuntimeError(stderr or "git archive 导出失败")
    if extract.returncode != 0:
        # Windows 可能没有创建符号链接的权限。回退仍从同一 base commit 导出文件，
        # 并在输出目录重新 init，因此不会把原仓库历史暴露给 Agent。
        shutil.rmtree(checkout, ignore_errors=True)
        staging = checkout.parent / f".archive-fallback-{_safe(commit[:12])}"
        if staging.exists():
            raise RuntimeError(f"封存临时 worktree 已存在，拒绝覆盖：{staging}")
        try:
            _git(["worktree", "add", "--detach", str(staging), commit], source)
            shutil.copytree(
                staging,
                checkout,
                symlinks=False,
                ignore=shutil.ignore_patterns(".git"),
            )
        finally:
            _git(["worktree", "remove", "--force", str(staging)], source, check=False)
    if task_text is not None:
        (checkout / "task.txt").write_text(task_text, encoding="utf-8")
    _git(["init"], checkout)
    _git(["config", "user.email", "swebench-eval@local.invalid"], checkout)
    _git(["config", "user.name", "SWE-bench Eval"], checkout)
    _git(["add", "-A"], checkout)
    _git(["commit", "-m", "sealed base snapshot"], checkout)


def _remove_worktree(repo: Path, checkout: Path) -> None:
    if checkout.exists():
        result = _git(["worktree", "remove", "--force", str(checkout)], repo, check=False)
        if result.returncode != 0:
            shutil.rmtree(checkout, ignore_errors=True)


def _git(args: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
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


def _safe(value: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in value)


def _text(value: str | bytes | None) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else (value or "")


__all__ = ["SystemOptions", "SystemResult", "TestResult", "run_system", "write_reports"]
