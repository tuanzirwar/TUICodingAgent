"""权限规则与 Hook 条件共用的结构化文本匹配器。"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any, Literal

MatchType = Literal["exact", "glob", "regex", "not"]


@dataclass(frozen=True, slots=True)
class Matcher:
    type: MatchType
    value: str = ""
    inner: Matcher | None = None
    _regex: re.Pattern[str] | None = field(default=None, repr=False, compare=False)

    def matches(self, candidate: object) -> bool:
        text = "" if candidate is None else str(candidate)
        if self.type == "not":
            assert self.inner is not None
            return not self.inner.matches(text)
        if self.type == "exact":
            return text == self.value
        if self.type == "regex":
            assert self._regex is not None
            return self._regex.search(text) is not None
        return fnmatch.fnmatchcase(text, self.value)


def parse_match(value: Any) -> Matcher:
    """解析 Hook 结构化 matcher。"""

    if not isinstance(value, dict):
        raise ValueError("match 必须是对象")
    kind = value.get("type", "glob")
    if kind == "not":
        if "inner" not in value:
            raise ValueError("not matcher 缺少 inner")
        return Matcher("not", inner=parse_match(value["inner"]))
    if kind not in {"exact", "glob", "regex"}:
        raise ValueError(f"未知 matcher 类型：{kind}")
    raw = value.get("value")
    if not isinstance(raw, (str, int, float, bool)):
        raise ValueError(f"{kind} matcher 缺少 value")
    text = str(raw)
    compiled = None
    if kind == "regex":
        try:
            compiled = re.compile(text)
        except re.error as exc:
            raise ValueError(f"正则表达式无效：{exc}") from exc
    return Matcher(kind, text, _regex=compiled)


def parse_permission_match(pattern: str) -> Matcher:
    """解析权限简洁串中的 =、~、! 前缀。"""

    if pattern.startswith("!"):
        if len(pattern) == 1:
            raise ValueError("反向 matcher 缺少内部表达式")
        return Matcher("not", inner=parse_permission_match(pattern[1:]))
    if pattern.startswith("="):
        return Matcher("exact", pattern[1:])
    if pattern.startswith("~"):
        return parse_match({"type": "regex", "value": pattern[1:]})
    return Matcher("glob", pattern)
