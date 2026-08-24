"""五层权限判定流水线。"""

from __future__ import annotations

from pathlib import Path

from ..models import PermissionDecision, PermissionMode, ToolCall
from .blacklist import blacklist_denial
from .config import load_merged_settings, permission_paths, persist_local_allow
from .models import PermissionEvaluation, PermissionRule, ToolCategory
from .rules import TOOL_CATEGORIES, call_target, decide_rules, exact_rule_for, parse_rule
from .sandbox import sandbox_denial

MODE_FALLBACK = {
    PermissionMode.DEFAULT: {
        ToolCategory.READ_ONLY: PermissionDecision.ALLOW,
        ToolCategory.FILE_WRITE: PermissionDecision.ASK,
        ToolCategory.COMMAND: PermissionDecision.ASK,
    },
    PermissionMode.ACCEPT_EDITS: {
        ToolCategory.READ_ONLY: PermissionDecision.ALLOW,
        ToolCategory.FILE_WRITE: PermissionDecision.ALLOW,
        ToolCategory.COMMAND: PermissionDecision.ASK,
    },
    PermissionMode.PLAN: {
        ToolCategory.READ_ONLY: PermissionDecision.ALLOW,
        ToolCategory.FILE_WRITE: PermissionDecision.ASK,
        ToolCategory.COMMAND: PermissionDecision.ASK,
    },
    PermissionMode.BYPASS: {
        ToolCategory.READ_ONLY: PermissionDecision.ALLOW,
        ToolCategory.FILE_WRITE: PermissionDecision.ALLOW,
        ToolCategory.COMMAND: PermissionDecision.ALLOW,
    },
    PermissionMode.DONT_ASK: {
        ToolCategory.READ_ONLY: PermissionDecision.ALLOW,
        ToolCategory.FILE_WRITE: PermissionDecision.ALLOW,
        ToolCategory.COMMAND: PermissionDecision.ALLOW,
    },
}


class PermissionManager:
    def __init__(
        self,
        workspace: Path,
        *,
        rules: tuple[PermissionRule, ...] = (),
        mode: PermissionMode = PermissionMode.DEFAULT,
        local_path: Path | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.rules = rules
        self.mode = mode
        self.local_path = local_path or permission_paths(self.workspace)[2]

    @classmethod
    def load(cls, workspace: Path) -> PermissionManager:
        paths = permission_paths(workspace)
        settings = load_merged_settings(workspace, paths)
        try:
            mode = PermissionMode(settings.default_mode or PermissionMode.DEFAULT)
        except ValueError:
            mode = PermissionMode.DEFAULT
        return cls(workspace, rules=settings.rules, mode=mode, local_path=paths[2])

    def evaluate(self, call: ToolCall) -> PermissionEvaluation:
        if call.parse_error:
            return PermissionEvaluation(
                PermissionDecision.DENY,
                "权限拒绝：工具参数无法解析，已按安全默认处理",
            )
        category = TOOL_CATEGORIES.get(call.name)
        if category is None:
            return PermissionEvaluation(
                PermissionDecision.DENY,
                "权限拒绝：未知工具或类别不明，已按安全默认处理",
            )
        target = call_target(call, self.workspace)
        if category == ToolCategory.COMMAND:
            if not target:
                return PermissionEvaluation(
                    PermissionDecision.DENY,
                    "权限拒绝：命令参数无效",
                )
            denied = blacklist_denial(target)
            if denied:
                return PermissionEvaluation(PermissionDecision.DENY, denied)
        elif call.name != "install_skill":
            denied = sandbox_denial(call, self.workspace)
            if denied:
                return PermissionEvaluation(PermissionDecision.DENY, denied)

        rule_decision = decide_rules(call, target, self.rules)
        exact = exact_rule_for(call, target)
        if rule_decision is not None:
            return PermissionEvaluation(
                rule_decision,
                f"权限规则裁决：{rule_decision.value}",
                exact,
            )
        fallback = MODE_FALLBACK[self.mode][category]
        return PermissionEvaluation(
            fallback,
            f"权限模式 {self.mode.value} 对此类操作要求 {fallback.value}",
            exact,
        )

    def allow_always(self, exact_rule: str) -> bool:
        try:
            persist_local_allow(self.local_path, exact_rule)
        except (OSError, UnicodeError):
            # 受限文件系统中降级为“允许本次”，不让审批流程崩溃。
            return False
        rule = parse_rule(exact_rule, PermissionDecision.ALLOW, str(self.local_path))
        self.rules = (*self.rules, rule)
        return True

    def set_mode(self, mode: PermissionMode) -> None:
        self.mode = mode
