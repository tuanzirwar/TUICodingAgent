"""Skill 三级发现、覆盖与热更新。"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from .parser import SkillDef, parse_skill_file

LOGGER = logging.getLogger(__name__)


class SkillLoader:
    def __init__(
        self,
        workspace: Path,
        *,
        user_root: Path | None = None,
        builtin_root: Path | None = None,
        available_tools: Iterable[str] = (),
    ) -> None:
        self.workspace = workspace.resolve()
        self.user_root = (user_root or Path.home() / ".tuicodingagent" / "skills").resolve()
        self.builtin_root = (builtin_root or Path(__file__).resolve().parent / "builtin").resolve()
        self.available_tools = frozenset(available_tools)
        self._skills: dict[str, SkillDef] = {}
        self._cache: dict[str, SkillDef] = {}

    @property
    def skills(self) -> tuple[SkillDef, ...]:
        return tuple(sorted(self._skills.values(), key=lambda item: item.name))

    def load_all(self) -> tuple[SkillDef, ...]:
        """按项目、用户、内置顺序扫描，首次出现的同名定义获胜。"""

        loaded: dict[str, SkillDef] = {}
        roots = (
            self.workspace / ".tuicodingagent" / "skills",
            self.user_root,
            self.builtin_root,
        )
        for root in roots:
            for source in self._scan_directory(root):
                try:
                    skill = parse_skill_file(source)
                    self._validate_tools(skill)
                except ValueError as exc:
                    if "不存在的工具" in str(exc):
                        raise
                    LOGGER.warning("跳过无效 Skill %s：%s", source, exc)
                    continue
                except (OSError, UnicodeError, UnicodeDecodeError) as exc:
                    LOGGER.warning("跳过无法读取的 Skill %s：%s", source, exc)
                    continue
                loaded.setdefault(skill.name, skill)
        self._skills = loaded
        self._cache.update(loaded)
        return self.skills

    def get(self, name: str) -> SkillDef | None:
        """每次从源文件热重读；失败时保留上次有效缓存。"""

        key = name.casefold().removeprefix("/")
        current = self._skills.get(key)
        if current is None:
            return None
        try:
            source = current.source_path.parent if current.is_directory else current.source_path
            refreshed = parse_skill_file(source)
            if refreshed.name != key:
                raise ValueError("热更新不能修改 Skill 名称")
            self._validate_tools(refreshed)
        except (OSError, UnicodeError, ValueError) as exc:
            LOGGER.warning("Skill %s 热更新失败，沿用缓存：%s", key, exc)
            return self._cache.get(key, current)
        self._skills[key] = refreshed
        self._cache[key] = refreshed
        return refreshed

    def get_source_label(self, skill: SkillDef | str) -> str:
        item = self._skills.get(skill) if isinstance(skill, str) else skill
        if item is None:
            return "unknown"
        source = item.source_path
        project_root = (self.workspace / ".tuicodingagent" / "skills").resolve()
        if source.is_relative_to(project_root):
            return "project"
        if source.is_relative_to(self.user_root):
            return "user"
        if source.is_relative_to(self.builtin_root):
            return "builtin"
        return "unknown"

    def catalog_text(self) -> str:
        lines = ["## Available Skills"]
        lines.extend(f"- {item.name}: {item.description}" for item in self.skills)
        lines.append("需要完整 SOP 时调用只读工具 load_skill，并传入 Skill 名称。")
        return "\n".join(lines)

    @staticmethod
    def _scan_directory(root: Path) -> tuple[Path, ...]:
        if not root.is_dir():
            return ()
        sources: list[Path] = []
        for entry in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
            if (entry.is_file() and entry.suffix.casefold() == ".md") or (
                entry.is_dir() and (entry / "SKILL.md").is_file()
            ):
                sources.append(entry)
        return tuple(sources)

    def _validate_tools(self, skill: SkillDef) -> None:
        missing = sorted(set(skill.tools) - self.available_tools)
        if missing:
            raise ValueError(f"Skill {skill.name} 引用了不存在的工具：{', '.join(missing)}")
