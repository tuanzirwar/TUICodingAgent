"""供模型按需激活 Skill 的系统级只读工具。"""

from __future__ import annotations

from typing import Protocol

from ..models import ToolCall, ToolDefinition, ToolResult
from ..skills.loader import SkillLoader
from .base import require_string


class SkillRuntime(Protocol):
    def activate_skill(self, name: str, body: str, tools: tuple[str, ...] = ()) -> None: ...


class LoadSkillTool:
    read_only = True
    definition = ToolDefinition(
        "load_skill",
        "按名称加载并激活一个可用 Skill 的完整 SOP。",
        {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    )

    def __init__(self, loader: SkillLoader, runtime: SkillRuntime | None = None) -> None:
        self.loader = loader
        self.runtime = runtime

    def set_runtime(self, runtime: SkillRuntime) -> None:
        self.runtime = runtime

    async def execute(self, call: ToolCall) -> ToolResult:
        name = require_string(call, "name").casefold()
        skill = self.loader.get(name)
        if skill is None:
            return ToolResult(call.id, call.name, False, error=f"未知 Skill：{name}")
        if self.runtime is None:
            return ToolResult(call.id, call.name, False, error="Skill 运行时尚未就绪")
        self.runtime.activate_skill(skill.name, skill.prompt_body, skill.tools)
        return ToolResult(
            call.id,
            call.name,
            True,
            output=f"Skill '{skill.name}' activated. SOP pinned to environment context.",
        )
