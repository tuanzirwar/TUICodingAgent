"""友好工具名规则解析、目标提取与合并裁决。"""

from __future__ import annotations

import re
from pathlib import Path

from ..matching import parse_permission_match
from ..models import PermissionDecision, ToolCall
from .models import PermissionRule, ToolCategory

FRIENDLY_TO_INTERNAL = {
    "Bash": "bash",
    "Read": "read_file",
    "Write": "write_file",
    "Edit": "edit_file",
    "Glob": "glob",
    "Grep": "grep",
    "InstallSkill": "install_skill",
    "Agent": "Agent",
    "TaskList": "TaskList",
    "TaskGet": "TaskGet",
    "TaskStop": "TaskStop",
    "SendMessage": "SendMessage",
    "TeamCreate": "TeamCreate",
    "TeamDelete": "TeamDelete",
    "TaskCreate": "TaskCreate",
    "TaskUpdate": "TaskUpdate",
}
INTERNAL_TO_FRIENDLY = {value: key for key, value in FRIENDLY_TO_INTERNAL.items()}

TOOL_CATEGORIES = {
    "read_file": ToolCategory.READ_ONLY,
    "glob": ToolCategory.READ_ONLY,
    "grep": ToolCategory.READ_ONLY,
    "write_file": ToolCategory.FILE_WRITE,
    "edit_file": ToolCategory.FILE_WRITE,
    "bash": ToolCategory.COMMAND,
    "install_skill": ToolCategory.FILE_WRITE,
    "Agent": ToolCategory.READ_ONLY,
    "TaskList": ToolCategory.READ_ONLY,
    "TaskGet": ToolCategory.READ_ONLY,
    "TaskStop": ToolCategory.READ_ONLY,
    "SendMessage": ToolCategory.READ_ONLY,
    "TeamCreate": ToolCategory.FILE_WRITE,
    "TeamDelete": ToolCategory.FILE_WRITE,
    "TaskCreate": ToolCategory.FILE_WRITE,
    "TaskUpdate": ToolCategory.FILE_WRITE,
}

_RULE_PATTERN = re.compile(r"^([A-Za-z]+)(?:\((.*)\))?$")
_DECISION_ORDER = {
    PermissionDecision.ALLOW: 1,
    PermissionDecision.ASK: 2,
    PermissionDecision.DENY: 3,
}


def parse_rule(text: str, decision: PermissionDecision, source: str = "") -> PermissionRule:
    match = _RULE_PATTERN.fullmatch(text.strip())
    if match is None or match.group(1) not in FRIENDLY_TO_INTERNAL:
        raise ValueError(f"无效权限规则：{text}")
    raw_pattern = match.group(2)
    matcher = parse_permission_match(raw_pattern) if raw_pattern is not None else None
    return PermissionRule(match.group(1), matcher, decision, source)


def decide_rules(
    call: ToolCall,
    target: str,
    rules: tuple[PermissionRule, ...],
) -> PermissionDecision | None:
    matched = [rule.decision for rule in rules if rule_matches(rule, call, target)]
    return max(matched, key=_DECISION_ORDER.get) if matched else None


def rule_matches(rule: PermissionRule, call: ToolCall, target: str) -> bool:
    if FRIENDLY_TO_INTERNAL[rule.tool_name] != call.name:
        return False
    if rule.pattern is None:
        return True
    matcher = rule.pattern
    if call.name == "bash" and matcher.type == "glob" and "**" in matcher.value:
        matcher = parse_permission_match(matcher.value.replace("**", "*"))
    return matcher.matches(target)


def call_target(call: ToolCall, workspace: Path) -> str:
    if call.name == "install_skill":
        value = call.arguments.get("url")
        return value if isinstance(value, str) else ""
    if call.name == "bash":
        value = call.arguments.get("command")
        return value if isinstance(value, str) else ""
    value = call.arguments.get("path", "." if call.name in {"glob", "grep"} else "")
    if not isinstance(value, str) or not value:
        return ""
    path = Path(value)
    absolute = path if path.is_absolute() else workspace / path
    try:
        return absolute.resolve(strict=False).relative_to(workspace.resolve()).as_posix()
    except (OSError, ValueError):
        return value.replace("\\", "/")


def exact_rule_for(call: ToolCall, target: str) -> str:
    friendly = INTERNAL_TO_FRIENDLY.get(call.name, call.name)
    return f"{friendly}(={target})"
