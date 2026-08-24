"""Skill 的共享对话与隔离对话执行器。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from ..models import ChatMessage, PermissionMode, ProviderConfig, RuntimeEventType
from ..permissions import PermissionManager
from ..providers.base import ChatProvider
from ..tools.registry import ToolRegistry
from .loader import SkillLoader
from .parser import SkillDef, substitute_arguments

if TYPE_CHECKING:
    from ..subagent.service import Service as SubAgentService


class SkillExecutor:
    def __init__(
        self,
        loader: SkillLoader,
        runtime,
        registry: ToolRegistry,
        workspace: Path,
        provider_factory: Callable[[ProviderConfig], ChatProvider],
        providers: Sequence[ProviderConfig],
        subagent_service: SubAgentService | None = None,
    ) -> None:
        self.loader = loader
        self.runtime = runtime
        self.registry = registry
        self.workspace = workspace.resolve()
        self.provider_factory = provider_factory
        self.providers = tuple(providers)
        self.subagent_service = subagent_service

    def execute_inline(self, skill: SkillDef, args: str) -> str:
        """把渲染后的 SOP 钉入主运行时，返回触发用户消息。"""

        body = substitute_arguments(skill.prompt_body, args)
        self.runtime.activate_skill(skill.name, body, skill.tools)
        suffix = f"，参数：{args}" if args else ""
        return f"请按照已激活的 Skill `{skill.name}` 完成任务{suffix}。"

    async def execute_fork(self, skill: SkillDef, args: str) -> str:
        """创建全新运行时执行 Skill，只回流最终累计文本。"""

        if self.subagent_service is not None:
            from ..subagent.definitions import Definition as AgentDefinition

            body = substitute_arguments(skill.prompt_body, args)
            prompt = f"执行 Skill `{skill.name}`"
            if args:
                prompt += f"，参数：{args}"
            definition = AgentDefinition(
                name=f"skill-fork-{skill.name}"[:32],
                description=f"Skill {skill.name} 隔离执行",
                prompt=body,
                tools=skill.tools,
                model=skill.model or "inherit",
                max_turns=25,
                permission_mode=PermissionMode.BYPASS,
                source=str(skill.source_path),
            )
            return await self.subagent_service.run_definition(
                definition,
                prompt,
                seed=self._fork_history(skill.context),
            )

        from ..runtime import ConversationRuntime

        config = self._select_provider(skill.model)
        provider = self.provider_factory(config)
        permission = PermissionManager(self.workspace, mode=PermissionMode.BYPASS)
        fork = ConversationRuntime(
            provider,
            tool_registry=self.registry,
            workspace=self.workspace,
            permission_manager=permission,
        )
        fork.set_skill_catalog(self.runtime.skill_catalog)
        body = substitute_arguments(skill.prompt_body, args)
        fork.activate_skill(skill.name, body, skill.tools)
        fork.seed_history(self._fork_history(skill.context))
        prompt = f"执行 Skill `{skill.name}`"
        if args:
            prompt += f"，参数：{args}"
        chunks: list[str] = []
        try:
            async for event in fork.chat(prompt):
                if event.type == RuntimeEventType.TEXT_DELTA or (
                    event.type == RuntimeEventType.COMPLETED and not chunks
                ):
                    chunks.append(event.text)
                elif event.type == RuntimeEventType.FAILED:
                    raise RuntimeError(event.text)
        finally:
            await fork.close()
        return "".join(chunks).strip() or "Skill 已完成，但没有返回文本。"

    def _fork_history(self, context: str) -> tuple[ChatMessage, ...]:
        history = self.runtime.history
        if context == "none":
            return ()
        selected = history[-5:] if context == "recent" else history
        if not selected:
            return ()
        lines = ["主对话上下文摘要（按消息拼接）："]
        for message in selected:
            detail = message.content.strip()
            if message.tool_calls:
                detail += " 工具调用：" + ", ".join(call.name for call in message.tool_calls)
            if message.tool_results:
                detail += " 工具结果：" + ", ".join(result.name for result in message.tool_results)
            lines.append(f"- {message.role}: {detail}".rstrip())
        return (ChatMessage("user", "\n".join(lines)),)

    def _select_provider(self, requested: str | None) -> ProviderConfig:
        if requested:
            for config in self.providers:
                if requested in {config.name, config.model}:
                    return config
            raise ValueError(f"Skill 指定的模型未配置：{requested}")
        return self.runtime.provider.config
