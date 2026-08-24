"""三级权限配置的安全加载与本地精确规则持久化。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

from ..models import PermissionDecision, PermissionMode
from .models import PermissionRule, PermissionSettings
from .rules import parse_rule

PROJECT_DIR = ".tuicodingagent"
PROJECT_FILE = "settings.yaml"
LOCAL_FILE = "settings.local.yaml"


def permission_paths(workspace: Path) -> tuple[Path, Path, Path]:
    return (
        Path.home() / PROJECT_DIR / PROJECT_FILE,
        workspace / PROJECT_DIR / PROJECT_FILE,
        workspace / PROJECT_DIR / LOCAL_FILE,
    )


def load_merged_settings(
    workspace: Path, paths: tuple[Path, Path, Path] | None = None
) -> PermissionSettings:
    selected = paths or permission_paths(workspace)
    loaded = [_load_file(path) for path in selected]
    rules = tuple(rule for settings in loaded for rule in settings.rules)
    default_mode = next(
        (settings.default_mode for settings in reversed(loaded) if settings.default_mode),
        None,
    )
    return PermissionSettings(rules, default_mode)


def persist_local_allow(path: Path, rule_text: str) -> None:
    raw = _read_mapping(path)
    permissions = raw.get("permissions")
    if not isinstance(permissions, dict):
        permissions = {}
        raw["permissions"] = permissions
    allow = permissions.get("allow")
    if not isinstance(allow, list):
        allow = []
        permissions["allow"] = allow
    if rule_text not in allow:
        allow.append(rule_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _load_file(path: Path) -> PermissionSettings:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return PermissionSettings(())
    if not isinstance(raw, dict):
        return PermissionSettings(())
    permissions = raw.get("permissions", raw)
    if not isinstance(permissions, dict):
        return PermissionSettings(())
    rules: list[PermissionRule] = []
    for key, decision in (
        ("allow", PermissionDecision.ALLOW),
        ("ask", PermissionDecision.ASK),
        ("deny", PermissionDecision.DENY),
    ):
        values = permissions.get(key, [])
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, str):
                continue
            try:
                rules.append(parse_rule(value, decision, str(path)))
            except ValueError as exc:
                print(f'rule "{value}" parse failed: {exc}', file=sys.stderr)
                continue
    mode = raw.get("defaultMode")
    try:
        default_mode = PermissionMode(mode).value if isinstance(mode, str) else None
    except ValueError:
        default_mode = None
    return PermissionSettings(tuple(rules), default_mode)


def _read_mapping(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return {}
    return raw if isinstance(raw, dict) else {}
