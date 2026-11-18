"""Team 队员的创建、运行、停机与续派。"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

from ..models import PermissionMode
from ..session import SessionContext, SessionInfo, Writer, restore_session
from ..subagent.definitions import Definition
from ..subagent.service import Service as SubAgentService
from ..tools.context import with_cwd
from ..worktree import ExitOptions
from ..worktree import Manager as WorktreeManager
from .backend import BackendType, SpawnRequest, new_backend
from .context import TeammateContext, current_teammate, with_teammate
from .core import Manager, TeammateInfo
from .delivery import MailboxDelivery
from .mailbox import Mailbox, Message

TEAMMATE_TOOLS = frozenset(
    {
        "Agent",
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "bash",
        "load_skill",
        "TaskCreate",
        "TaskGet",
        "TaskList",
        "TaskUpdate",
        "SendMessage",
    }
)


class InProcessTeammateNoSpawnError(RuntimeError):
    pass


class TeamService:
    def __init__(
        self,
        manager: Manager,
        subagents: SubAgentService,
        worktrees: WorktreeManager | None,
        workspace: Path,
        *,
        fork_teammate: bool = False,
    ) -> None:
        self.manager = manager
        self.subagents = subagents
        self.worktrees = worktrees
        self.workspace = workspace
        self.fork_teammate = fork_teammate
        self.names = manager.registry

    async def spawn(
        self,
        *,
        team_name: str,
        name: str,
        prompt: str,
        subagent_type: str = "",
        model: str | None = None,
        plan_mode_required: bool = False,
    ):
        context = current_teammate()
        if context is not None:
            current_team = self.manager.get(context.team_name)
            if current_team and current_team.backend == BackendType.IN_PROCESS:
                raise InProcessTeammateNoSpawnError("in-process Team 队员不能继续派生 Team 队员")
        team = self.manager.get(team_name)
        if team is None:
            raise LookupError(f"未知 Team：{team_name}")
        fork = not subagent_type and self.fork_teammate
        resolved_type = subagent_type or "general-purpose"
        definition = self.subagents.catalog.resolve(resolved_type)
        if definition is None:
            raise LookupError(f"未知 subagent_type: {resolved_type}")
        if self.worktrees is None:
            raise ValueError("Team 队员需要 Git Worktree，但当前目录不是 Git 仓库")
        member_name = self.names.claim(name or f"member-{uuid4().hex[:6]}")
        agent_id = f"agent-{uuid4().hex[:8]}"
        worktree_name = f"team-{team.sanitized_name}/{member_name}"
        try:
            worktree = await self.worktrees.create(worktree_name, "HEAD", manual=True)
        except BaseException:
            self.names.release(member_name)
            raise
        session_dir = self.workspace / ".tuicodingagent" / "sessions" / agent_id
        member = TeammateInfo(
            name=member_name,
            agent_id=agent_id,
            agent_type="" if fork else resolved_type,
            model=model or "",
            worktree_path=str(worktree.path),
            branch=worktree.branch,
            backend_type=team.backend.value,
            plan_mode_required=plan_mode_required,
            session_dir=str(session_dir),
        )
        try:
            session_dir.mkdir(parents=True, exist_ok=True)
            await team.add_member(member)
            self.names.register(member_name, agent_id)
            definition = Definition(
                definition.name,
                definition.description,
                definition.prompt + self._prompt_suffix(team.name, member, team.members),
                tools=tuple(TEAMMATE_TOOLS),
                model=model or definition.model,
                permission_mode=(
                    PermissionMode.PLAN if plan_mode_required else PermissionMode.DONT_ASK
                ),
                max_turns=definition.max_turns,
                source=definition.source,
            )
            runtime, task = self.subagents._build_runtime(
                definition, prompt, fork=fork, background=False, workspace=worktree.path
            )
            runtime.allowed_tools = frozenset(set(runtime.allowed_tools or ()) | {"Agent"})
            runtime.blocked_tool_errors.pop("Agent", None)
            if fork:
                runtime.instructions = (
                    f"{runtime.instructions}\n\n"
                    f"{self._prompt_suffix(team.name, member, team.members)}"
                ).strip()
            runtime.session_writer = Writer(
                SessionContext.create(self.workspace, agent_id), runtime.provider.config.model
            )
            runtime.agent_id = agent_id
            self._attach_mailbox(runtime, team, member)
            request = SpawnRequest(
                team_name=team.name,
                member_name=member_name,
                agent_id=agent_id,
                worktree_path=worktree.path,
                session_dir=session_dir,
                agent_type="" if fork else resolved_type,
                model=model or "",
                initial_prompt=prompt,
                plan_mode_required=plan_mode_required,
                sub_agent=runtime,
                task_mgr=self.subagents.manager,
            )
            backend = new_backend(team.backend, self.manager.in_process_backend)
            if team.backend != BackendType.IN_PROCESS:
                await Mailbox(team.config_dir / "mailbox").write(agent_id, Message("lead", prompt))
            member.pane_id, spawned_id = await backend.spawn(request)
            if spawned_id != agent_id:
                raise RuntimeError("Team 后端返回了不一致的 agent_id")
        except BaseException:
            await team.remove_member(agent_id)
            self.names.unregister(member_name)
            await self.cleanup_member(member)
            raise
        await team.set_member_pane(agent_id, member.pane_id)
        await team.set_member_active(agent_id, True)
        if team.backend != BackendType.IN_PROCESS:
            return {
                "member_name": member_name,
                "agent_id": agent_id,
                "worktree": str(worktree.path),
                "backend": team.backend.value,
                "pane_id": member.pane_id,
            }
        with ExitStack() as stack:
            stack.enter_context(with_cwd(worktree.path))
            stack.enter_context(with_teammate(TeammateContext(team.name, agent_id, member_name)))
            item = self.subagents.manager.launch(runtime, task, name=member_name)
        assert item.handle is not None
        self.manager.in_process_backend.tasks[agent_id] = item.handle

        self._watch_completion(team, member, item)
        return {
            "member_name": member_name,
            "agent_id": agent_id,
            "worktree": str(worktree.path),
            "backend": team.backend.value,
            "pane_id": "",
            "task_id": item.id,
            "status": "async_launched",
        }

    async def send_from_lead(
        self,
        target: str,
        content: str,
        *,
        team_name: str = "",
        message_type: str = "text",
        request_id: str = "",
        approve: bool | None = None,
    ) -> list[str]:
        if target == "*":
            if team_name:
                team = self.manager.get(team_name)
            elif len(self.manager.teams) == 1:
                team = next(iter(self.manager.teams.values()))
            else:
                team = None
            if team is None:
                raise LookupError("广播时必须能唯一确定 Team")
            recipients = [item for item in team.members if item.agent_id != team.lead_agent_id]
        elif team_name:
            team = self.manager.get(team_name)
            member = team.find_member(target) if team is not None else None
            found = (team, member) if team is not None and member is not None else None
            recipients = [member] if found is not None else []
        else:
            found = self.manager.find_member(target)
            if found is None:
                raise LookupError(f"未知 Team 队员：{target}")
            team, member = found
            recipients = [member]
        delivered: list[str] = []
        for member in recipients:
            await Mailbox(team.config_dir / "mailbox").write(
                member.agent_id,
                Message("lead", content, message_type, request_id, approve),
            )
            if message_type == "plan_approval_response" and approve:
                previous = self.subagents.manager.find_name(member.name)
                if previous is not None:
                    previous.runtime.set_permission_mode(PermissionMode.DEFAULT)
            if member.is_active is False and team.backend == BackendType.IN_PROCESS:
                await self._continue_in_process(team, member, content)
            else:
                backend = new_backend(team.backend, self.manager.in_process_backend)
                await backend.wake(member.pane_id, member.agent_id)
            delivered.append(member.agent_id)
        return delivered

    async def _continue_in_process(self, team, member: TeammateInfo, content: str) -> None:
        previous = self.subagents.manager.find_name(member.name)
        if previous is None:
            definition = self.subagents.catalog.resolve(member.agent_type or "general-purpose")
            if definition is None:
                raise ValueError(f"队员定义不可恢复：{member.agent_type}")
            definition = Definition(
                definition.name,
                definition.description,
                definition.prompt + self._prompt_suffix(team.name, member, team.members),
                tools=tuple(TEAMMATE_TOOLS),
                model=member.model or definition.model,
                permission_mode=(
                    PermissionMode.PLAN if member.plan_mode_required else PermissionMode.DONT_ASK
                ),
                max_turns=definition.max_turns,
                source=definition.source,
            )
            runtime, _ = self.subagents._build_runtime(
                definition,
                content,
                fork=False,
                background=False,
                workspace=Path(member.worktree_path),
            )
            session_context = SessionContext.create(self.workspace, member.agent_id)
            session_info = SessionInfo(
                session_context,
                member.name,
                runtime.provider.config.model,
                0,
                0,
                0,
            )
            await runtime.restore(
                restore_session(session_info),
                Writer(session_context, runtime.provider.config.model),
            )
            runtime.agent_id = member.agent_id
            self._attach_mailbox(runtime, team, member)
            previous_runtime = runtime
        else:
            previous_runtime = previous.runtime
        with ExitStack() as stack:
            stack.enter_context(with_cwd(Path(member.worktree_path)))
            stack.enter_context(
                with_teammate(TeammateContext(team.name, member.agent_id, member.name))
            )
            item = self.subagents.manager.launch(previous_runtime, content, name=member.name)
        await team.set_member_active(member.agent_id, True)
        assert item.handle is not None
        self.manager.in_process_backend.tasks[member.agent_id] = item.handle
        self._watch_completion(team, member, item)

    def _attach_mailbox(self, runtime, team, member: TeammateInfo) -> None:
        mailbox = Mailbox(team.config_dir / "mailbox")
        MailboxDelivery(
            runtime, mailbox, member.agent_id, permission_mode=team.lead_permission_mode
        ).bind()

    def _watch_completion(self, team, member: TeammateInfo, item) -> None:
        async def finished(_handle) -> None:
            try:
                await team.set_member_active(member.agent_id, False)
                await Mailbox(team.config_dir / "mailbox").write(
                    team.lead_agent_id,
                    Message(member.name, f"[idle] {member.name}: {item.result}"),
                )
            except (FileNotFoundError, LookupError):
                return

        assert item.handle is not None
        item.handle.add_done_callback(lambda handle: asyncio.create_task(finished(handle)))

    async def cleanup_member(self, member: TeammateInfo) -> None:
        if member.worktree_path and self.worktrees is not None:
            name = next(
                (
                    item.name
                    for item in self.worktrees.list()
                    if str(item.path) == member.worktree_path
                ),
                "",
            )
            if name:
                await self.worktrees.remove(name, ExitOptions(discard_changes=True))
        if member.session_dir:
            import shutil

            shutil.rmtree(member.session_dir, ignore_errors=True)

    @staticmethod
    def _prompt_suffix(
        team_name: str,
        member: TeammateInfo,
        members: list[TeammateInfo] | tuple[TeammateInfo, ...] = (),
    ) -> str:
        roster = members or (member,)
        member_list = ", ".join(f"{item.name}({item.agent_type or 'teammate'})" for item in roster)
        return (
            "\n\nIMPORTANT: You are running as an agent in a team.\n"
            "Just writing a response in text is not visible to others\n"
            "on your team - you MUST use the SendMessage tool.\n"
            "The user interacts primarily with the team lead.\n"
            "Your work is coordinated through the task system\n"
            "and teammate messaging.\n\n"
            "<team-context>\n"
            f"team: {team_name}\n"
            f"你的成员名: {member.name}\n"
            f"你的 agent_id: {member.agent_id}\n"
            f"worktree 目录: {member.worktree_path}\n"
            f"当前团队成员: {member_list}\n"
            "</team-context>"
        )


__all__ = ["InProcessTeammateNoSpawnError", "TEAMMATE_TOOLS", "TeamService"]
