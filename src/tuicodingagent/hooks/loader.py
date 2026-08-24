"""项目级与用户级 Hook YAML 加载和集中校验。"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

from ..matching import parse_match
from .models import (
    BLOCKING_EVENTS,
    HookAction,
    HookCondition,
    HookEvent,
    HookPredicate,
    HookRule,
)

_DURATION = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|m)$")


def hook_paths(workspace: Path, user_home: Path | None = None) -> tuple[Path, Path]:
    return (
        workspace / ".tuicodingagent" / "hooks.yaml",
        (user_home or Path.home()) / ".tuicodingagent" / "hooks.yaml",
    )


def load_hooks(
    workspace: Path, paths: tuple[Path, Path] | None = None
) -> tuple[tuple[HookRule, ...], tuple[Path, ...]]:
    rules: list[HookRule] = []
    sources: list[Path] = []
    names: set[str] = set()
    for path in paths or hook_paths(workspace):
        if not path.is_file():
            continue
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            print(f"hooks file {path} parse failed: {exc}", file=sys.stderr)
            continue
        if not isinstance(raw, dict) or not isinstance(raw.get("hooks"), list):
            print(
                f"hooks file {path} parse failed: top-level hooks must be a list",
                file=sys.stderr,
            )
            continue
        sources.append(path.resolve())
        for entry in raw["hooks"]:
            name = entry.get("name") if isinstance(entry, dict) else "<unknown>"
            try:
                rule = _parse_rule(entry, path)
                if rule.name in names:
                    raise ValueError("duplicate name")
            except ValueError as exc:
                print(f'hook "{name}": {exc}, skipped', file=sys.stderr)
                continue
            names.add(rule.name)
            rules.append(rule)
    return tuple(rules), tuple(sources)


def _parse_rule(raw: Any, source: Path) -> HookRule:
    if not isinstance(raw, dict):
        raise ValueError("hook must be an object")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name is required")
    event_text = raw.get("event")
    try:
        event = HookEvent(event_text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'unknown event "{event_text}"') from exc
    predicate = _parse_predicate(raw.get("if"))
    action = _parse_action(raw.get("action"))
    only_once = raw.get("only_once", False)
    async_run = raw.get("async", False)
    if not isinstance(only_once, bool) or not isinstance(async_run, bool):
        raise ValueError("only_once and async must be booleans")
    if async_run and event in BLOCKING_EVENTS:
        raise ValueError("async not allowed for blocking events")
    timeout = _parse_timeout(raw.get("timeout", "30s"))
    return HookRule(
        name.strip(), event, predicate, action, only_once, async_run, timeout, source.resolve()
    )


def _parse_predicate(raw: Any) -> HookPredicate | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("if must be an object")
    operators = [key for key in ("all_of", "any_of") if key in raw]
    if len(operators) != 1 or len(raw) != 1:
        raise ValueError("if must contain exactly one of all_of or any_of")
    operator = operators[0]
    entries = raw[operator]
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{operator} must be a non-empty list")
    conditions: list[HookCondition] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"field", "match"}:
            raise ValueError("condition requires only field and match")
        field = entry["field"]
        if not isinstance(field, str) or not field:
            raise ValueError("condition field must be a non-empty string")
        conditions.append(HookCondition(field, parse_match(entry["match"])))
    return HookPredicate(operator, tuple(conditions))


def _parse_action(raw: Any) -> HookAction:
    if not isinstance(raw, dict):
        raise ValueError("action is required")
    kind = raw.get("type")
    if kind not in {"shell", "prompt", "http", "subagent"}:
        raise ValueError(f"unknown action type {kind}")
    required = {
        "shell": ("command",),
        "prompt": ("text",),
        "http": ("url",),
        "subagent": ("agent_name", "prompt"),
    }[kind]
    for field in required:
        if not isinstance(raw.get(field), str) or not raw[field]:
            raise ValueError(f"{kind} action requires {field}")
    if kind == "http":
        method = raw.get("method", "POST")
        headers = raw.get("headers", {})
        body = raw.get("body")
        if not isinstance(method, str):
            raise ValueError("http method must be a string")
        if not isinstance(headers, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in headers.items()
        ):
            raise ValueError("http headers must be string pairs")
        if body is not None and not isinstance(body, str):
            raise ValueError("http body must be a string")
    return HookAction(kind, {key: value for key, value in raw.items() if key != "type"})


def _parse_timeout(raw: Any) -> float:
    if not isinstance(raw, str) or (match := _DURATION.fullmatch(raw)) is None:
        raise ValueError("timeout must look like 30s, 500ms, or 2m")
    value = float(match.group(1))
    factor = {"ms": 0.001, "s": 1.0, "m": 60.0}[match.group(2)]
    if value <= 0:
        raise ValueError("timeout must be positive")
    return value * factor
