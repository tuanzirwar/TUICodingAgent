from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tuicodingagent.models import (
    ChatMessage,
    PermissionChoice,
    PermissionMode,
    ProviderConfig,
    ProviderEvent,
    ProviderEventType,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from tuicodingagent.runtime import ConversationRuntime
from tuicodingagent.subagent import (
    Catalog,
    Definition,
    Manager,
    Service,
    build_forked_messages,
)
from tuicodingagent.subagent.definitions import parse_definition
from tuicodingagent.tools.registry import ToolRegistry
from tuicodingagent.tools.subagent import (
    AgentTool,
    SendMessageTool,
    TaskGetTool,
    TaskListTool,
    TaskStopTool,
)

CONFIG = ProviderConfig("fake", "openai", "sonnet", "secret")


class Provider:
    def __init__(self, replies: list[list[ProviderEvent]], delay: float = 0.0) -> None:
        self.config = CONFIG
        self.replies = replies
        self.delay = delay
        self.requests: list[tuple[object, tuple[ChatMessage, ...], tuple[object, ...]]] = []

    async def stream(self, prompt, messages, tools) -> AsyncIterator[ProviderEvent]:
        self.requests.append((prompt, tuple(messages), tuple(tools)))
        if self.delay:
            await asyncio.sleep(self.delay)
        for event in self.replies.pop(0):
            yield event

    async def close(self) -> None:
        return None


class FailingProvider(Provider):
    async def stream(self, prompt, messages, tools) -> AsyncIterator[ProviderEvent]:
        del prompt, messages, tools
        raise RuntimeError("SUBAGENT_BOOM")
        yield  # pragma: no cover


def text(value: str) -> ProviderEvent:
    return ProviderEvent(ProviderEventType.TEXT_DELTA, text=value)


def tool(identifier: str, name: str, **arguments: object) -> ProviderEvent:
    return ProviderEvent(
        ProviderEventType.TOOL_CALL,
        tool_call=ToolCall(identifier, name, arguments),
    )


class FakeBash:
    read_only = False
    definition = ToolDefinition("bash", "测试命令", {"type": "object"})

    async def execute(self, call: ToolCall) -> ToolResult:
        return ToolResult(call.id, call.name, True, output="BASH_OK")


def test_catalog_priority_and_invalid_fallback(tmp_path: Path, capsys) -> None:
    user = tmp_path / "user"
    project = tmp_path / ".tuicodingagent" / "agents"
    user.mkdir()
    project.mkdir(parents=True)
    (user / "explore.md").write_text(
        "---\nname: explore\ndescription: user\n---\nUSER", encoding="utf-8"
    )
    (project / "explore.md").write_text(
        "---\nname: explore\ndescription: project\nmodel: bad\npermissionMode: bad\n---\nPROJECT",
        encoding="utf-8",
    )
    (project / "broken.md").write_text("bad", encoding="utf-8")
    catalog = Catalog(tmp_path, user_root=user)
    catalog.load_all()
    resolved = catalog.resolve("Explore")
    assert resolved is not None
    assert resolved.prompt == "PROJECT"
    assert resolved.model == "inherit"
    assert resolved.permission_mode == PermissionMode.DEFAULT
    assert "加载失败" in capsys.readouterr().err


def test_builtin_catalog_and_strict_definition(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path, user_root=tmp_path / "missing")
    assert {item.name for item in catalog.load_all()} >= {
        "general-purpose",
        "explore",
        "plan",
    }
    with pytest.raises(ValueError, match="name"):
        parse_definition("---\nname: BAD\ndescription: x\n---\nbody", source="x")


def test_build_forked_messages_closes_pending_and_preserves_prefix() -> None:
    call = ToolCall("pending", "read_file", {"path": "x"})
    parent = (
        ChatMessage("user", "原任务"),
        ChatMessage("assistant", tool_calls=(call,)),
    )
    forked = build_forked_messages(parent, "检查结果")
    assert forked[:2] == parent
    assert forked[2].tool_results[0].metadata["fork_placeholder"] is True
    assert forked[-1].content.startswith("<fork_boilerplate>")


@pytest.mark.asyncio
async def test_defined_agent_runs_inline_and_hides_agent(tmp_path: Path) -> None:
    child = Provider([[text("EXPLORE_OK")]])
    parent = ConversationRuntime(Provider([[text("unused")]]), workspace=tmp_path)
    registry = ToolRegistry()
    agent_tool = AgentTool()
    registry.register(agent_tool)
    catalog = Catalog(tmp_path, user_root=tmp_path / "missing")
    catalog.load_all()
    manager = Manager(parent.queue_reminder)
    service = Service(
        workspace=tmp_path,
        registry=registry,
        catalog=catalog,
        manager=manager,
        parent_runtime=parent,
        provider_factory=lambda _: child,
        providers=(CONFIG,),
    )
    agent_tool.set_service(service)
    result = await agent_tool.execute(
        ToolCall(
            "a",
            "Agent",
            {"prompt": "探索", "description": "探索", "subagent_type": "explore"},
        )
    )
    assert result.success and result.output == "EXPLORE_OK"
    assert all(item.name != "Agent" for item in child.requests[0][2])
    assert "Explore 子 Agent" in str(child.requests[0][0])


@pytest.mark.asyncio
async def test_unknown_agent_and_background_disabled_fork(tmp_path: Path) -> None:
    parent = ConversationRuntime(Provider([]), workspace=tmp_path)
    catalog = Catalog(tmp_path, user_root=tmp_path / "missing")
    catalog.load_all()
    service = Service(
        workspace=tmp_path,
        registry=ToolRegistry(),
        catalog=catalog,
        manager=Manager(),
        parent_runtime=parent,
        provider_factory=lambda _: Provider([]),
        providers=(CONFIG,),
        background_enabled=False,
    )
    tool_impl = AgentTool(service)
    unknown = await tool_impl.execute(
        ToolCall(
            "u",
            "Agent",
            {"prompt": "x", "description": "x", "subagent_type": "missing"},
        )
    )
    disabled = await tool_impl.execute(ToolCall("f", "Agent", {"prompt": "x", "description": "x"}))
    assert not unknown.success and "未知 subagent_type" in unknown.error
    assert not disabled.success and "后台禁用" in disabled.error


@pytest.mark.asyncio
async def test_dont_ask_and_approval_bridge(tmp_path: Path) -> None:
    registry = ToolRegistry((FakeBash(),))
    parent = ConversationRuntime(Provider([]), workspace=tmp_path)
    catalog = Catalog(tmp_path, user_root=tmp_path / "missing")
    catalog.load_all()
    providers = [
        Provider([[tool("b1", "bash", command="ok")], [text("DONT_OK")]]),
        Provider([[tool("b2", "bash", command="ok")], [text("ASK_OK")]]),
    ]
    manager = Manager()
    approvals: list[str] = []

    async def approve(name: str, call: ToolCall, reason: str) -> PermissionChoice:
        approvals.append(f"{name}:{call.name}:{reason}")
        return PermissionChoice.ALLOW_ONCE

    manager.approval_handler = approve
    service = Service(
        workspace=tmp_path,
        registry=registry,
        catalog=catalog,
        manager=manager,
        parent_runtime=parent,
        provider_factory=lambda _: providers.pop(0),
        providers=(CONFIG,),
    )
    dont = Definition("custom", "x", "prompt", permission_mode=PermissionMode.DONT_ASK)
    ask = Definition("custom", "x", "prompt", permission_mode=PermissionMode.DEFAULT)
    assert await service.run_definition(dont, "task") == "DONT_OK"
    assert await service.run_definition(ask, "task") == "ASK_OK"
    assert len(approvals) == 1 and "custom:bash" in approvals[0]


@pytest.mark.asyncio
async def test_background_notification_and_task_tools(tmp_path: Path) -> None:
    child = Provider([[text("BG_OK")]], delay=0.01)
    parent = ConversationRuntime(Provider([]), workspace=tmp_path)
    catalog = Catalog(tmp_path, user_root=tmp_path / "missing")
    catalog.load_all()
    manager = Manager(parent.queue_reminder)
    service = Service(
        workspace=tmp_path,
        registry=ToolRegistry(),
        catalog=catalog,
        manager=manager,
        parent_runtime=parent,
        provider_factory=lambda _: child,
        providers=(CONFIG,),
    )
    launched = await service.launch(
        prompt="后台",
        description="后台",
        subagent_type="general-purpose",
        run_in_background=True,
        name="worker",
    )
    assert not isinstance(launched, str)
    await launched.handle
    assert launched.status == "completed"
    listing = await TaskListTool(manager).execute(ToolCall("l", "TaskList", {}))
    detail = await TaskGetTool(manager).execute(ToolCall("g", "TaskGet", {"task_id": launched.id}))
    assert launched.id in listing.output
    assert "BG_OK" in detail.output
    assert parent._external_reminders[0].startswith("<task-notification>")


@pytest.mark.asyncio
async def test_task_stop_and_send_message(tmp_path: Path) -> None:
    slow = Provider([[text("late")]], delay=1)
    parent = ConversationRuntime(Provider([]), workspace=tmp_path)
    manager = Manager()
    running = manager.launch(parent.__class__(slow, workspace=tmp_path), "x", name="slow")
    stopped = await TaskStopTool(manager).execute(
        ToolCall("s", "TaskStop", {"task_id": running.id})
    )
    assert stopped.success
    await asyncio.gather(running.handle, return_exceptions=True)
    assert running.status == "cancelled"
    invalid_type = await SendMessageTool(manager).execute(
        ToolCall(
            "invalid",
            "SendMessage",
            {"name": "missing", "message": "x", "type": "unknown"},
        )
    )
    assert not invalid_type.success and "未知消息类型" in invalid_type.error
    missing = await SendMessageTool(manager).execute(
        ToolCall("m", "SendMessage", {"name": "missing", "message": "x"})
    )
    assert not missing.success


@pytest.mark.asyncio
async def test_fork_is_background_and_nested_agent_is_blocked(tmp_path: Path) -> None:
    child = Provider(
        [
            [tool("nested", "Agent", prompt="x", description="x")],
            [text("Scope: NESTED_BLOCK_OK")],
        ]
    )
    parent = ConversationRuntime(Provider([]), workspace=tmp_path)
    parent.seed_history((ChatMessage("user", "父上下文"),))
    registry = ToolRegistry((AgentTool(),))
    catalog = Catalog(tmp_path, user_root=tmp_path / "missing")
    catalog.load_all()
    manager = Manager()
    service = Service(
        workspace=tmp_path,
        registry=registry,
        catalog=catalog,
        manager=manager,
        parent_runtime=parent,
        provider_factory=lambda _: child,
        providers=(CONFIG,),
    )
    launched = await service.launch(prompt="fork", description="fork")
    assert not isinstance(launched, str)
    await launched.handle
    assert launched.result == "Scope: NESTED_BLOCK_OK"
    assert child.requests[0][1][0].content == "父上下文"
    assert any(item.name == "Agent" for item in child.requests[0][2])
    tool_result = child.requests[1][1][-1].tool_results[0]
    assert "不能再启动 Agent" in tool_result.error


@pytest.mark.asyncio
async def test_auto_and_manual_background_switch(tmp_path: Path) -> None:
    providers = [
        Provider([[text("AUTO_OK")]], delay=0.05),
        Provider([[text("MANUAL_OK")]], delay=0.05),
    ]
    parent = ConversationRuntime(Provider([]), workspace=tmp_path)
    catalog = Catalog(tmp_path, user_root=tmp_path / "missing")
    catalog.load_all()
    manager = Manager(parent.queue_reminder)
    service = Service(
        workspace=tmp_path,
        registry=ToolRegistry(),
        catalog=catalog,
        manager=manager,
        parent_runtime=parent,
        provider_factory=lambda _: providers.pop(0),
        providers=(CONFIG,),
        auto_background_seconds=0.005,
    )
    automatic = await service.launch(
        prompt="auto", description="auto", subagent_type="general-purpose"
    )
    assert not isinstance(automatic, str)
    assert automatic.launch_status == "timed_out_to_background"
    await automatic.handle

    service.auto_background_seconds = 1
    launching = asyncio.create_task(
        service.launch(prompt="manual", description="manual", subagent_type="general-purpose")
    )
    await asyncio.sleep(0)
    assert service.move_foreground_to_background()
    manual = await launching
    assert not isinstance(manual, str)
    assert manual.launch_status == "manual_to_background"
    await manual.handle


@pytest.mark.asyncio
async def test_failed_notification_and_send_message_success(tmp_path: Path) -> None:
    parent = ConversationRuntime(Provider([]), workspace=tmp_path)
    manager = Manager(parent.queue_reminder)
    failed = manager.launch(
        ConversationRuntime(FailingProvider([]), workspace=tmp_path), "fail", name="bad"
    )
    await failed.handle
    assert failed.status == "failed"
    assert "SUBAGENT_BOOM" in parent._external_reminders[-1]

    reusable = Provider([[text("FIRST")], [text("SECOND")]])
    first = manager.launch(
        ConversationRuntime(reusable, workspace=tmp_path), "first", name="worker"
    )
    await first.handle
    sent = await SendMessageTool(manager).execute(
        ToolCall("send", "SendMessage", {"name": "worker", "message": "second"})
    )
    assert sent.success
    second = manager.find_name("worker")
    assert second is not None and second.id != first.id
    await second.handle
    assert second.result == "SECOND"


def test_agent_schema_stays_stable_when_catalog_changes(tmp_path: Path) -> None:
    registry = ToolRegistry((AgentTool(),))
    before = registry.definitions()
    root = tmp_path / ".tuicodingagent" / "agents"
    root.mkdir(parents=True)
    (root / "custom.md").write_text(
        "---\nname: custom\ndescription: x\n---\ncustom", encoding="utf-8"
    )
    catalog = Catalog(tmp_path, user_root=tmp_path / "missing")
    catalog.load_all()
    assert catalog.resolve("custom") is not None
    assert registry.definitions() == before
