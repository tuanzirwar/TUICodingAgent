"""带实例级 resume 的 test_patch 评测入口。"""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

from .swebench_system import _safe
from .swebench_testpatch import PatchedInstance, run_testpatch
from .swebench_validated import ValidatedResult


def _saved_result(path: Path) -> ValidatedResult | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        names = {field.name for field in fields(ValidatedResult)}
        return ValidatedResult(**{key: data[key] for key in names})
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def run_testpatch_resumable(
    *,
    dataset: str | Path,
    output_dir: Path,
    options: object,
    setup_command: str,
    sample: int | None = None,
    instance_ids: set[str] | None = None,
    benchmark: str = "swebench_live",
) -> list[ValidatedResult]:
    """复用主 runner；resume 时仅跳过已有完整 result.json 的实例代理组合。"""

    if not getattr(options, "resume", False):
        return run_testpatch(
            dataset=dataset,
            output_dir=output_dir,
            options=options,
            setup_command=setup_command,
            sample=sample,
            instance_ids=instance_ids,
            benchmark=benchmark,
        )

    import tuicodingagent.eval.swebench_testpatch as impl

    original = impl._run_one

    def run_one(
        instance: PatchedInstance,
        name: str,
        adapter: object,
        output: Path,
        current_options: object,
        current_setup: str,
    ) -> ValidatedResult:
        saved = _saved_result(output / name / _safe(instance.instance_id) / "result.json")
        if saved is not None:
            return saved
        return original(instance, name, adapter, output, current_options, current_setup)

    impl._run_one = run_one
    try:
        return run_testpatch(
            dataset=dataset,
            output_dir=output_dir,
            options=options,
            setup_command=setup_command,
            sample=sample,
            instance_ids=instance_ids,
            benchmark=benchmark,
        )
    finally:
        impl._run_one = original


__all__ = ["run_testpatch_resumable"]
