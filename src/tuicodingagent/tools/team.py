"""Team 创建、删除与队员协作工具。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from ..models import ToolCall, ToolDefinition, ToolResult
from ..team.context import TeammateContext, current_teammate
from ..team.core import Manager, TeamHasActiveMembersError
from ..team.mailbox import MESSAGE_TYPES, Mailbox, Message
from ..team.tasks import TaskStore
from ..verification import VerificationRunner
from .base import ToolArgumentError, require_string
from .context import cwd_from_ctx


def success(call: ToolCall, value: object) -> ToolResult:
    return ToolResult(call.id, call.name, True, output=json.dumps(value, ensure_ascii=False))


class TeamCreateTool:
    read_only = False
    definition = ToolDefinition(
        "TeamCreate",
        "创建长期协作 Team。",
        {
            "type": "object",
            "properties": {
                "team_name": {"type": "string"},
                "description": {"type": "string"},
                "agent_type": {"type": "string"},
            },
            "required": ["team_name"],
            "additionalProperties": False,
        },
    )

    def __init__(self, manager: Manager) -> None:
        self.manager = manager

    async def execute(self, call: ToolCall) -> ToolResult:
        team = await self.manager.create(
            require_string(call, "team_name"), str(call.arguments.get("description", ""))
        )
        return success(
            call,
            {
                "team_name": team.sanitized_name,
                "backend": team.backend.value,
                "config_path": str(team.config_path),
            },
        )


class TeamDeleteTool:
    read_only = False
    definition = ToolDefinition(
        "TeamDelete",
        "删除 Team；活跃队员存在时需 force。",
        {
            "type": "object",
            "properties": {"team_name": {"type": "string"}, "force": {"type": "boolean"}},
            "required": ["team_name"],
            "additionalProperties": False,
        },
    )

    def __init__(self, manager: Manager) -> None:
        self.manager = manager

    async def execute(self, call: ToolCall) -> ToolResult:
        try:
            await self.manager.delete(
                require_string(call, "team_name"), bool(call.arguments.get("force", False))
            )
        except (LookupError, TeamHasActiveMembersError) as exc:
            return ToolResult(call.id, call.name, False, error=str(exc))
        return success(call, {"deleted": True})


class _TeamTool:
    def __init__(self, manager: Manager) -> None:
        self.manager = manager

    def scope(self):
        context = current_teammate()
        if context is None:
            if len(self.manager.teams) != 1:
                raise ToolArgumentError("Lead 使用协作工具时必须恰好有一个活跃 Team")
            team = next(iter(self.manager.teams.values()))
            return TeammateContext(team.name, team.lead_agent_id, "lead"), team
        team = self.manager.get(context.team_name)
        if team is None:
            raise ToolArgumentError(f"Team 已不存在：{context.team_name}")
        return context, team


class TaskCreateTool(_TeamTool):
    read_only = False
    definition = ToolDefinition(
        "TaskCreate",
        "创建 Team 共享任务。",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "assignee": {"type": "string"},
                "blocked_by": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title"],
            "additionalProperties": False,
        },
    )

    async def execute(self, call: ToolCall) -> ToolResult:
        _, team = self.scope()
        store = TaskStore(team.config_dir / "tasks.json")
        task = await store.create(
            require_string(call, "title"),
            str(call.arguments.get("description", "")),
            str(call.arguments.get("assignee", "")),
            call.arguments.get("blocked_by"),
        )
        return success(
            call,
            task.__dict__
            if hasattr(task, "__dict__")
            else {key: getattr(task, key) for key in task.__slots__},
        )


class TaskUpdateTool(_TeamTool):
    read_only = False
    definition = ToolDefinition(
        "TaskUpdate",
        "更新 Team 共享任务及依赖。",
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed", "blocked"],
                },
                "assignee": {"type": "string"},
                "add_blocks": {"type": "array", "items": {"type": "string"}},
                "add_blocked_by": {"type": "array", "items": {"type": "string"}},
                "remove_blocks": {"type": "array", "items": {"type": "string"}},
                "remove_blocked_by": {"type": "array", "items": {"type": "string"}},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    )

    async def execute(self, call: ToolCall) -> ToolResult:
        _, team = self.scope()
        if call.arguments.get("status") == "completed":
            context = current_teammate()
            member = team.find_member(context.agent_id) if context is not None else None
            if member is not None and member.worktree_path:
                from pathlib import Path

                workspace = cwd_from_ctx() or Path(member.worktree_path)
                verifier = VerificationRunner(workspace)
                changed = await verifier.working_state()
                if not changed and (workspace / ".git").exists():
                    # 提交后的工作区也需要验证，不能用一次 git commit 绕过完成门槛。
                    process = await asyncio.create_subprocess_exec(
                        "git",
                        "ls-files",
                        "-z",
                        cwd=workspace,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    try:
                        raw, _ = await asyncio.wait_for(process.communicate(), 10)
                    except BaseException:
                        if process.returncode is None:
                            process.kill()
                        await process.wait()
                        raise
                    if process.returncode:
                        return ToolResult(call.id, call.name, False, error="无法核验已提交任务产物")
                    changed = {
                        (workspace / name).resolve(): "tracked"
                        for name in raw.decode("utf-8", errors="replace").split("\0")
                        if name
                    }
                verification = await verifier.run(set(changed))
                if not verification.success:
                    return ToolResult(
                        call.id,
                        call.name,
                        False,
                        error="任务产物未通过自动验证，不能标记 completed：\n"
                        + verification.output,
                        metadata={"verification_failed": True},
                    )
        task = await TaskStore(team.config_dir / "tasks.json").update(
            require_string(call, "task_id"),
            title=call.arguments.get("title"),
            description=call.arguments.get("description"),
            status=call.arguments.get("status"),
            assignee=call.arguments.get("assignee"),
            add_blocks=call.arguments.get("add_blocks"),
            add_blocked_by=call.arguments.get("add_blocked_by"),
            remove_blocks=call.arguments.get("remove_blocks"),
            remove_blocked_by=call.arguments.get("remove_blocked_by"),
            actor=current_teammate().member_name if current_teammate() is not None else None,
            actor_aliases=(current_teammate().agent_id,) if current_teammate() is not None else (),
            expected_revision=call.arguments.get("expected_revision"),
        )
        return success(call, {key: getattr(task, key) for key in task.__slots__})


class TeamTaskListTool(_TeamTool):
    read_only = True
    definition = ToolDefinition(
        "TaskList",
        "列出 Team 共享任务。",
        {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed", "blocked"],
                }
            },
            "additionalProperties": False,
        },
    )

    async def execute(self, call: ToolCall) -> ToolResult:
        _, team = self.scope()
        return success(
            call, TaskStore(team.config_dir / "tasks.json").list(call.arguments.get("status"))
        )


class TeamTaskGetTool(_TeamTool):
    read_only = True
    definition = ToolDefinition(
        "TaskGet",
        "读取 Team 共享任务。",
        {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
            "additionalProperties": False,
        },
    )

    async def execute(self, call: ToolCall) -> ToolResult:
        _, team = self.scope()
        task = TaskStore(team.config_dir / "tasks.json").get(require_string(call, "task_id"))
        if task is None:
            return ToolResult(call.id, call.name, False, error="共享任务不存在")
        return success(call, {key: getattr(task, key) for key in task.__slots__})


class TeamSendMessageTool(_TeamTool):
    read_only = False
    definition = ToolDefinition(
        "SendMessage",
        "向同一 Team 队员或 Lead 发送消息，to='*' 广播。",
        {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "content": {"type": "string"},
                "type": {"type": "string", "enum": sorted(MESSAGE_TYPES)},
                "request_id": {"type": "string"},
                "approve": {"type": "boolean"},
            },
            "required": ["to", "content"],
            "additionalProperties": False,
        },
    )

    def __init__(
        self,
        manager: Manager,
        *,
        shutdown_event: asyncio.Event | None = None,
    ) -> None:
        super().__init__(manager)
        self.shutdown_event = shutdown_event

    async def execute(self, call: ToolCall) -> ToolResult:
        context, team = self.scope()
        target = require_string(call, "to")
        message_type = str(call.arguments.get("type", "text"))
        if message_type not in MESSAGE_TYPES:
            return ToolResult(call.id, call.name, False, error=f"未知消息类型：{message_type}")
        if message_type == "plan_approval_response":
            return ToolResult(call.id, call.name, False, error="只有 Team Lead 可以审批队员计划")
        if message_type == "shutdown_response" and target != "lead":
            return ToolResult(call.id, call.name, False, error="shutdown_response 只能发送给 Lead")
        if message_type == "shutdown_response" and not isinstance(
            call.arguments.get("approve"), bool
        ):
            return ToolResult(call.id, call.name, False, error="shutdown_response 必须提供 approve")
        mailbox = Mailbox(team.config_dir / "mailbox")
        recipients = (
            [item for item in team.members if item.agent_id != context.agent_id]
            if target == "*"
            else []
        )
        if target != "*":
            if target == "lead":
                ids = [team.lead_agent_id]
            else:
                member = team.find_member(target)
                if member is None:
                    return ToolResult(call.id, call.name, False, error=f"未知收件人：{target}")
                ids = [member.agent_id]
        else:
            ids = [item.agent_id for item in recipients]
        for agent_id in ids:
            await mailbox.write(
                agent_id,
                Message(
                    context.member_name,
                    require_string(call, "content"),
                    message_type,
                    str(call.arguments.get("request_id", "")),
                    call.arguments.get("approve"),
                ),
            )
        if (
            message_type == "shutdown_response"
            and call.arguments.get("approve") is True
            and self.shutdown_event is not None
        ):
            self.shutdown_event.set()
        return success(
            call,
            {
                "delivered": len(ids),
                "delivered_to": ids,
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )


__all__ = [
    "TaskCreateTool",
    "TaskUpdateTool",
    "TeamCreateTool",
    "TeamDeleteTool",
    "TeamSendMessageTool",
    "TeamTaskGetTool",
    "TeamTaskListTool",
]
