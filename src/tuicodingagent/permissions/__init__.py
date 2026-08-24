"""TUICodingAgent 内置工具权限系统。"""

from .config import load_merged_settings, permission_paths, persist_local_allow
from .manager import MODE_FALLBACK, PermissionManager
from .models import PermissionEvaluation, PermissionRule, PermissionSettings, ToolCategory
from .rules import call_target, decide_rules, exact_rule_for, parse_rule, rule_matches

__all__ = [
    "MODE_FALLBACK",
    "PermissionEvaluation",
    "PermissionManager",
    "PermissionRule",
    "PermissionSettings",
    "ToolCategory",
    "call_target",
    "decide_rules",
    "exact_rule_for",
    "load_merged_settings",
    "parse_rule",
    "permission_paths",
    "persist_local_allow",
    "rule_matches",
]
