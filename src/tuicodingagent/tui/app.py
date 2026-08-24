"""TUICodingAgent 主 Textual 应用。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.css.query import NoMatches
from textual.timer import Timer
from textual.widgets import Label, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from .. import __version__
from ..commands import BUILTIN_REGISTRY, build_skill_registry
from ..context import ContextController
from ..coordinator import COORDINATOR_ALLOWED_TOOLS, SYSTEM_PROMPT_SUFFIX
from ..coordinator import is_enabled as coordinator_enabled
from ..hooks import HookEngine, HookEvent
from ..mcp_client import MCPPermissionManager
from ..memory import MemoryManager
from ..models import (
    AppConfig,
    PermissionChoice,
    PermissionMode,
    ProviderConfig,
    RuntimeEventType,
    SessionState,
)
from ..permissions import PermissionManager
from ..providers.base import ChatProvider
from ..providers.factory import create_provider
from ..runtime import ConversationRuntime
from ..session import SessionContext, SessionInfo, Writer, restore_session, scan_sessions
from ..skills import InstallSkillTool, SkillExecutor, SkillInstaller, SkillLoader
from ..subagent import Catalog as AgentCatalog
from ..subagent import Manager as TaskManager
from ..subagent import Service as SubAgentService
from ..team import Manager as TeamManager
from ..team import TeamHasActiveMembersError
from ..team.service import TeamService
from ..tools import LoadSkillTool, ToolRegistry, create_default_registry
from ..tools.context import with_cwd
from ..tools.subagent import (
    AgentTool,
    SendMessageTool,
    TaskGetTool,
    TaskListTool,
    TaskStopTool,
)
from ..tools.team import TaskCreateTool, TaskUpdateTool, TeamCreateTool, TeamDeleteTool
from ..worktree import ExitAction, ExitOptions, WorktreeError
from ..worktree import Manager as WorktreeManager
from .screens import PermissionScreen, ProviderSelectScreen, ResumeScreen
from .widgets import ChatEntry, ErrorEntry, PromptInput, ToolEntry

BANNER = r""" /\_/\\
( o.o )  TUICodingAgent
 > ^ <"""


async def _chat_with_cwd(runtime: ConversationRuntime, text: str, cwd: Path):
    """在显式 cwd 上下文内消费整个主 Agent turn。"""

    with with_cwd(cwd):
        async for event in runtime.chat(text):
            yield event


class TUICodingAgentApp(App[None]):
    CSS_PATH = "app.tcss"
    TITLE = "TUICodingAgent"
    BINDINGS = [
        Binding("ctrl+c", "interrupt_or_quit", "取消/退出", show=False, priority=True),
        Binding("escape", "cancel_loop", "取消", show=False, priority=True),
        Binding("shift+tab", "cycle_permission_mode", "权限模式", show=False, priority=True),
    ]

    def __init__(
        self,
        config: AppConfig,
        *,
        workspace: Path | None = None,
        provider_factory: Callable[[ProviderConfig], ChatProvider] = create_provider,
        tool_registry: ToolRegistry | None = None,
        permission_manager: PermissionManager | MCPPermissionManager | None = None,
        instructions: str = "",
        memory_manager: MemoryManager | None = None,
        hook_engine: HookEngine | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.workspace = (workspace or Path.cwd()).resolve()
        self.provider_factory = provider_factory
        self.tool_registry = tool_registry
        self.permission_manager = permission_manager
        self.instructions = instructions
        self.memory_manager = memory_manager
        self.hook_engine = hook_engine
        self.active_provider: ProviderConfig | None = None
        self.runtime: ConversationRuntime | None = None
        self.session_state = SessionState.IDLE
        self._started_at = 0.0
        self._timer: Timer | None = None
        self._stream_entry: ChatEntry | None = None
        self._stream_text = ""
        self._tool_entries: dict[str, ToolEntry] = {}
        self._iteration = 1
        self._reply_worker = None
        self._session_context: SessionContext | None = None
        self._active_registry: ToolRegistry | None = None
        self._completion_candidates = ()
        self.command_registry = BUILTIN_REGISTRY
        self.skill_loader: SkillLoader | None = None
        self.skill_executor: SkillExecutor | None = None
        self._skill_tasks: set[asyncio.Task[None]] = set()
        self._session_start_task: asyncio.Task[None] | None = None
        self.task_manager: TaskManager | None = None
        self.subagent_service: SubAgentService | None = None
        self.agent_catalog: AgentCatalog | None = None
        self.worktree_manager: WorktreeManager | None = None
        self.active_cwd: Path | None = None
        self._worktree_sweep_task: asyncio.Task[list[str]] | None = None
        self.team_manager: TeamManager | None = None
        self.team_service: TeamService | None = None
        self.coordinator_mode = coordinator_enabled(config)
        self._lead_mail_task: asyncio.Task[None] | None = None

    def compose(self) -> ComposeResult:
        yield Static(
            f"{BANNER}\n版本 {__version__}  ·  {self.workspace}",
            id="banner",
        )
        yield Static("准备就绪，可以开始对话。", id="ready-line")
        yield VerticalScroll(id="conversation")
        with Container(id="prompt-box"):
            with Horizontal(id="prompt-row"):
                yield Label("❯", id="prompt-symbol")
                yield PromptInput(id="prompt-input", show_line_numbers=False)
            yield Static("Send a message...  ·  Alt+Enter 换行", id="prompt-placeholder")
        yield OptionList(id="command-menu")
        yield Static("正在初始化…", id="status-bar")

    async def on_mount(self) -> None:
        if len(self.config.providers) == 1:
            self._activate_provider(self.config.providers[0])
        else:
            await self.push_screen(
                ProviderSelectScreen(self.config.providers), self._activate_provider
            )
        self.query_one(PromptInput).focus()

    def _activate_provider(self, provider: ProviderConfig | None) -> None:
        if provider is None:
            self.exit()
            return
        self.active_provider = provider
        registry = self.tool_registry or create_default_registry(self.workspace)
        self._active_registry = registry
        available_tools = {item.name for item in registry.definitions()}
        available_tools.update(("load_skill", "install_skill"))
        loader = SkillLoader(self.workspace, available_tools=available_tools)
        loader.load_all()
        load_skill_tool = LoadSkillTool(loader)
        registry.register(load_skill_tool)
        registry.register(InstallSkillTool(SkillInstaller(loader.user_root), self.reload_skills))
        agent_tool = AgentTool()
        registry.register(agent_tool)
        permission_manager = self.permission_manager or PermissionManager.load(self.workspace)
        chat_provider = self.provider_factory(provider)
        session_context = SessionContext.create(self.workspace)
        self._session_context = session_context
        writer = Writer(session_context, provider.model)
        self.runtime = ConversationRuntime(
            chat_provider,
            tool_registry=registry,
            workspace=self.workspace,
            permission_manager=permission_manager,
            context_controller=ContextController.for_provider(
                self.workspace, chat_provider, session_context
            ),
            instructions=(
                f"{self.instructions}\n\n{SYSTEM_PROMPT_SUFFIX}".strip()
                if self.coordinator_mode
                else self.instructions
            ),
            memory_manager=self.memory_manager,
            session_writer=writer,
            hook_engine=self.hook_engine,
        )
        self.runtime.set_permission_mode(permission_manager.mode)
        self.runtime.set_skill_catalog(loader.catalog_text())
        load_skill_tool.set_runtime(self.runtime)
        agent_catalog = AgentCatalog(self.workspace)
        agent_catalog.load_all()
        self.runtime.set_skill_catalog(f"{loader.catalog_text()}\n\n{agent_catalog.catalog_text()}")
        task_manager = TaskManager(self.runtime.queue_reminder)
        task_manager.approval_handler = self._approve_subagent
        try:
            worktree_manager = WorktreeManager(self.workspace)
        except WorktreeError:
            worktree_manager = None
        if worktree_manager is not None:
            self.worktree_manager = worktree_manager
            if worktree_manager.current_session is not None:
                self.active_cwd = Path(worktree_manager.current_session.worktree_path)
            self._worktree_sweep_task = asyncio.create_task(
                worktree_manager.sweep_stale(datetime.now() - timedelta(hours=24))
            )
        service = SubAgentService(
            workspace=self.workspace,
            registry=registry,
            catalog=agent_catalog,
            manager=task_manager,
            parent_runtime=self.runtime,
            provider_factory=self.provider_factory,
            providers=self.config.providers,
            background_enabled=self.config.enable_subagent_background,
            worktree_manager=worktree_manager,
        )
        agent_tool.set_service(service)
        try:
            team_manager = TeamManager()
        except PermissionError:
            # 受限测试环境无法写用户目录时，退回项目内运行状态。
            team_manager = TeamManager(self.workspace / ".tuicodingagent" / "teams")
        team_manager.lead_permission_mode = permission_manager.mode.value
        team_service = TeamService(
            team_manager,
            service,
            worktree_manager,
            self.workspace,
            fork_teammate=self.config.features.get("fork_teammate", False),
        )
        team_manager.cleanup_member = team_service.cleanup_member
        agent_tool.set_team_service(team_service)
        registry.register(TeamCreateTool(team_manager))
        registry.register(TeamDeleteTool(team_manager))
        registry.register(TaskCreateTool(team_manager))
        registry.register(TaskUpdateTool(team_manager))
        registry.register(
            TaskListTool(task_manager, team_manager, prefer_team=self.coordinator_mode)
        )
        registry.register(
            TaskGetTool(task_manager, team_manager, prefer_team=self.coordinator_mode)
        )
        registry.register(TaskStopTool(task_manager))
        registry.register(SendMessageTool(task_manager, team_manager, team_service))
        if self.coordinator_mode:
            self.runtime.allowed_tools = frozenset(COORDINATOR_ALLOWED_TOOLS)
        else:
            self.runtime.allowed_tools = frozenset(
                name for name in registry.names() if name not in {"TaskCreate", "TaskUpdate"}
            )
        self.agent_catalog = agent_catalog
        self.task_manager = task_manager
        self.subagent_service = service
        self.team_manager = team_manager
        self.team_service = team_service
        self._lead_mail_task = asyncio.create_task(self._consume_lead_mail())
        self.skill_loader = loader
        self.skill_executor = SkillExecutor(
            loader,
            self.runtime,
            registry,
            self.workspace,
            self.provider_factory,
            self.config.providers,
            service,
        )
        self.command_registry = build_skill_registry(loader.skills)
        self._session_start_task = asyncio.create_task(self.runtime.session_start())
        self._update_status()
        self.query_one(PromptInput).focus()

    async def _approve_subagent(self, name: str, call, reason: str) -> PermissionChoice:
        """把子 Agent 的审批安全转发到主 TUI。"""

        return await self.push_screen_wait(
            PermissionScreen(call, f"[来自 SubAgent {name}] {reason}")
        )

    async def on_prompt_input_submitted(self, event: PromptInput.Submitted) -> None:
        if self.runtime is None:
            return
        text = event.text.strip()
        prompt = self.query_one(PromptInput)
        prompt.clear()
        self._close_completion()
        await self._dispatch_input(text)

    async def _dispatch_input(self, text: str) -> None:
        if not text:
            return
        if self._session_start_task is not None:
            await self._session_start_task
            self._session_start_task = None
        if text.casefold() == "/team" or text.casefold().startswith("/team "):
            await self.manage_teams(text[5:].strip())
            return
        if await self.command_registry.dispatch(text, self):
            return
        if self.session_state != SessionState.IDLE:
            return
        assert self.runtime is not None
        decision = await self.runtime.run_hook(HookEvent.USER_PROMPT_SUBMIT, prompt=text)
        if decision.blocked:
            await self.show_message(f"[hook {decision.hook_name}] {decision.reason}")
            return
        await self.send_user_message(text, text)

    async def send_user_message(self, text: str, display_text: str) -> None:
        await self.query_one("#conversation", VerticalScroll).mount(
            ChatEntry("你", display_text, classes="user-entry")
        )
        self._stream_entry = ChatEntry("TUICodingAgent", "")
        await self.query_one("#conversation", VerticalScroll).mount(self._stream_entry)
        self._begin_streaming()
        self._reply_worker = self._consume_reply(text)

    async def show_message(self, text: str) -> None:
        conversation = self.query_one("#conversation", VerticalScroll)
        await conversation.mount(Static(text, markup=False, classes="mode-entry"))
        conversation.scroll_end(animate=False)

    def is_idle(self) -> bool:
        return self.session_state == SessionState.IDLE

    async def set_mode(self, mode: PermissionMode) -> None:
        assert self.runtime is not None
        self.runtime.set_permission_mode(mode)
        if mode == PermissionMode.PLAN:
            await self.show_message("已进入 Plan Mode：仅开放 Read / Glob / Grep。")
        self._update_status()

    async def trigger_compact(self) -> None:
        assert self.runtime is not None
        await self.show_message("正在压缩上下文...")
        result = await self.runtime.force_compact()
        message = (
            f"已压缩，token 从 {result.before_tokens} 降至 {result.after_tokens}"
            if result.success
            else f"上下文压缩失败：{result.error}"
        )
        await self.show_message(message)

    async def open_resume(self) -> None:
        await self._resume_session()

    async def clear_and_new_session(self) -> None:
        assert self.runtime is not None
        context = await self.runtime.clear_and_new_session()
        self._session_context = context
        conversation = self.query_one("#conversation", VerticalScroll)
        await conversation.remove_children()
        await self.show_message("已结束当前会话并开启新会话。")
        self._update_status()

    def command_help(self) -> str:
        return self.command_registry.help_text()

    def hooks_summary(self) -> str:
        return self.hook_engine.summary() if self.hook_engine is not None else "No hooks loaded."

    async def run_skill(self, name: str, args: str) -> None:
        if self.skill_loader is None or self.skill_executor is None or self.runtime is None:
            await self.show_message("Skill 系统尚未就绪。")
            return
        skill = self.skill_loader.get(name)
        if skill is None:
            await self.show_message(f"未知 Skill：{name}")
            return
        if skill.mode == "inline":
            trigger = self.skill_executor.execute_inline(skill, args)
            await self.send_user_message(trigger, f"/{skill.name} {args}".rstrip())
            return
        await self.show_message(f"Skill `{skill.name}` 正在独立上下文中运行...")
        task = asyncio.create_task(self._run_fork_skill(skill, args))
        self._skill_tasks.add(task)
        task.add_done_callback(self._skill_tasks.discard)

    async def _run_fork_skill(self, skill, args: str) -> None:
        assert self.skill_executor is not None
        try:
            result = await self.skill_executor.execute_fork(skill, args)
        except Exception as exc:
            await self.show_message(f"Skill `{skill.name}` 执行失败：{exc}")
            return
        if self.runtime is not None:
            self.runtime.append_external_result(f"Skill `{skill.name}` 结果：\n{result}")
        await self.show_message(f"Skill `{skill.name}` 结果：\n{result}")

    async def manage_skills(self, args: str) -> None:
        if self.skill_loader is None:
            await self.show_message("Skill 系统尚未就绪。")
            return
        parts = args.split()
        action = parts[0].casefold() if parts else "list"
        if action == "list" and len(parts) == 1:
            lines = [
                f"{item.name}  [{self.skill_loader.get_source_label(item)}]  {item.description}"
                for item in self.skill_loader.skills
            ]
            await self.show_message("\n".join(lines) if lines else "没有已加载的 Skill。")
            return
        if action == "info" and len(parts) == 2:
            skill = self.skill_loader.get(parts[1])
            if skill is None:
                await self.show_message(f"未知 Skill：{parts[1]}")
                return
            await self.show_message(
                "\n".join(
                    (
                        f"name: {skill.name}",
                        f"description: {skill.description}",
                        f"tools: {', '.join(skill.tools) or 'all'}",
                        f"mode: {skill.mode}",
                        f"context: {skill.context}",
                        f"model: {skill.model or 'default'}",
                        f"source: {skill.source_path}",
                    )
                )
            )
            return
        if action == "reload" and len(parts) == 1:
            await self.reload_skills()
            await self.show_message("Skill 目录已重新加载。")
            return
        await self.show_message("用法：/skill list | info <name> | reload")

    async def reload_skills(self) -> None:
        if self.skill_loader is None:
            return
        self.skill_loader.load_all()
        self.command_registry = build_skill_registry(self.skill_loader.skills)
        if self.runtime is not None:
            agent_text = self.agent_catalog.catalog_text() if self.agent_catalog else ""
            self.runtime.set_skill_catalog(
                f"{self.skill_loader.catalog_text()}\n\n{agent_text}".strip()
            )

    async def manage_worktrees(self, args: str) -> None:
        manager = self.worktree_manager
        if manager is None:
            await self.show_message("当前目录不是 Git 仓库，Worktree 功能未启用。")
            return
        parts = args.split()
        action = parts[0].casefold() if parts else "list"
        try:
            if action == "list" and len(parts) == 1:
                current = manager.current_session.worktree_name if manager.current_session else ""
                lines = [
                    f"{item.name}  {item.path}  {item.branch}"
                    + ("  [active]" if item.name == current else "")
                    for item in manager.list()
                ]
                await self.show_message("\n".join(lines) if lines else "没有 Worktree。")
                return
            if action == "create" and len(parts) == 2:
                item = await manager.create(parts[1], "HEAD", manual=True)
                await self.show_message(f"已创建 Worktree：{item.path}\n分支：{item.branch}")
                return
            if action == "enter" and len(parts) == 2:
                session = await manager.enter(parts[1])
                self.active_cwd = Path(session.worktree_path)
                await self.show_message(f"已进入 Worktree：{session.worktree_path}")
                return
            if action == "exit":
                session = manager.current_session
                if session is None:
                    raise WorktreeError("当前没有已进入的 Worktree")
                remove = "--remove" in parts[1:]
                discard = "--discard" in parts[1:]
                report = await manager.exit(
                    session.worktree_name,
                    ExitAction.REMOVE if remove else ExitAction.KEEP,
                    ExitOptions(discard),
                )
                self.active_cwd = None
                await self.show_message(
                    f"已退出 Worktree：{report.path}"
                    + ("（已删除）" if report.removed else "（已保留）")
                )
                return
            if action == "remove" and len(parts) in {2, 3}:
                discard = "--discard" in parts[2:]
                report = await manager.remove(parts[1], ExitOptions(discard))
                await self.show_message(f"已删除 Worktree：{report.path}")
                return
            await self.show_message(
                "用法：/worktree create <slug> | list | enter <slug> | "
                "exit [--remove] [--discard] | remove <slug> [--discard]"
            )
        except (ValueError, WorktreeError) as exc:
            await self.show_message(f"Worktree 操作失败：{exc}")

    async def manage_teams(self, args: str) -> None:
        manager = self.team_manager
        if manager is None:
            await self.show_message("Team 系统尚未就绪。")
            return
        parts = args.split()
        action = parts[0].casefold() if parts else "list"
        try:
            if action == "list" and len(parts) == 1:
                lines = []
                for team in manager.teams.values():
                    active = sum(item.is_active is True for item in team.members)
                    lines.append(
                        f"{team.name}  {team.backend.value}  {len(team.members)} 成员  "
                        f"[{active}/{len(team.members)}] 活跃"
                    )
                await self.show_message("\n".join(lines) if lines else "没有 Team。")
                return
            if action == "info" and len(parts) == 2:
                team = manager.get(parts[1])
                if team is None:
                    raise LookupError(f"未知 Team：{parts[1]}")
                lines = [
                    f"Team: {team.name}",
                    f"配置: {team.config_path}",
                    f"后端: {team.backend.value}",
                ]
                lines.extend(
                    f"{item.name}  {item.agent_id}  {item.backend_type}  "
                    f"active={item.is_active}  {item.worktree_path}"
                    for item in team.members
                )
                await self.show_message("\n".join(lines))
                return
            if action == "delete" and len(parts) in {2, 3}:
                await manager.delete(parts[1], "--force" in parts[2:])
                await self.show_message(f"已删除 Team：{parts[1]}")
                return
            if action == "kill" and len(parts) == 2:
                found = manager.find_member(parts[1])
                if found is None:
                    raise LookupError(f"未知 Team 队员：{parts[1]}")
                team, member = found
                from ..team.backend import BackendType, new_backend

                await new_backend(
                    BackendType(member.backend_type), manager.in_process_backend
                ).kill(member.pane_id, member.agent_id)
                await team.set_member_active(member.agent_id, False)
                await self.show_message(f"已停止队员：{member.name}")
                return
            await self.show_message(
                "用法：/team list | info <name> | delete <name> [--force] | kill <member>"
            )
        except (LookupError, TeamHasActiveMembersError, ValueError) as exc:
            await self.show_message(f"Team 操作失败：{exc}")

    async def _consume_lead_mail(self) -> None:
        """轮询 Pane/in-process 共用邮箱，并在 Lead 空闲时自动续推。"""

        while True:
            await asyncio.sleep(1)
            if self.team_manager is None or self.runtime is None:
                continue
            messages = await self.team_manager.poll_lead_mailboxes()
            if not messages:
                continue
            body = "\n".join(f"[{item.team_name}/{item.sender}] {item.text}" for item in messages)[
                :8000
            ]
            self.runtime.queue_reminder(f"<team-update>\n{body}\n</team-update>")
            if self.session_state == SessionState.IDLE:
                trigger = "[team-update] 队员发来新消息，请按 Coordinator 流程处理。"
                await self.send_user_message(trigger, trigger)

    def request_exit(self) -> None:
        if self.runtime is not None:
            self.runtime.cancel_background_tasks()
        if self._reply_worker is not None:
            self._reply_worker.cancel()
        self.exit()

    def permission_mode_name(self) -> str:
        return self.runtime.permission_mode.value if self.runtime else PermissionMode.DEFAULT.value

    def usage(self) -> tuple[int, int]:
        if self.runtime is None:
            return 0, 0
        return self.runtime.total_input_tokens, self.runtime.total_output_tokens

    def tool_count(self) -> int:
        return len(self._active_registry.definitions()) if self._active_registry else 0

    def memory_files(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        return self.memory_manager.note_filenames() if self.memory_manager else ((), ())

    def model_name(self) -> str:
        return self.active_provider.model if self.active_provider else ""

    def workspace_path(self) -> Path:
        return self.workspace

    def session_identity(self) -> tuple[str, Path]:
        if self._session_context is None:
            return "", self.workspace
        return self._session_context.session_id, self._session_context.conversation_path

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if not isinstance(event.text_area, PromptInput):
            return
        text = event.text_area.text
        if not text.startswith("/") or "\n" in text or "\r" in text:
            self._close_completion()
            return
        self._completion_candidates = self.command_registry.complete(text)
        menu = self.query_one("#command-menu", OptionList)
        menu.clear_options()
        if self._completion_candidates:
            width = max(len(item.name) for item in self._completion_candidates)
            menu.add_options(
                [
                    Option(f"{item.name.ljust(width)}  {item.description}", id=item.name)
                    for item in self._completion_candidates
                ]
            )
            menu.highlighted = 0
        else:
            menu.add_option(Option("无匹配", id="__none__", disabled=True))
        menu.styles.display = "block"
        event.text_area.completion_active = True

    async def on_prompt_input_completion_key(self, event: PromptInput.CompletionKey) -> None:
        menu = self.query_one("#command-menu", OptionList)
        if event.key == "escape":
            self._close_completion()
            return
        if event.key == "up":
            menu.action_cursor_up()
            return
        if event.key == "down":
            menu.action_cursor_down()
            return
        raw = self.query_one(PromptInput).text.strip()
        if not self._completion_candidates:
            self._close_completion()
            if event.key == "enter":
                self.query_one(PromptInput).clear()
                await self._dispatch_input(raw)
            return
        index = menu.highlighted or 0
        selected = self._completion_candidates[index].name
        self.query_one(PromptInput).clear()
        self._close_completion()
        await self._dispatch_input(selected)

    def _close_completion(self) -> None:
        try:
            menu = self.query_one("#command-menu", OptionList)
            prompt = self.query_one(PromptInput)
        except Exception:
            return
        menu.styles.display = "none"
        menu.clear_options()
        prompt.completion_active = False
        self._completion_candidates = ()

    async def _resume_session(self) -> None:
        assert self.runtime is not None
        self.session_state = SessionState.RESUMING
        sessions = await asyncio.to_thread(scan_sessions, self.workspace)
        if not sessions:
            await self.query_one("#conversation", VerticalScroll).mount(
                Static("没有可恢复的历史会话。", classes="mode-entry")
            )
            self.session_state = SessionState.IDLE
            return
        await self.push_screen(ResumeScreen(sessions), self._resume_selected)

    async def _resume_selected(self, selected: SessionInfo | None) -> None:
        assert self.runtime is not None
        try:
            if selected is None:
                return
            conversation = self.query_one("#conversation", VerticalScroll)
            await conversation.mount(Static("正在加载会话...", classes="mode-entry"))
            restored = await asyncio.to_thread(restore_session, selected)
            writer = Writer(
                selected.context, self.active_provider.model if self.active_provider else ""
            )
            compacted = await self.runtime.restore(restored, writer)
            self._session_context = selected.context
            detail = (
                f"已恢复会话 {selected.context.session_id}，共 {len(self.runtime.history)} 条消息"
            )
            if compacted is not None and not compacted.success:
                detail += f"（超限压缩失败：{compacted.error}，已保留可恢复内容）"
            await conversation.mount(Static(detail, classes="mode-entry"))
            conversation.scroll_end(animate=False)
        finally:
            self.session_state = SessionState.IDLE
            self.query_one(PromptInput).focus()

    def _begin_streaming(self) -> None:
        self.session_state = SessionState.STREAMING
        self._started_at = time.monotonic()
        self._stream_text = ""
        self._iteration = 1
        self._tool_entries.clear()
        self.query_one(PromptInput).disabled = True
        self.query_one("#prompt-placeholder", Static).update("正在等待回复…")
        self._timer = self.set_interval(0.2, self._update_elapsed)
        self._update_elapsed()

    def _update_elapsed(self) -> None:
        if self.session_state != SessionState.STREAMING or self._stream_entry is None:
            return
        elapsed = int(time.monotonic() - self._started_at)
        if not self._stream_text:
            # 新一轮消息组件正处于异步挂载窗口时，下一次计时器会继续刷新。
            with suppress(NoMatches):
                self._stream_entry.append_text(
                    f"Agent 迭代 {self._iteration} · Imagining… ({elapsed}s)"
                )

    @work(exclusive=True, group="conversation")
    async def _consume_reply(self, text: str) -> None:
        assert self.runtime is not None
        assert self._stream_entry is not None
        entry = self._stream_entry
        try:
            async for event in _chat_with_cwd(
                self.runtime, text, self.active_cwd or self.workspace
            ):
                if event.type == RuntimeEventType.TEXT_DELTA:
                    self._stream_text += event.text
                    entry.append_text(self._stream_text)
                    self.query_one("#conversation", VerticalScroll).scroll_end(animate=False)
                elif event.type == RuntimeEventType.COMPLETED:
                    await entry.finalize_markdown(event.text, event.elapsed or 0.0)
                elif event.type == RuntimeEventType.ITERATION_STARTED:
                    self._iteration = event.iteration or self._iteration
                    if self._iteration > 1:
                        if self._stream_text:
                            await entry.finalize_markdown(self._stream_text, event.elapsed or 0.0)
                        else:
                            await entry.remove()
                        entry = ChatEntry("TUICodingAgent", "")
                        self._stream_entry = entry
                        self._stream_text = ""
                        await self.query_one("#conversation", VerticalScroll).mount(entry)
                    self.query_one("#prompt-placeholder", Static).update(
                        f"Agent 迭代 {self._iteration}… Esc/Ctrl+C 取消"
                    )
                    self._update_elapsed()
                elif event.type == RuntimeEventType.USAGE:
                    self._update_status()
                elif event.type == RuntimeEventType.CONTEXT_STATUS:
                    await self.query_one("#conversation", VerticalScroll).mount(
                        Static(event.text, classes="mode-entry")
                    )
                elif event.type == RuntimeEventType.PERMISSION_REQUESTED and event.tool_call:
                    choice = await self.push_screen_wait(
                        PermissionScreen(event.tool_call, event.text)
                    )
                    if choice == PermissionChoice.CANCEL:
                        self.runtime.request_cancel()
                    self.runtime.resolve_permission(event.tool_call.id, choice)
                elif event.type == RuntimeEventType.TOOL_STARTED and event.tool_call:
                    tool_entry = ToolEntry(event.tool_call)
                    self._tool_entries[event.tool_call.id] = tool_entry
                    await self.query_one("#conversation", VerticalScroll).mount(tool_entry)
                    self.query_one("#conversation", VerticalScroll).scroll_end(animate=False)
                elif (
                    event.type == RuntimeEventType.TOOL_FINISHED
                    and event.tool_call
                    and event.tool_result
                ):
                    tool_entry = self._tool_entries.get(event.tool_call.id)
                    if tool_entry is not None:
                        tool_entry.finish(event.tool_result)
                elif event.type in (
                    RuntimeEventType.LIMIT_REACHED,
                    RuntimeEventType.CANCELLED,
                ):
                    await self.query_one("#conversation", VerticalScroll).mount(
                        Static(event.text, classes="limit-warning")
                    )
                elif event.type == RuntimeEventType.FAILED:
                    await entry.remove()
                    await self.query_one("#conversation", VerticalScroll).mount(
                        ErrorEntry(event.text, event.elapsed or 0.0)
                    )
        finally:
            self._finish_streaming()

    def _finish_streaming(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self.session_state = SessionState.IDLE
        prompt = self.query_one(PromptInput)
        prompt.disabled = False
        self.query_one("#prompt-placeholder", Static).update("Send a message...  ·  Alt+Enter 换行")
        prompt.focus()

    async def action_interrupt_or_quit(self) -> None:
        if self.session_state == SessionState.STREAMING:
            await self._cancel_current_loop()
        else:
            self.exit()

    async def action_cancel_loop(self) -> None:
        prompt = self.query_one(PromptInput)
        if prompt.completion_active:
            self._close_completion()
            prompt.focus()
            return
        if self.session_state == SessionState.STREAMING:
            await self._cancel_current_loop()

    async def _cancel_current_loop(self) -> None:
        if (
            self.subagent_service is not None
            and self.subagent_service.move_foreground_to_background()
        ):
            await self.query_one("#conversation", VerticalScroll).mount(
                Static("前台 SubAgent 已转入后台继续运行。", classes="mode-entry")
            )
            return
        if self.runtime is not None:
            self.runtime.request_cancel()
        if isinstance(self.screen, PermissionScreen):
            self.screen.dismiss(PermissionChoice.CANCEL)
            return
        worker = self._reply_worker
        if worker is not None:
            worker.cancel()
        await self.query_one("#conversation", VerticalScroll).mount(
            Static("已取消当前 Agent Loop。", classes="limit-warning")
        )

    async def action_cycle_permission_mode(self) -> None:
        if self.runtime is None or self.session_state != SessionState.IDLE:
            return
        modes = tuple(mode for mode in PermissionMode if mode != PermissionMode.DONT_ASK)
        current = self.runtime.permission_mode
        next_mode = modes[(modes.index(current) + 1) % len(modes)]
        self.runtime.set_permission_mode(next_mode)
        await self.query_one("#conversation", VerticalScroll).mount(
            Static(f"权限模式已切换为 {next_mode.value}。", classes="mode-entry")
        )
        self._update_status()

    def _update_status(self) -> None:
        if self.active_provider is None:
            return
        mode = self.runtime.permission_mode.value if self.runtime else PermissionMode.DEFAULT.value
        input_tokens = self.runtime.total_input_tokens if self.runtime else 0
        output_tokens = self.runtime.total_output_tokens if self.runtime else 0
        coordinator_label = " \\[COORDINATOR]" if self.coordinator_mode else ""
        self.query_one("#status-bar", Static).update(
            f"{mode}{coordinator_label}    "
            f"{self.active_provider.model}    Token ↑{input_tokens} ↓{output_tokens}"
        )

    async def on_unmount(self) -> None:
        if self._session_start_task is not None:
            await self._session_start_task
            self._session_start_task = None
        for task in tuple(self._skill_tasks):
            task.cancel()
        if self._skill_tasks:
            await asyncio.gather(*tuple(self._skill_tasks), return_exceptions=True)
        if self._worktree_sweep_task is not None:
            await asyncio.gather(self._worktree_sweep_task, return_exceptions=True)
        if self._lead_mail_task is not None:
            self._lead_mail_task.cancel()
            await asyncio.gather(self._lead_mail_task, return_exceptions=True)
        if self.task_manager is not None:
            await self.task_manager.close()
        if self.runtime is not None:
            await self.runtime.close()
