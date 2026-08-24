"""稳定的 Agent 与后台任务工具。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from ..models import ToolCall, ToolDefinition, ToolResult
from ..subagent import Manager, Service
from ..team.context import current_teammate
from ..team.core import Manager as TeamManager
from ..team.mailbox import MESSAGE_TYPES, Mailbox, Message
from ..team.service import TeamService
from ..team.tasks import TaskStore
from .base import ToolArgumentError, require_string


def _success(call: ToolCall, value: object) -> ToolResult:
    output = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return ToolResult(call.id, call.name, True, output=output)


class AgentTool:
    read_only = True
    definition = ToolDefinition(
        "Agent",
        "启动独立 SubAgent；指定 subagent_type 使用角色，省略则 Fork 当前对话并后台运行。",
        {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "description": {"type": "string"},
                "subagent_type": {"type": "string"},
                "model": {
                    "type": "string",
                    "enum": ["haiku", "sonnet", "opus", "inherit"],
                },
                "run_in_background": {"type": "boolean"},
                "name": {"type": "string"},
                "team_name": {"type": "string"},
                "plan_mode_required": {"type": "boolean"},
            },
            "required": ["prompt", "description"],
            "additionalProperties": False,
        },
    )

    def __init__(self, service: Service | None = None) -> None:
        self.service = service
        self.team_service: TeamService | None = None

    def set_service(self, service: Service) -> None:
        self.service = service

    def set_team_service(self, service: TeamService) -> None:
        self.team_service = service

    async def execute(self, call: ToolCall) -> ToolResult:
        try:
            prompt = require_string(call, "prompt")
            description = require_string(call, "description")
            subagent_type = call.arguments.get("subagent_type")
            model = call.arguments.get("model")
            name = call.arguments.get("name", "")
            background = call.arguments.get("run_in_background", False)
            if subagent_type is not None and not isinstance(subagent_type, str):
                raise ToolArgumentError("subagent_type 必须是字符串")
            if model is not None and model not in {"haiku", "sonnet", "opus", "inherit"}:
                raise ToolArgumentError("model 取值无效")
            if not isinstance(name, str) or not isinstance(background, bool):
                raise ToolArgumentError("name/run_in_background 类型无效")
            if current_teammate() is not None:
                if background:
                    raise ToolArgumentError("Team 队员只允许启动同步 SubAgent")
                if not subagent_type:
                    raise ToolArgumentError("Team 队员启动同步 SubAgent 时必须指定 subagent_type")
            if self.service is None:
                return ToolResult(call.id, call.name, False, error="SubAgent 服务尚未就绪")
            team_name = call.arguments.get("team_name")
            plan_required = call.arguments.get("plan_mode_required", False)
            if team_name is not None:
                if not isinstance(team_name, str) or not team_name:
                    raise ToolArgumentError("team_name 必须是非空字符串")
                if not isinstance(plan_required, bool):
                    raise ToolArgumentError("plan_mode_required 必须是布尔值")
                if self.team_service is None:
                    return ToolResult(call.id, call.name, False, error="Team 服务尚未就绪")
                result = await self.team_service.spawn(
                    team_name=team_name,
                    name=name,
                    prompt=prompt,
                    subagent_type=subagent_type or "",
                    model=model,
                    plan_mode_required=plan_required,
                )
                return _success(call, result)
            launched = await self.service.launch(
                prompt=prompt,
                description=description,
                subagent_type=subagent_type or None,
                model=model,
                run_in_background=background,
                name=name,
            )
            if isinstance(launched, str):
                return _success(call, launched)
            return _success(
                call,
                {"task_id": launched.id, "status": launched.launch_status},
            )
        except (LookupError, ValueError, RuntimeError, ToolArgumentError) as exc:
            return ToolResult(call.id, call.name, False, error=str(exc))


class TaskListTool:
    read_only = True
    definition = ToolDefinition(
        "TaskList",
        "列出本进程中的 SubAgent 任务。",
        {
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "additionalProperties": False,
        },
    )

    def __init__(
        self,
        manager: Manager,
        team_manager: TeamManager | None = None,
        *,
        prefer_team: bool = False,
    ) -> None:
        self.manager = manager
        self.team_manager = team_manager
        self.prefer_team = prefer_team

    async def execute(self, call: ToolCall) -> ToolResult:
        context = current_teammate()
        if self.team_manager is not None and (context is not None or self.prefer_team):
            team = (
                self.team_manager.get(context.team_name)
                if context is not None
                else next(iter(self.team_manager.teams.values()), None)
            )
            if team is None:
                return _success(call, [])
            return _success(
                call,
                TaskStore(team.config_dir / "tasks.json").list(call.arguments.get("status")),
            )
        return _success(
            call,
            [
                {
                    "id": item.id,
                    "name": item.name,
                    "status": item.status,
                    "tool_count": item.tool_count,
                    "last_activity": item.last_activity,
                }
                for item in self.manager.list()
            ],
        )


class TaskGetTool:
    read_only = True
    definition = ToolDefinition(
        "TaskGet",
        "读取一个 SubAgent 任务的完整状态和结果。",
        {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
            "additionalProperties": False,
        },
    )

    def __init__(
        self,
        manager: Manager,
        team_manager: TeamManager | None = None,
        *,
        prefer_team: bool = False,
    ) -> None:
        self.manager = manager
        self.team_manager = team_manager
        self.prefer_team = prefer_team

    async def execute(self, call: ToolCall) -> ToolResult:
        task_id = require_string(call, "task_id")
        context = current_teammate()
        if self.team_manager is not None and (context is not None or self.prefer_team):
            team = (
                self.team_manager.get(context.team_name)
                if context is not None
                else next(iter(self.team_manager.teams.values()), None)
            )
            if team is None:
                return ToolResult(call.id, call.name, False, error="当前没有活跃 Team")
            shared = TaskStore(team.config_dir / "tasks.json").get(task_id)
            if shared is None:
                return ToolResult(call.id, call.name, False, error=f"未知共享任务：{task_id}")
            return _success(call, {key: getattr(shared, key) for key in shared.__slots__})
        item = self.manager.get(task_id)
        if item is None:
            return ToolResult(call.id, call.name, False, error=f"未知任务：{task_id}")
        return _success(
            call,
            {
                "id": item.id,
                "name": item.name,
                "status": item.status,
                "result": item.result,
                "error": str(item.err or ""),
                "tool_count": item.tool_count,
                "last_activity": item.last_activity,
                "usage": {
                    "input_tokens": item.usage.input_tokens,
                    "output_tokens": item.usage.output_tokens,
                },
            },
        )


class TaskStopTool:
    read_only = False
    definition = ToolDefinition(
        "TaskStop",
        "请求取消一个正在运行的 SubAgent 任务。",
        {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
            "additionalProperties": False,
        },
    )

    def __init__(self, manager: Manager) -> None:
        self.manager = manager

    async def execute(self, call: ToolCall) -> ToolResult:
        task_id = require_string(call, "task_id")
        if not self.manager.stop(task_id):
            return ToolResult(call.id, call.name, False, error=f"任务不可取消：{task_id}")
        return _success(call, {"status": "cancellation_requested"})


class SendMessageTool:
    read_only = False
    definition = ToolDefinition(
        "SendMessage",
        "向 Team 成员发送消息，或按名称向已完成的普通 SubAgent 续派任务。",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "message": {"type": "string"},
                "to": {"type": "string"},
                "content": {"type": "string"},
                "type": {"type": "string", "enum": sorted(MESSAGE_TYPES)},
                "request_id": {"type": "string"},
                "approve": {"type": "boolean"},
                "team_name": {"type": "string"},
            },
            "additionalProperties": False,
        },
    )

    def __init__(
        self,
        manager: Manager,
        team_manager: TeamManager | None = None,
        team_service: TeamService | None = None,
    ) -> None:
        self.manager = manager
        self.team_manager = team_manager
        self.team_service = team_service

    async def execute(self, call: ToolCall) -> ToolResult:
        context = current_teammate()
        requested_type = str(call.arguments.get("type", "text"))
        if requested_type not in MESSAGE_TYPES:
            return ToolResult(call.id, call.name, False, error=f"未知消息类型：{requested_type}")
        if requested_type in {
            "plan_approval_response",
            "shutdown_response",
        } and not isinstance(call.arguments.get("approve"), bool):
            return ToolResult(call.id, call.name, False, error=f"{requested_type} 必须提供 approve")
        if requested_type == "plan_approval_response" and not call.arguments.get("request_id"):
            return ToolResult(
                call.id,
                call.name,
                False,
                error="plan_approval_response 必须提供 request_id",
            )
        if context is None and self.team_service is not None and "to" in call.arguments:
            try:
                delivered = await self.team_service.send_from_lead(
                    require_string(call, "to"),
                    require_string(call, "content"),
                    team_name=str(call.arguments.get("team_name", "")),
                    message_type=requested_type,
                    request_id=str(call.arguments.get("request_id", "")),
                    approve=call.arguments.get("approve"),
                )
            except (LookupError, ValueError) as exc:
                return ToolResult(call.id, call.name, False, error=str(exc))
            return _success(
                call,
                {
                    "delivered": len(delivered),
                    "delivered_to": delivered,
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            )
        if context is not None and self.team_manager is not None and "to" in call.arguments:
            team = self.team_manager.get(context.team_name)
            assert team is not None
            target = require_string(call, "to")
            message_type = requested_type
            if message_type == "plan_approval_response":
                return ToolResult(
                    call.id,
                    call.name,
                    False,
                    error="只有 Team Lead 可以审批队员计划",
                )
            if message_type == "shutdown_response" and target != "lead":
                return ToolResult(
                    call.id,
                    call.name,
                    False,
                    error="shutdown_response 只能发送给 Lead",
                )
            if target == "*":
                ids = [item.agent_id for item in team.members if item.agent_id != context.agent_id]
            elif target == "lead":
                ids = [team.lead_agent_id]
            else:
                member = team.find_member(target)
                if member is None:
                    return ToolResult(call.id, call.name, False, error=f"未知收件人：{target}")
                ids = [member.agent_id]
            mailbox = Mailbox(team.config_dir / "mailbox")
            for identifier in ids:
                await mailbox.write(
                    identifier,
                    Message(
                        context.member_name,
                        require_string(call, "content"),
                        message_type,
                        str(call.arguments.get("request_id", "")),
                        call.arguments.get("approve"),
                    ),
                )
            return _success(
                call,
                {
                    "delivered": len(ids),
                    "delivered_to": ids,
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            )
        try:
            item = await self.manager.send_message(
                require_string(call, "name"), require_string(call, "message")
            )
        except ValueError as exc:
            return ToolResult(call.id, call.name, False, error=str(exc))
        return _success(call, {"task_id": item.id, "status": "async_launched"})


__all__ = ["AgentTool", "SendMessageTool", "TaskGetTool", "TaskListTool", "TaskStopTool"]
