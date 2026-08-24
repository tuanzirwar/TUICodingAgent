"""SubAgent 角色定义、校验与多来源目录。"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from ..models import PermissionMode

NAME_RE = re.compile(r"^[a-z0-9-]{1,32}$")
MODELS = {"haiku", "sonnet", "opus", "inherit"}


@dataclass(frozen=True, slots=True)
class Definition:
    name: str
    description: str
    prompt: str
    tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    model: str = "inherit"
    max_turns: int = 25
    permission_mode: PermissionMode = PermissionMode.DEFAULT
    background: bool = False
    isolation: str = ""
    source: str = ""


def parse_definition(text: str, *, source: str, strict: bool = False) -> Definition:
    if not text.startswith("---\n"):
        raise ValueError("缺少 YAML frontmatter")
    marker = text.find("\n---\n", 4)
    if marker < 0:
        raise ValueError("frontmatter 未闭合")
    raw = yaml.safe_load(text[4:marker])
    if not isinstance(raw, dict):
        raise ValueError("frontmatter 必须是映射")
    name = _required(raw, "name")
    description = _required(raw, "description")
    if not NAME_RE.fullmatch(name):
        raise ValueError("name 仅允许小写字母、数字和连字符，长度 1-32")
    model = raw.get("model", "inherit")
    if model not in MODELS:
        _fallback_warning(source, f"未知 model {model!r}，已回退 inherit", strict)
        model = "inherit"
    raw_mode = raw.get("permissionMode", "default")
    try:
        mode = PermissionMode(raw_mode)
    except (TypeError, ValueError):
        _fallback_warning(source, f"未知 permissionMode {raw_mode!r}，已回退 default", strict)
        mode = PermissionMode.DEFAULT
    max_turns = raw.get("maxTurns", 25)
    if not isinstance(max_turns, int) or isinstance(max_turns, bool) or max_turns <= 0:
        raise ValueError("maxTurns 必须是正整数")
    background = raw.get("background", False)
    if not isinstance(background, bool):
        raise ValueError("background 必须是布尔值")
    isolation = raw.get("isolation", "")
    if isolation not in {"", "worktree"}:
        _fallback_warning(source, f"未知 isolation {isolation!r}，已回退为空", strict)
        isolation = ""
    prompt = text[marker + 5 :].strip()
    if not prompt:
        raise ValueError("角色系统提示不能为空")
    return Definition(
        name=name,
        description=description,
        prompt=prompt,
        tools=_string_list(raw, "tools"),
        disallowed_tools=_string_list(raw, "disallowedTools"),
        model=model,
        max_turns=max_turns,
        permission_mode=mode,
        background=background,
        isolation=isolation,
        source=source,
    )


class Catalog:
    """项目 > 用户 > 内置 > 插件占位的角色目录。"""

    def __init__(self, workspace: Path, user_root: Path | None = None) -> None:
        self.workspace = workspace.resolve()
        self.user_root = (user_root or Path.home() / ".tuicodingagent" / "agents").resolve()
        self._definitions: dict[str, Definition] = {}

    @property
    def definitions(self) -> tuple[Definition, ...]:
        return tuple(sorted(self._definitions.values(), key=lambda item: item.name))

    def resolve(self, name: str) -> Definition | None:
        return self._definitions.get(name.casefold())

    def load_all(self) -> tuple[Definition, ...]:
        definitions: dict[str, Definition] = {}
        # 低优先级先加载，高优先级随后覆盖。
        for item in self._load_builtin():
            definitions[item.name.casefold()] = item
        for root in (self.user_root, self.workspace / ".tuicodingagent" / "agents"):
            for item in self._load_directory(root):
                definitions[item.name.casefold()] = item
        self._definitions = definitions
        return self.definitions

    def catalog_text(self) -> str:
        lines = ["可用 SubAgent 角色："]
        lines.extend(f"- {item.name}: {item.description}" for item in self.definitions)
        return "\n".join(lines)

    def _load_builtin(self) -> list[Definition]:
        loaded: list[Definition] = []
        root = resources.files("tuicodingagent.subagent.builtin")
        for item in sorted(root.iterdir(), key=lambda path: path.name):
            if item.name.endswith(".md"):
                text = item.read_text(encoding="utf-8")
                loaded.append(parse_definition(text, source=f"builtin:{item.name}", strict=True))
        return loaded

    def _load_directory(self, root: Path) -> list[Definition]:
        if not root.is_dir():
            return []
        loaded: list[Definition] = []
        for path in sorted(root.glob("*.md")):
            try:
                loaded.append(parse_definition(path.read_text(encoding="utf-8"), source=str(path)))
            except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
                print(f"Agent 定义 {path} 加载失败：{exc}", file=sys.stderr)
        return loaded


def _required(raw: dict[str, Any], name: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串")
    return value.strip()


def _string_list(raw: dict[str, Any], name: str) -> tuple[str, ...]:
    if name not in raw:
        return ()
    value = raw[name]
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{name} 必须是字符串列表")
    return tuple(value)


def _fallback_warning(source: str, message: str, strict: bool) -> None:
    if strict:
        raise ValueError(message)
    print(f"Agent 定义 {source}：{message}", file=sys.stderr)


__all__ = ["Catalog", "Definition", "parse_definition"]
