"""SubAgent 构造、工具过滤和前后台启动服务。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from ..mcp_client.permission import MCPPermissionManager
from ..models import ChatMessage, ProviderConfig
from ..permissions import PermissionManager
from ..providers.base import ChatProvider
from ..runtime import ConversationRuntime
from ..tools.context import with_cwd
from ..tools.registry import ToolRegistry
from ..worktree import Manager as WorktreeManager
from ..worktree import build_worktree_notice
from .definitions import Catalog, Definition
from .fork import build_forked_messages
from .manager import BackgroundTask, Manager

ALL_AGENT_DISALLOWED_TOOLS = frozenset({"Agent"})
CUSTOM_AGENT_DISALLOWED_TOOLS = frozenset()
ASYNC_AGENT_ALLOWED_TOOLS = frozenset(
    {
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "bash",
        "load_skill",
        "install_skill",
    }
)
AUTO_BACKGROUND_SECONDS = 120.0


class Service:
    def __init__(
        self,
        *,
        workspace: Path,
        registry: ToolRegistry,
        catalog: Catalog,
        manager: Manager,
        parent_runtime: ConversationRuntime,
        provider_factory: Callable[[ProviderConfig], ChatProvider],
        providers: Sequence[ProviderConfig],
        background_enabled: bool = True,
        auto_background_seconds: float = AUTO_BACKGROUND_SECONDS,
        worktree_manager: WorktreeManager | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.registry = registry
        self.catalog = catalog
        self.manager = manager
        self.parent_runtime = parent_runtime
        self.provider_factory = provider_factory
        self.providers = tuple(providers)
        self.background_enabled = background_enabled
        self.auto_background_seconds = auto_background_seconds
        self.worktree_manager = worktree_manager
        self.foreground_task: BackgroundTask | None = None
        self._manual_background = asyncio.Event()

    async def launch(
        self,
        *,
        prompt: str,
        description: str,
        subagent_type: str | None = None,
        model: str | None = None,
        run_in_background: bool = False,
        name: str = "",
    ) -> str | BackgroundTask:
        del description  # 仅用于主界面显示，运行时不重复注入。
        fork = not subagent_type
        if fork:
            if not self.background_enabled:
                raise ValueError("后台禁用，无法 Fork")
            definition = Definition(
                "fork",
                "Fork 父对话",
                "",
                max_turns=25,
                source="fork",
            )
            background = True
        else:
            definition = self.catalog.resolve(subagent_type)
            if definition is None:
                raise LookupError(f"未知 subagent_type: {subagent_type}")
            background = definition.background or run_in_background
            if background and not self.background_enabled:
                background = False
        if model:
            definition = replace(definition, model=model)
        if definition.isolation == "worktree":
            return await self._launch_isolated(definition, prompt, name=name)
        runtime, task = self._build_runtime(definition, prompt, fork=fork, background=background)
        if background:
            item = self.manager.launch(runtime, task, name=name)
            item.launch_status = "async_launched"
            return item

        item = self.manager.launch(runtime, task, name=name, notify=False)
        assert item.handle is not None
        self.foreground_task = item
        self._manual_background.clear()
        manual_wait = asyncio.create_task(self._manual_background.wait())
        done, _ = await asyncio.wait(
            {item.handle, manual_wait},
            timeout=self.auto_background_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if item.handle in done:
            manual_wait.cancel()
            self.foreground_task = None
            if item.err is not None:
                raise RuntimeError(str(item.err)) from item.err
            return item.result
        manual = manual_wait in done
        manual_wait.cancel()
        if not self.background_enabled:
            await item.handle
            self.foreground_task = None
            if item.err is not None:
                raise RuntimeError(str(item.err)) from item.err
            return item.result
        item.notify = True
        item.launch_status = "manual_to_background" if manual else "timed_out_to_background"
        self.foreground_task = None
        return item

    def move_foreground_to_background(self) -> bool:
        """让 ESC 把当前前台子任务移交后台，而不是取消它。"""

        if not self.background_enabled or self.foreground_task is None:
            return False
        self._manual_background.set()
        return True

    async def _launch_isolated(self, definition: Definition, prompt: str, *, name: str) -> str:
        if self.worktree_manager is None:
            raise ValueError("当前目录未启用 Git Worktree，无法运行隔离 SubAgent")
        worktree_name = f"agent-a{uuid4().hex[:7]}"
        item = await self.worktree_manager.create(worktree_name, "HEAD", manual=False)
        notice = build_worktree_notice(str(self.workspace), str(item.path))
        runtime, task = self._build_runtime(
            definition,
            f"{notice}\n\n{prompt}",
            fork=False,
            background=False,
            workspace=item.path,
        )
        with with_cwd(item.path):
            result_item = self.manager.launch(runtime, task, name=name, notify=False)
        assert result_item.handle is not None
        try:
            await asyncio.shield(result_item.handle)
        except asyncio.CancelledError:
            result_item.handle.cancel()
            await asyncio.gather(result_item.handle, return_exceptions=True)
            await self.worktree_manager.auto_cleanup(worktree_name)
            raise
        cleanup = await self.worktree_manager.auto_cleanup(worktree_name)
        if result_item.err is not None:
            raise RuntimeError(str(result_item.err)) from result_item.err
        result = result_item.result
        if cleanup.kept:
            result += f"\n[Worktree 保留在 {cleanup.path}，分支 {cleanup.branch}]"
        return result

    async def run_definition(
        self,
        definition: Definition,
        prompt: str,
        *,
        seed: tuple[ChatMessage, ...] = (),
    ) -> str:
        """供 Skill fork 等调用方复用同一构造与过滤底座。"""

        runtime, task = self._build_runtime(
            definition,
            prompt,
            fork=False,
            background=False,
        )
        if seed:
            runtime.seed_history(seed)
        return await self.manager.run_inline(runtime, task, name=definition.name)

    def _build_runtime(
        self,
        definition: Definition,
        prompt: str,
        *,
        fork: bool,
        background: bool,
        workspace: Path | None = None,
    ) -> tuple[ConversationRuntime, str]:
        child_workspace = (workspace or self.workspace).resolve()
        provider = self.provider_factory(self._provider_config(definition.model))
        permission = self._permission_manager(definition, child_workspace)
        allowed = self._allowed_tools(definition, background=background, fork=fork)
        blocked = (
            {"Agent": "Fork 子 Agent 不能再启动 Agent"}
            if fork
            else {"Agent": "定义式子 Agent 不能启动 Agent"}
        )
        runtime = ConversationRuntime(
            provider,
            system_prompt=None if fork else definition.prompt,
            tool_registry=self.registry,
            workspace=child_workspace,
            permission_manager=permission,
            hook_engine=self.parent_runtime.hook_engine,
            instructions=self.parent_runtime.instructions,
            memory_manager=self.parent_runtime.memory_manager,
            max_iterations=definition.max_turns,
            allowed_tools=allowed,
            blocked_tool_errors=blocked,
            permission_source=definition.name,
        )
        runtime.set_skill_catalog(self.parent_runtime.skill_catalog)
        if fork:
            for skill_name, body in self.parent_runtime.active_skills.items():
                tools = self.parent_runtime._skill_tool_allowlists.get(skill_name, frozenset())
                runtime.activate_skill(skill_name, body, tuple(tools))
            forked = build_forked_messages(self.parent_runtime.history, prompt)
            runtime.seed_history(forked[:-1])
            return runtime, forked[-1].content
        return runtime, prompt

    def _allowed_tools(
        self, definition: Definition, *, background: bool, fork: bool
    ) -> frozenset[str]:
        allowed = set(self.registry.names())
        if not fork:
            allowed.difference_update(ALL_AGENT_DISALLOWED_TOOLS)
        allowed.difference_update(CUSTOM_AGENT_DISALLOWED_TOOLS)
        if background:
            dynamic = {
                name for name in allowed if name.startswith("mcp__") or name.startswith("skill__")
            }
            background_allowed = set(ASYNC_AGENT_ALLOWED_TOOLS) | dynamic
            if fork:
                background_allowed.add("Agent")
            allowed.intersection_update(background_allowed)
        allowed.difference_update(definition.disallowed_tools)
        if definition.tools:
            allowed.intersection_update(definition.tools)
        return frozenset(allowed)

    def _permission_manager(
        self, definition: Definition, workspace: Path
    ) -> PermissionManager | MCPPermissionManager:
        parent = self.parent_runtime.permission_manager
        if isinstance(parent, MCPPermissionManager):
            base = PermissionManager(
                workspace,
                rules=parent.base.rules,
                mode=definition.permission_mode,
                local_path=parent.base.local_path,
            )
            return MCPPermissionManager(base, self.registry, parent.rules)
        if isinstance(parent, PermissionManager):
            return PermissionManager(
                workspace,
                rules=parent.rules,
                mode=definition.permission_mode,
                local_path=parent.local_path,
            )
        return PermissionManager(workspace, mode=definition.permission_mode)

    def _provider_config(self, requested: str) -> ProviderConfig:
        if requested == "inherit":
            return self.parent_runtime.provider.config
        for config in self.providers:
            if requested.casefold() in {config.name.casefold(), config.model.casefold()}:
                return config
            if requested.casefold() in config.model.casefold():
                return config
        # 别名没有对应配置时安全继承，避免角色目录让应用无法启动。
        return self.parent_runtime.provider.config


def history_has_fork_marker(messages: tuple[ChatMessage, ...]) -> bool:
    return any("<fork_boilerplate>" in message.content for message in messages)


__all__ = [
    "ALL_AGENT_DISALLOWED_TOOLS",
    "ASYNC_AGENT_ALLOWED_TOOLS",
    "AUTO_BACKGROUND_SECONDS",
    "Service",
]
