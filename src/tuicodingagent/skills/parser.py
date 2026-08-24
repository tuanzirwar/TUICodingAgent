"""Skill frontmatter 与 Markdown 正文解析。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

SkillMode = Literal["inline", "fork"]
SkillContext = Literal["full", "recent", "none"]
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True, slots=True)
class SkillDef:
    """一份已校验并保留来源信息的 Skill 定义。"""

    name: str
    description: str
    prompt_body: str
    tools: tuple[str, ...]
    mode: SkillMode
    model: str | None
    context: SkillContext
    source_path: Path
    is_directory: bool


def parse_skill_file(path: Path) -> SkillDef:
    """解析单文件或目录入口 `SKILL.md`。"""

    source = path / "SKILL.md" if path.is_dir() else path
    text = source.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError("缺少 YAML frontmatter")
    try:
        frontmatter, body = text[4:].split("\n---\n", 1)
    except ValueError as exc:
        raise ValueError("frontmatter 未闭合") from exc
    data = yaml.safe_load(frontmatter)
    if not isinstance(data, dict):
        raise ValueError("frontmatter 必须是映射")
    name = data.get("name")
    description = data.get("description")
    if not isinstance(name, str) or not _NAME.fullmatch(name.casefold()):
        raise ValueError("name 必须是小写字母、数字、下划线或连字符")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description 必须是非空字符串")
    mode = data.get("mode", "inline")
    context = data.get("context", "full")
    if mode not in ("inline", "fork"):
        raise ValueError("mode 必须是 inline 或 fork")
    if context not in ("full", "recent", "none"):
        raise ValueError("context 必须是 full、recent 或 none")
    raw_tools = data.get("tools", data.get("allowed-tools", []))
    if raw_tools is None:
        raw_tools = []
    if not isinstance(raw_tools, list) or not all(
        isinstance(item, str) and item for item in raw_tools
    ):
        raise ValueError("tools 必须是字符串列表")
    model = data.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ValueError("model 必须是非空字符串")
    if not body.strip():
        raise ValueError("Skill 正文不能为空")
    return SkillDef(
        name.casefold(),
        description.strip(),
        body.strip(),
        tuple(dict.fromkeys(raw_tools)),
        mode,
        model.strip() if isinstance(model, str) else None,
        context,
        source.resolve(),
        path.is_dir(),
    )


def substitute_arguments(prompt_body: str, args: str) -> str:
    """仅替换约定的参数占位符，未声明时保持正文原样。"""

    return prompt_body.replace("$ARGUMENTS", args) if "$ARGUMENTS" in prompt_body else prompt_body
