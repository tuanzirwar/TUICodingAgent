"""权限系统内部不可变模型。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..matching import Matcher
from ..models import PermissionDecision


class ToolCategory(StrEnum):
    READ_ONLY = "read_only"
    FILE_WRITE = "file_write"
    COMMAND = "command"


@dataclass(frozen=True, slots=True)
class PermissionRule:
    tool_name: str
    pattern: Matcher | None
    decision: PermissionDecision
    source: str = ""


@dataclass(frozen=True, slots=True)
class PermissionEvaluation:
    decision: PermissionDecision
    reason: str
    exact_rule: str = ""


@dataclass(frozen=True, slots=True)
class PermissionSettings:
    rules: tuple[PermissionRule, ...]
    default_mode: str | None = None
