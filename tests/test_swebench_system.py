from __future__ import annotations

import json
from pathlib import Path

import pytest

from tuicodingagent.eval.adapters import EXPECTED_MODEL, CodexCLIAdapter, TUICodingAgentAdapter
from tuicodingagent.eval.swebench import DatasetError, load_instances, select_instances
from tuicodingagent.eval.swebench_system import SystemResult, _report


def test_load_and_select_normalizes_swebench_fields(tmp_path: Path) -> None:
    path = tmp_path / "instances.jsonl"
    path.write_text(
        json.dumps(
            {
                "instance_id": "demo__repo-1",
                "base_commit": "abc123",
                "repo": str(tmp_path),
                "problem_statement": "修复问题",
                "FAIL_TO_PASS": '["tests/test_demo.py::test_fix"]',
                "PASS_TO_PASS": ["tests/test_demo.py::test_old"],
                "version": "v1",
                "split": "canary",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    item = load_instances(path)[0]
    assert item.instance_id == "demo__repo-1"
    assert item.fail_to_pass == ("tests/test_demo.py::test_fix",)
    assert select_instances([item], instance_ids={item.instance_id}) == [item]


def test_dataset_requires_core_fields(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(DatasetError, match="instance_id"):
        load_instances(path)


def test_model_lock_rejects_wrong_tuicodingagent_model(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        (
            "providers:\n"
            "  - name: CloudVisit\n"
            "    protocol: openai\n"
            "    model: gpt-5.5\n"
            "    api_key: secret\n"
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="模型锁"):
        TUICodingAgentAdapter(config, "CloudVisit").validate()


def test_codex_command_is_explicit_and_ephemeral() -> None:
    command = CodexCLIAdapter("codex").build_command(Path("C:/repo"), timeout_seconds=10)
    assert "--json" in command
    assert "--ephemeral" in command
    assert command[command.index("-m") + 1] == EXPECTED_MODEL


def test_validated_codex_adapter_uses_the_same_supported_command() -> None:
    from tuicodingagent.eval.adapters_live import CodexCLIAdapter as ValidatedCodexCLIAdapter

    command = ValidatedCodexCLIAdapter("codex").build_command(Path("C:/repo"), timeout_seconds=10)
    assert "--approve-for-me" in command
    assert "--ask-for-approval" not in command


def test_failure_analysis_classifies_target_failure_and_tool_error(tmp_path: Path) -> None:
    from tuicodingagent.eval.swebench_analysis import analyze_run

    artifact = tmp_path / "tuicodingagent" / "demo__repo-1"
    artifact.mkdir(parents=True)
    (artifact / "result.json").write_text(
        json.dumps({"instance_id": "demo__repo-1", "status": "completed"}), encoding="utf-8"
    )
    (artifact / "final.patch").write_text("diff --git a/a b/a\n", encoding="utf-8")
    (artifact / "trajectory.jsonl").write_text(
        json.dumps({"tool_result": {"success": False, "error": "编辑冲突"}}) + "\n",
        encoding="utf-8",
    )
    report = tmp_path / "eval-tuicodingagent" / "demo__repo-1"
    report.mkdir(parents=True)
    (report / "report.json").write_text(
        json.dumps(
            {
                "resolved": False,
                "FAIL_TO_PASS": {"failure": ["target"]},
                "PASS_TO_PASS": {"failure": []},
            }
        ),
        encoding="utf-8",
    )
    rows = analyze_run(tmp_path, ("tuicodingagent",))
    assert rows[0].reason == "目标测试未通过（未修对）"
    assert rows[0].suspicious_trajectory == "编辑冲突"


def test_report_zero_resolved_uses_na() -> None:
    result = SystemResult(
        benchmark="swebench_live",
        version="v1",
        split="canary",
        instance_id="x",
        base_commit="abc",
        agent="codex",
        model=EXPECTED_MODEL,
        status="failed",
        resolved=None,
        baseline_passed=None,
        after_passed=None,
        fail_to_pass_total=0,
        fail_to_pass_passed=0,
        pass_to_pass_total=0,
        pass_to_pass_passed=0,
        input_tokens=0,
        output_tokens=0,
        cached_input_tokens=0,
        total_tokens=0,
        elapsed_seconds=1,
        tool_calls=0,
        tool_failures=0,
        retries=0,
        timeout=False,
        error="失败",
    )
    report = _report([result])
    assert "N/A" in report
    assert "Resolved Rate / Pass@1" in report
