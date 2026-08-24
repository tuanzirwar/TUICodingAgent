"""Pane 后端队员的无 TUI 自治运行入口。"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import suppress
from pathlib import Path

from ..config import ConfigLoader
from ..models import PermissionMode, RuntimeEventType
from ..permissions import PermissionManager
from ..providers.factory import create_provider
from ..runtime import ConversationRuntime
from ..session import SessionContext, Writer
from ..subagent import Catalog as AgentCatalog
from ..subagent import Manager as SubAgentManager
from ..subagent import Service as SubAgentService
from ..tools import create_default_registry
from ..tools.subagent import AgentTool
from ..tools.team import (
    TaskCreateTool,
    TaskUpdateTool,
    TeamSendMessageTool,
    TeamTaskGetTool,
    TeamTaskListTool,
)
from .context import TeammateContext, with_teammate
from .core import Manager
from .mailbox import Mailbox, Message
from .service import TeamService


async def run_team_member(args) -> int:
    worktree = await asyncio.to_thread(Path(args.worktree).resolve)
    os.chdir(worktree)
    config = ConfigLoader.load(worktree / "config.yaml")
    provider_config = _select_provider(config.providers, args.model or "")
    manager = Manager()
    team = manager.get(args.team)
    if team is None:
        print(f"Team 不存在：{args.team}", file=sys.stderr)
        return 2
    member = team.find_member(args.agent_id)
    if member is None:
        print(f"Team 队员不存在：{args.agent_id}", file=sys.stderr)
        return 2
    shutdown_event = asyncio.Event()
    registry = create_default_registry(worktree)
    registry.register(TaskCreateTool(manager))
    registry.register(TaskUpdateTool(manager))
    registry.register(TeamTaskGetTool(manager))
    registry.register(TeamTaskListTool(manager))
    registry.register(TeamSendMessageTool(manager, shutdown_event=shutdown_event))
    permission = PermissionManager(worktree, mode=PermissionMode.DONT_ASK)
    session_dir = await asyncio.to_thread(Path(args.session_dir).resolve)
    session_context = SessionContext(
        worktree,
        session_dir.name,
        session_dir,
        session_dir / "tool-results",
        session_dir / "conversation.jsonl",
    )
    runtime = ConversationRuntime(
        create_provider(provider_config),
        system_prompt=TeamService._prompt_suffix(team.name, member, team.members),
        tool_registry=registry,
        workspace=worktree,
        permission_manager=permission,
        session_writer=Writer(session_context, provider_config.model),
        allowed_tools=frozenset(registry.names()),
    )
    agent_catalog = AgentCatalog(worktree)
    agent_catalog.load_all()
    agent_tool = AgentTool()
    registry.register(agent_tool)
    subagent_service = SubAgentService(
        workspace=worktree,
        registry=registry,
        catalog=agent_catalog,
        manager=SubAgentManager(runtime.queue_reminder),
        parent_runtime=runtime,
        provider_factory=create_provider,
        providers=config.providers,
        background_enabled=False,
    )
    agent_tool.set_service(subagent_service)
    runtime.allowed_tools = frozenset(registry.names())
    if args.plan_mode:
        runtime.set_permission_mode(PermissionMode.PLAN)
    mailbox = Mailbox(team.config_dir / "mailbox")

    async def provide_mailbox_messages() -> str:
        messages = await mailbox.read(member.agent_id)
        if not messages:
            return ""
        await mailbox.mark_read(member.agent_id, list(range(len(messages))))
        for message in messages:
            if message.type == "plan_approval_response":
                runtime.set_permission_mode(
                    PermissionMode(team.lead_permission_mode)
                    if message.approve
                    else PermissionMode.PLAN
                )
        rows = [f"收到 {len(messages)} 条新消息:"]
        rows.extend(
            f"[{index}] 来自 {message.sender}"
            f"(type={message.type},ts={message.timestamp}):\n    {message.text}"
            for index, message in enumerate(messages, 1)
        )
        return "<incoming-messages>\n" + "\n".join(rows) + "\n</incoming-messages>"

    runtime.set_reminder_provider(provide_mailbox_messages)
    wake_event = asyncio.Event()
    reader = asyncio.create_task(_stdin_waker(wake_event))
    try:
        while mailbox.root.exists():
            if not await mailbox.read(member.agent_id):
                with suppress(TimeoutError):
                    await asyncio.wait_for(wake_event.wait(), timeout=2)
                wake_event.clear()
                continue
            await team.set_member_active(member.agent_id, True)
            with with_teammate(TeammateContext(team.name, member.agent_id, member.name)):
                async for event in runtime.chat("收到新的团队消息，请读取并处理。"):
                    _print_event(event)
            await team.set_member_active(member.agent_id, False)
            if shutdown_event.is_set():
                break
            await mailbox.write(
                team.lead_agent_id,
                Message(member.name, f"[idle] {member.name} (reason: available)"),
            )
            print("-" * 40, flush=True)
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        await runtime.close()
    return 0


async def _stdin_waker(event: asyncio.Event) -> None:
    while True:
        line = await asyncio.to_thread(sys.stdin.readline)
        if line == "":
            return
        event.set()


def _print_event(event) -> None:
    if event.type == RuntimeEventType.TEXT_DELTA and event.text:
        print(event.text, end="", flush=True)
    elif event.type == RuntimeEventType.TOOL_STARTED and event.tool_call:
        print(f"\n● {event.tool_call.name}({event.tool_call.arguments})", flush=True)
    elif event.type == RuntimeEventType.FAILED:
        print(f"错误：{event.text}", file=sys.stderr, flush=True)


def _select_provider(providers, requested: str):
    if not requested:
        return providers[0]
    return next(
        (item for item in providers if requested in {item.name, item.model}),
        providers[0],
    )


__all__ = ["run_team_member"]
