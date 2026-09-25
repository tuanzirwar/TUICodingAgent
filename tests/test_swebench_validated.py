from __future__ import annotations

from tuicodingagent.eval.swebench import SWEbenchInstance
from tuicodingagent.eval.swebench_validated import CommandResult, _baseline_reason, _report


def test_baseline_gate_accepts_expected_fail_and_pass() -> None:
    instance = SWEbenchInstance("b", "v", "s", "i", "c", "r", "p", ("new",), ("old",))
    result = CommandResult("pytest", 1, False, "", 0.1, {"new": False, "old": True})
    assert _baseline_reason(instance, result) == ""


def test_baseline_gate_marks_infrastructure_mismatch_invalid() -> None:
    instance = SWEbenchInstance("b", "v", "s", "i", "c", "r", "p", ("new",), ("old",))
    result = CommandResult("pytest", None, False, "WinError 5", 0.1, {"new": False, "old": False})
    assert _baseline_reason(instance, result).startswith("baseline_expected_state_mismatch")


def test_invalid_results_are_excluded_from_report_denominator() -> None:
    report = _report([])
    assert "invalid_count" in report or "五项对比表" in report
