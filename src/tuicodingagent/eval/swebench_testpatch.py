"""注入 SWE-bench test_patch 后再进行有效性门控的评测层。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .adapters import EXPECTED_MODEL, AdapterResult, TUICodingAgentAdapter
from .adapters_live import CodexCLIAdapter
from .swebench import SWEbenchInstance, _decode_rows, select_instances
from .swebench_system import SystemOptions, _git, _prompt, _repo, _safe
from .swebench_validated import (
    CommandResult,
    ValidatedResult,
    _baseline_reason,
    _passed,
    _remove_worktree,
    _resolved,
    _run_command,
    _write_artifacts,
    write_validated_reports,
)


@dataclass(frozen=True, slots=True)
class PatchedInstance(SWEbenchInstance):
    """保留官方 test_patch 的规范化实例。"""

    test_patch: str = ""


@dataclass(frozen=True, slots=True)
class TestPatchResult:
    """test_patch 应用过程的审计记录。"""

    sha256: str
    applied: bool
    returncode: int
    output: str
    elapsed_seconds: float
    files: tuple[str, ...]


def load_patched_instances(
    source: str | Path, *, default_benchmark: str = "swebench_live"
) -> list[PatchedInstance]:
    """读取本地 JSON/JSONL，保留原始 test_patch。"""

    from .swebench import load_instances

    base = load_instances(source, default_benchmark=default_benchmark)
    path = Path(source)
    if not path.is_file():
        raise ValueError("test_patch 保留读取要求本地 JSON/JSONL 数据源")
    text = path.read_text(encoding="utf-8-sig")
    rows = _decode_rows(text, path.name)
    patches = {
        str(row.get("instance_id", row.get("id", ""))): row.get("test_patch", "") for row in rows
    }
    result: list[PatchedInstance] = []
    for item in base:
        patch = patches.get(item.instance_id, "")
        if not isinstance(patch, str):
            raise ValueError(f"实例 {item.instance_id} 的 test_patch 必须是字符串")
        result.append(PatchedInstance(**asdict(item), test_patch=patch))
    return result


def run_testpatch(
    *,
    dataset: str | Path,
    output_dir: Path,
    options: SystemOptions,
    setup_command: str,
    sample: int | None = None,
    instance_ids: set[str] | None = None,
    benchmark: str = "swebench_live",
) -> list[ValidatedResult]:
    """在两个独立 workspace 注入 test_patch 后运行。"""

    instances = select_instances(
        load_patched_instances(dataset, default_benchmark=benchmark),
        instance_ids=instance_ids,
        sample=sample,
    )
    adapters: dict[str, object] = {}
    if "tuicodingagent" in options.agents:
        adapters["tuicodingagent"] = TUICodingAgentAdapter(options.config_path, options.provider)
    if "codex" in options.agents:
        adapters["codex"] = CodexCLIAdapter()
    if set(options.agents) - set(adapters):
        raise ValueError("代理只能是 tuicodingagent 或 codex")
    for adapter in adapters.values():
        adapter.validate()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "experiment-manifest.json").write_text(
        json.dumps(
            {
                "dataset": str(dataset),
                "model": EXPECTED_MODEL,
                "agents": list(options.agents),
                "setup_command": setup_command,
                "test_patch_injected": True,
                "instances": [item.as_manifest() for item in instances],
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
    results = [
        _run_one(item, name, adapter, output_dir, options, setup_command)
        for item, name, adapter in tasks
    ]
    write_validated_reports(results, output_dir)
    return results


def _run_one(
    instance: PatchedInstance,
    name: str,
    adapter: object,
    output: Path,
    options: SystemOptions,
    setup_command: str,
) -> ValidatedResult:
    artifact = output / name / _safe(instance.instance_id)
    artifact.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    source: Path | None = None
    checkout: Path | None = None
    patch = ""
    setup = CommandResult(setup_command, None, None, "未执行", 0)
    baseline = CommandResult("", None, None, "未执行", 0)
    after = CommandResult("", None, None, "未执行", 0)
    adapter_result = AdapterResult("invalid", EXPECTED_MODEL, 0, error="未启动")
    test_patch_result = _patch_result(instance.test_patch, False, "未执行", 0)
    invalid_reason = ""
    error = ""
    try:
        source = _repo(instance.repo, output / "_repos")
        _git(["cat-file", "-e", f"{instance.base_commit}^{{commit}}"], source)
        checkout = output / "_checkouts" / _safe(instance.instance_id) / name
        _git(["worktree", "add", "--detach", str(checkout), instance.base_commit], source)
        test_patch_result = _apply_test_patch(checkout, instance.test_patch)
        if not test_patch_result.applied:
            invalid_reason = "test_patch_apply_failed"
        setup = (
            _run_command(setup_command, checkout, options.timeout_seconds)
            if setup_command
            else CommandResult("", 0, True, "未配置 setup command", 0)
        )
        if not invalid_reason and setup.passed is not True:
            invalid_reason = "setup_failed"
        if not invalid_reason:
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
            patch = _agent_diff(checkout, test_patch_result.files)
            error = adapter_result.error
    except (OSError, RuntimeError, ValueError) as exc:
        invalid_reason = "checkout_or_source_failed"
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if checkout is not None and source is not None:
            _remove_worktree(source, checkout)
    invalid = bool(invalid_reason)
    if invalid:
        status, resolved = "invalid", None
        adapter_result = AdapterResult(
            "invalid", EXPECTED_MODEL, adapter_result.elapsed_seconds, error=error or invalid_reason
        )
    else:
        status, resolved = adapter_result.status, _resolved(adapter_result, instance, after)
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
    (artifact / "test-patch.json").write_text(
        json.dumps(asdict(test_patch_result), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


def _apply_test_patch(checkout: Path, patch: str) -> TestPatchResult:
    started = time.monotonic()
    digest = hashlib.sha256(patch.encode("utf-8")).hexdigest()
    if not patch.strip():
        return TestPatchResult(digest, True, 0, "未提供 test_patch", 0, ())
    result = subprocess.run(
        ["git", "-C", str(checkout), "apply", "--whitespace=nowarn", "-"],
        input=patch,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    files = tuple(
        sorted(
            {
                line.split(" b/", 1)[1]
                for line in patch.splitlines()
                if line.startswith("diff --git a/") and " b/" in line
            }
        )
    )
    return TestPatchResult(
        digest,
        result.returncode == 0,
        result.returncode,
        result.stdout + result.stderr,
        time.monotonic() - started,
        files,
    )


def _patch_result(patch: str, applied: bool, output: str, elapsed: float) -> TestPatchResult:
    return TestPatchResult(
        hashlib.sha256(patch.encode("utf-8")).hexdigest(),
        applied,
        0 if applied else 1,
        output,
        elapsed,
        (),
    )


def _run_tests(workspace: Path, instance: PatchedInstance, timeout: float) -> CommandResult:
    tests = (*instance.fail_to_pass, *instance.pass_to_pass)
    values: dict[str, bool] = {}
    outputs: list[str] = []
    started = time.monotonic()
    for test in tests:
        item = _run_command(f"pytest -q {test}", workspace, timeout)
        values[test] = item.passed is True
        outputs.append(f"$ {item.command}\n{item.output}")
    return CommandResult(
        "pytest -q <per-test>",
        0 if values and all(values.values()) else 1,
        bool(values) and all(values.values()),
        "\n".join(outputs),
        time.monotonic() - started,
        values,
    )


def _agent_diff(checkout: Path, excluded: tuple[str, ...]) -> str:
    args = ["diff", "--binary", "HEAD", "--", "."] + [f":(exclude){path}" for path in excluded]
    result = _git(args, checkout, check=False)
    return result.stdout if result.returncode == 0 else ""


__all__ = ["PatchedInstance", "TestPatchResult", "load_patched_instances", "run_testpatch"]
