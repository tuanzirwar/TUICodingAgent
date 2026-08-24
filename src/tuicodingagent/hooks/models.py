"""Hook 系统不可变模型。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..matching import Matcher


class HookEvent(StrEnum):
    SESSION_START = "SessionStart"
    SESSION_END = "SessionEnd"
    SESSION_RESUME = "SessionResume"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    STOP = "Stop"
    PRE_USER_MESSAGE = "PreUserMessage"
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    PRE_COMPACT = "PreCompact"
    POST_COMPACT = "PostCompact"
    NOTIFICATION = "Notification"


BLOCKING_EVENTS = frozenset({HookEvent.PRE_TOOL_USE, HookEvent.USER_PROMPT_SUBMIT})


@dataclass(frozen=True, slots=True)
class HookCondition:
    field: str
    matcher: Matcher


@dataclass(frozen=True, slots=True)
class HookPredicate:
    operator: str
    conditions: tuple[HookCondition, ...]

    def matches(self, payload: dict[str, Any]) -> bool:
        values = (
            condition.matcher.matches(_field_value(payload, condition.field))
            for condition in self.conditions
        )
        return all(values) if self.operator == "all_of" else any(values)


@dataclass(frozen=True, slots=True)
class HookAction:
    type: str
    options: dict[str, Any]


@dataclass(frozen=True, slots=True)
class HookRule:
    name: str
    event: HookEvent
    predicate: HookPredicate | None
    action: HookAction
    only_once: bool
    async_run: bool
    timeout: float
    source: Path


@dataclass(frozen=True, slots=True)
class HookDispatch:
    blocked: bool = False
    hook_name: str = ""
    reason: str = ""


def _field_value(payload: dict[str, Any], field: str) -> object:
    value: object = payload
    for part in field.split("."):
        if not isinstance(value, dict) or part not in value:
            return ""
        value = value[part]
    return value
