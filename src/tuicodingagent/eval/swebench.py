"""SWE-bench Live 实例的读取、规范化和选择。"""

from __future__ import annotations

import gzip
import json
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class SWEbenchInstance:
    """评测系统内部使用的稳定实例格式。"""

    benchmark: str
    version: str
    split: str
    instance_id: str
    base_commit: str
    repo: str
    problem_statement: str
    fail_to_pass: tuple[str, ...] = ()
    pass_to_pass: tuple[str, ...] = ()
    test_command: str = ""

    def as_manifest(self) -> dict[str, object]:
        """返回不含运行时秘密的 manifest 字段。"""

        return asdict(self) | {
            "fail_to_pass": list(self.fail_to_pass),
            "pass_to_pass": list(self.pass_to_pass),
        }


class DatasetError(ValueError):
    """数据集格式或来源错误。"""


def load_instances(
    source: str | Path, *, default_benchmark: str = "swebench_live"
) -> list[SWEbenchInstance]:
    """读取本地 JSON/JSONL 或 HTTP(S) JSON/JSONL 数据源。"""

    source_text = str(source)
    if source_text.startswith(("http://", "https://")):
        with urllib.request.urlopen(source_text, timeout=30) as response:  # noqa: S310
            data = response.read()
        name = source_text.rsplit("/", 1)[-1]
    else:
        path = Path(source)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise DatasetError(f"无法读取数据集：{path}") from exc
        name = path.name
    if name.endswith(".gz"):
        try:
            data = gzip.decompress(data)
        except OSError as exc:
            raise DatasetError("数据集 gzip 解压失败") from exc
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DatasetError("数据集必须是 UTF-8 JSON/JSONL") from exc
    rows = _decode_rows(text, name)
    instances = [_normalize(row, default_benchmark, index) for index, row in enumerate(rows, 1)]
    if not instances:
        raise DatasetError("数据集没有实例")
    return instances


def select_instances(
    instances: list[SWEbenchInstance],
    *,
    instance_ids: set[str] | None = None,
    sample: int | None = None,
) -> list[SWEbenchInstance]:
    """按实例 ID 和稳定的文件顺序选择任务。"""

    selected = instances
    if instance_ids:
        selected = [item for item in selected if item.instance_id in instance_ids]
        found = {item.instance_id for item in selected}
        missing = sorted(instance_ids - found)
        if missing:
            raise DatasetError("找不到 instance_id：" + ", ".join(missing))
    if sample is not None:
        if sample <= 0:
            raise DatasetError("sample 必须大于 0")
        selected = selected[:sample]
    if not selected:
        raise DatasetError("筛选后没有实例")
    return selected


def _decode_rows(text: str, name: str) -> list[dict[str, Any]]:
    try:
        if name.lower().endswith((".jsonl", ".jsonl.gz", ".ndjson")):
            value: Any = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            value = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise DatasetError(f"无法解析数据集 JSON/JSONL：{exc}") from exc
    if isinstance(value, dict):
        for key in ("instances", "data", "tasks", "rows"):
            if isinstance(value.get(key), list):
                value = value[key]
                break
        else:
            value = [value]
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise DatasetError("数据集顶层必须是对象列表，或包含 instances/data/tasks/rows 列表")
    return value


def _normalize(row: dict[str, Any], default_benchmark: str, index: int) -> SWEbenchInstance:
    def text_field(*names: str, default: str = "") -> str:
        for name in names:
            value = row.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return default

    instance_id = text_field("instance_id", "id")
    base_commit = text_field("base_commit", "base_sha", "base_commit_sha")
    repo = text_field("repo", "repository", "repo_url")
    problem = text_field("problem_statement", "problem", "issue", "prompt")
    if not instance_id or not base_commit or not repo or not problem:
        missing = [
            name
            for name, value in (
                ("instance_id", instance_id),
                ("base_commit", base_commit),
                ("repo", repo),
                ("problem_statement", problem),
            )
            if not value
        ]
        raise DatasetError(f"第 {index} 个实例缺少必要字段：{', '.join(missing)}")

    benchmark = text_field("benchmark", default=default_benchmark)
    version = text_field("version", "benchmark_version", default="unknown")
    split = text_field("split", default="unknown")
    return SWEbenchInstance(
        benchmark=benchmark,
        version=version,
        split=split,
        instance_id=instance_id,
        base_commit=base_commit,
        repo=repo,
        problem_statement=problem,
        fail_to_pass=_string_tuple(row.get("FAIL_TO_PASS", row.get("fail_to_pass", ()))),
        pass_to_pass=_string_tuple(row.get("PASS_TO_PASS", row.get("pass_to_pass", ()))),
        test_command=text_field("test_command", "evaluator_command"),
    )


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            decoded = [value]
        value = decoded
    if not isinstance(value, (list, tuple)):
        raise DatasetError("FAIL_TO_PASS/PASS_TO_PASS 必须是字符串列表")
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


__all__ = ["DatasetError", "SWEbenchInstance", "load_instances", "select_instances"]
