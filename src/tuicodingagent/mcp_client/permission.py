"""在不修改内置权限包的前提下组合 MCP 动态工具权限。"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..models import PermissionDecision, PermissionMode, ToolCall
from ..permissions import PermissionEvaluation, PermissionManager, permission_paths
from ..permissions.config import persist_local_allow
from ..tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class _MCPRule:
    pattern: str
    decision: PermissionDecision


class MCPPermissionManager:
    """内置工具委托原管理器，MCP 工具按动态元数据分类。"""

    def __init__(
        self,
        base: PermissionManager,
        registry: ToolRegistry,
        rules: tuple[_MCPRule, ...],
    ) -> None:
        self.base = base
        self.registry = registry
        self.rules = rules

    @classmethod
    def load(cls, workspace: Path, registry: ToolRegistry) -> MCPPermissionManager:
        return cls(
            PermissionManager.load(workspace),
            registry,
            _load_mcp_rules(permission_paths(workspace)),
        )

    @property
    def mode(self) -> PermissionMode:
        return self.base.mode

    def set_mode(self, mode: PermissionMode) -> None:
        self.base.set_mode(mode)

    def evaluate(self, call: ToolCall) -> PermissionEvaluation:
        if not call.name.startswith("mcp__"):
            return self.base.evaluate(call)
        tool = self.registry.get(call.name)
        if call.parse_error or tool is None:
            return PermissionEvaluation(
                PermissionDecision.DENY,
                "MCP 工具未知或参数无法解析，已按安全默认处理",
                call.name,
            )
        matched = [
            rule.decision for rule in self.rules if fnmatch.fnmatchcase(call.name, rule.pattern)
        ]
        if matched:
            decision = max(matched, key=_decision_weight)
            return PermissionEvaluation(decision, f"MCP 权限规则裁决：{decision.value}", call.name)
        if self.registry.is_read_only(call.name) or self.mode in {
            PermissionMode.BYPASS,
            PermissionMode.DONT_ASK,
        }:
            decision = PermissionDecision.ALLOW
        else:
            decision = PermissionDecision.ASK
        return PermissionEvaluation(
            decision,
            f"MCP 工具按 {self.mode.value} 模式裁决为 {decision.value}",
            call.name,
        )

    def allow_always(self, exact_rule: str) -> bool:
        if not exact_rule.startswith("mcp__"):
            return self.base.allow_always(exact_rule)
        try:
            persist_local_allow(self.base.local_path, exact_rule)
        except (OSError, UnicodeError):
            return False
        self.rules = (*self.rules, _MCPRule(exact_rule, PermissionDecision.ALLOW))
        return True


def _load_mcp_rules(paths: tuple[Path, Path, Path]) -> tuple[_MCPRule, ...]:
    rules: list[_MCPRule] = []
    for path in paths:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            continue
        if not isinstance(raw, dict):
            continue
        permissions = raw.get("permissions", raw)
        if not isinstance(permissions, dict):
            continue
        for key, decision in (
            ("allow", PermissionDecision.ALLOW),
            ("ask", PermissionDecision.ASK),
            ("deny", PermissionDecision.DENY),
        ):
            values = permissions.get(key, [])
            if not isinstance(values, list):
                continue
            rules.extend(
                _MCPRule(value, decision)
                for value in values
                if isinstance(value, str) and value.startswith("mcp__")
            )
    return tuple(rules)


def _decision_weight(decision: PermissionDecision) -> int:
    return {
        PermissionDecision.ALLOW: 1,
        PermissionDecision.ASK: 2,
        PermissionDecision.DENY: 3,
    }[decision]
