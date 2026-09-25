from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import Static

from tuicodingagent.coordinator import COORDINATOR_ALLOWED_TOOLS, env_truthy, is_enabled
from tuicodingagent.models import (
    AppConfig,
    ChatMessage,
    PermissionMode,
    ProviderConfig,
    ProviderEvent,
    ProviderEventType,
    ToolCall,
    ToolDefinition,
)
from tuicodingagent.runtime import ConversationRuntime
from tuicodingagent.subagent import Catalog, Definition
from tuicodingagent.subagent import Manager as SubAgentManager
from tuicodingagent.subagent import Service as SubAgentService
from tuicodingagent.team import (
    AgentNameRegistry,
    BackendType,
    Mailbox,
    Manager,
    Message,
    TaskStore,
    TeamHasActiveMembersError,
    TeammateInfo,
    detect_backend,
    sanitize_name,
)
from tuicodingagent.team.backend import ITerm2Backend, SpawnRequest, TmuxBackend
from tuicodingagent.team.context import TeammateContext, with_teammate
from tuicodingagent.team.mailbox import MESSAGE_TYPES
from tuicodingagent.team.service import TeamService
from tuicodingagent.tools import create_default_registry
from tuicodingagent.tools.team import TaskUpdateTool, TeamSendMessageTool, TeamTaskListTool
from tuicodingagent.tui.app import TUICodingAgentApp
from tuicodingagent.worktree import Manager as WorktreeManager


def test_sanitize_and_backend_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    assert sanitize_name(" Refactor Auth! ") == "refactor-auth"
    with pytest.raises(ValueError):
        sanitize_name("!!!")
    monkeypatch.setenv("TMUX", "session")
    assert detect_backend() == BackendType.TMUX
    monkeypatch.delenv("TMUX")
    monkeypatch.setenv("TERM_PROGRAM", "other")
    monkeypatch.setattr("shutil.which", lambda _name: None)
    assert detect_backend() == BackendType.IN_PROCESS


@pytest.mark.asyncio
async def test_tmux_backend_spawn_wake_and_kill_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class Process:
        returncode = 0

        async def communicate(self):
            return b"%7\n", b""

        async def wait(self):
            return 0

    async def create(*args, **kwargs):
        calls.append((args, kwargs))
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setenv("TMUX", "session")
    request = SpawnRequest(
        "team-a",
        "alice",
        "agent-a",
        tmp_path,
        tmp_path / "session",
        agent_type="explore",
        model="sonnet",
        plan_mode_required=True,
    )
    backend = TmuxBackend()
    assert await backend.spawn(request) == ("%7", "agent-a")
    spawn_args, spawn_kwargs = calls[0]
    assert spawn_args[:7] == (
        "tmux",
        "split-window",
        "-h",
        "-P",
        "-F",
        "#{pane_id}",
        "--",
    )
    assert "--team-member" in spawn_args
    assert spawn_args[spawn_args.index("--agent-id") + 1] == "agent-a"
    assert spawn_args[-5:] == (
        "--agent-type",
        "explore",
        "--model",
        "sonnet",
        "--plan-mode",
    )
    assert spawn_kwargs["cwd"] == tmp_path
    await backend.wake("%7", "agent-a")
    await backend.kill("%7", "agent-a")
    assert calls[1][0] == ("tmux", "send-keys", "-t", "%7", "", "Enter")
    assert calls[2][0] == ("tmux", "kill-pane", "-t", "%7")


@pytest.mark.asyncio
async def test_external_tmux_and_iterm_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, ...]] = []

    class Process:
        returncode = 0

        async def communicate(self):
            return b"pane-1\n", b""

        async def wait(self):
            return 0

    async def create(*args, **_kwargs):
        calls.append(args)
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.delenv("TMUX", raising=False)
    request = SpawnRequest("team-a", "alice", "agent-a", tmp_path, tmp_path / "session")
    assert await TmuxBackend().spawn(request) == ("pane-1", "agent-a")
    assert calls[0][:6] == (
        "tmux",
        "new-session",
        "-d",
        "-P",
        "-F",
        "#{pane_id}",
    )
    assert await ITerm2Backend().spawn(request) == ("pane-1", "agent-a")
    assert calls[1][:5] == ("it2", "split", "--new-pane", "--command", calls[1][4])
    assert "--team-member" in str(calls[1][4])
    await ITerm2Backend().wake("pane-1", "agent-a")
    await ITerm2Backend().kill("pane-1", "agent-a")
    assert calls[2] == ("it2", "send-text", "--pane", "pane-1", "")
    assert calls[3] == ("it2", "close-pane", "--pane", "pane-1")


@pytest.mark.asyncio
async def test_manager_create_suffix_recover_and_delete_guard(tmp_path: Path) -> None:
    manager = Manager(tmp_path / "teams")
    first = await manager.create("Refactor Auth", "认证重构")
    second = await manager.create("Refactor Auth")
    assert first.sanitized_name == "refactor-auth"
    assert second.sanitized_name == "refactor-auth-2"
    assert first.members[0].name == "lead"
    await first.add_member(TeammateInfo("alice", "agent-a", is_active=True))
    with pytest.raises(TeamHasActiveMembersError):
        await manager.delete(first.name)
    recovered = Manager(tmp_path / "teams")
    assert recovered.get("refactor-auth") is not None
    assert recovered.get("refactor-auth").members[0].is_active is False
    await manager.delete(first.name, force=True)
    assert not first.config_dir.exists()


@pytest.mark.asyncio
async def test_task_store_bidirectional_dependency_and_ready(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.json")
    blocker = await store.create("准备接口")
    assert blocker.assignee == ""
    blocked = await store.create("接入接口")
    await store.update(blocked.id, add_blocked_by=[blocker.id])
    assert store.get(blocker.id).blocks == [blocked.id]
    assert store.list("pending")[1]["is_ready"] is False
    await store.update(blocker.id, status="completed")
    assert store.list("pending")[0]["is_ready"] is True
    await store.update(blocked.id, remove_blocked_by=[blocker.id], assignee="alice")
    assert store.get(blocked.id).blocked_by == []
    assert store.get(blocker.id).blocks == []
    payload = json.loads((tmp_path / "tasks.json").read_text(encoding="utf-8"))
    assert isinstance(payload["tasks"], list)
    assert payload["tasks"][0]["id"].startswith("task_")
    assert not (tmp_path / "tasks.lock").exists()
    with pytest.raises(ValueError, match="任务状态无效"):
        await store.update(blocker.id, status="cancelled")
    with pytest.raises(ValueError, match="任务状态无效"):
        store.list("cancelled")


def test_team_tool_schemas_use_exact_status_and_message_types() -> None:
    statuses = ["pending", "in_progress", "completed", "blocked"]
    update_properties = TaskUpdateTool.definition.input_schema["properties"]
    list_properties = TeamTaskListTool.definition.input_schema["properties"]
    message_properties = TeamSendMessageTool.definition.input_schema["properties"]
    assert update_properties["status"]["enum"] == statuses
    assert list_properties["status"]["enum"] == statuses
    assert message_properties["type"]["enum"] == sorted(MESSAGE_TYPES)


def test_team_context_contains_current_member_roster(tmp_path: Path) -> None:
    member = TeammateInfo(
        "alice",
        "agent-a",
        agent_type="explore",
        worktree_path=str(tmp_path),
    )
    prompt = TeamService._prompt_suffix(
        "audit",
        member,
        [TeammateInfo("lead", "lead", agent_type="lead"), member],
    )
    assert "当前团队成员: lead(lead), alice(explore)" in prompt


@pytest.mark.asyncio
async def test_approved_shutdown_response_signals_pane_loop(tmp_path: Path) -> None:
    manager = Manager(tmp_path / "teams")
    team = await manager.create("shutdown")
    member = TeammateInfo("alice", "agent-a")
    await team.add_member(member)
    shutdown_event = asyncio.Event()
    tool = TeamSendMessageTool(manager, shutdown_event=shutdown_event)
    with with_teammate(TeammateContext(team.name, member.agent_id, member.name)):
        result = await tool.execute(
            ToolCall(
                "shutdown",
                "SendMessage",
                {
                    "to": "lead",
                    "content": "同意退出",
                    "type": "shutdown_response",
                    "approve": True,
                },
            )
        )
    assert result.success
    assert shutdown_event.is_set()
    message = (await Mailbox(team.config_dir / "mailbox").read("lead"))[0]
    assert message.type == "shutdown_response" and message.approve is True


@pytest.mark.asyncio
async def test_mailbox_concurrent_atomic_and_stale_lock(tmp_path: Path) -> None:
    mailbox = Mailbox(tmp_path / "mailbox")
    await asyncio.gather(
        *(mailbox.write("alice", Message("lead", f"m-{index}")) for index in range(10))
    )
    assert len(mailbox.unread("alice")) == 10
    lock = mailbox.path("alice").with_suffix(".lock")
    lock.write_text("stale", encoding="utf-8")
    old = time.time() - 11
    os.utime(lock, (old, old))
    await mailbox.write("alice", Message("lead", "after stale"))
    assert len(json.loads(mailbox.path("alice").read_text(encoding="utf-8"))) == 11
    assert len(mailbox.unread("alice", mark_read=True)) == 11
    assert mailbox.unread("alice") == []
    await mailbox.write("alice", Message("lead", "new"))
    unread = await mailbox.read("alice")
    assert [item.text for item in unread] == ["new"]
    await mailbox.mark_read("alice", [0])
    assert await mailbox.read("alice") == []
    raw = json.loads(mailbox.path("alice").read_text(encoding="utf-8"))[-1]
    assert set(raw) == {"from", "text", "timestamp", "read"}


@pytest.mark.asyncio
async def test_sqlite_mailbox_priority_dedup_ack_retry_and_cancel(tmp_path: Path) -> None:
    mailbox = Mailbox(tmp_path / "mailbox")
    low = Message("lead", "低优先级", priority=1, dedup_key="same", plan_version=2)
    duplicate = Message("lead", "不会重复入队", priority=9, dedup_key="same")
    high = Message("lead", "高优先级", priority=10, max_retry=1)
    low_id = await mailbox.write("agent-a", low)
    assert await mailbox.write("agent-a", duplicate) == low_id
    high_id = await mailbox.write("agent-a", high)

    claimed = await mailbox.claim("agent-a", limit=2, ack_timeout=30)
    assert [item.text for item in claimed] == ["高优先级", "低优先级"]
    assert claimed[1].plan_version == 2
    await mailbox.ack("agent-a", [low_id])
    assert await mailbox.retry("agent-a", high_id)
    assert [item.message_id for item in await mailbox.read("agent-a")] == [high_id]
    assert await mailbox.cancel("agent-a", high_id)
    assert await mailbox.read("agent-a") == []


@pytest.mark.asyncio
async def test_sqlite_mailbox_recovers_unacked_claim_after_restart(tmp_path: Path) -> None:
    root = tmp_path / "mailbox"
    mailbox = Mailbox(root)
    message_id = await mailbox.write("agent-a", Message("lead", "恢复", max_retry=1))
    assert await mailbox.claim("agent-a", ack_timeout=0.01)
    await asyncio.sleep(0.02)

    recovered = Mailbox(root)
    pending = await recovered.read("agent-a")
    assert [item.message_id for item in pending] == [message_id]
    assert pending[0].retry_count == 1


@pytest.mark.asyncio
async def test_manager_polls_and_marks_lead_mail(tmp_path: Path) -> None:
    manager = Manager(tmp_path / "teams")
    team = await manager.create("mail-team")
    mailbox = Mailbox(team.config_dir / "mailbox")
    await mailbox.write("lead", Message("alice", "完成报告"))
    messages = await manager.poll_lead_mailboxes()
    assert [(item.team_name, item.sender, item.text) for item in messages] == [
        ("mail-team", "alice", "完成报告")
    ]
    assert await mailbox.read("lead") == []


@pytest.mark.asyncio
async def test_team_spawn_setup_failure_rolls_back_all_resources(tmp_path: Path) -> None:
    manager = Manager(tmp_path / "teams")
    team = await manager.create("rollback")
    worktree_path = tmp_path / "worktrees" / "team-rollback+alice"
    worktree_path.mkdir(parents=True)

    class Worktrees:
        removed = False

        async def create(self, name, start_point, *, manual):
            assert (name, start_point, manual) == ("team-rollback/alice", "HEAD", True)
            return SimpleNamespace(
                name=name, path=worktree_path, branch="worktree-team-rollback+alice"
            )

        def list(self):
            return [SimpleNamespace(name="team-rollback/alice", path=worktree_path)]

        async def remove(self, name, options):
            assert name == "team-rollback/alice" and options.discard_changes
            self.removed = True

    class Catalog:
        @staticmethod
        def resolve(name):
            assert name == "general-purpose"
            return Definition("general-purpose", "测试", "提示")

    class Subagents:
        catalog = Catalog()

        @staticmethod
        def _build_runtime(*_args, **_kwargs):
            raise RuntimeError("构造失败")

    worktrees = Worktrees()
    service = TeamService(manager, Subagents(), worktrees, tmp_path)
    with pytest.raises(RuntimeError, match="构造失败"):
        await service.spawn(
            team_name=team.name,
            name="alice",
            prompt="执行任务",
            subagent_type="general-purpose",
        )
    assert team.find_member("alice") is None
    assert manager.registry.resolve("alice") is None
    assert worktrees.removed is True
    session_dirs = list((tmp_path / ".tuicodingagent" / "sessions").glob("agent-*"))
    assert session_dirs == []


@pytest.mark.asyncio
async def test_in_process_team_real_git_worktree_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(
            ("git", "-C", str(repo), *args),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    git("init")
    git("config", "user.email", "team@example.com")
    git("config", "user.name", "TUICodingAgent Team Test")
    (repo / "README.md").write_text("main\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-m", "initial")
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    monkeypatch.setattr("shutil.which", lambda _name: None)

    provider_config = ProviderConfig("team", "openai", "model", "secret")

    class TeamProvider:
        def __init__(self) -> None:
            self.config = provider_config

        async def stream(self, _prompt, messages, _tools=()):
            if messages and messages[-1].role == "tool":
                yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="TEAM_WORK_DONE")
            else:
                yield ProviderEvent(
                    ProviderEventType.TOOL_CALL,
                    tool_call=ToolCall(
                        "write",
                        "write_file",
                        {"path": "team-result.txt", "content": "isolated\n"},
                    ),
                )

        async def close(self) -> None:
            return None

    registry = create_default_registry(repo)
    parent = ConversationRuntime(TeamProvider(), workspace=repo, tool_registry=registry)
    catalog = Catalog(repo, user_root=tmp_path / "missing-agents")
    catalog.load_all()
    task_manager = SubAgentManager()
    subagents = SubAgentService(
        workspace=repo,
        registry=registry,
        catalog=catalog,
        manager=task_manager,
        parent_runtime=parent,
        provider_factory=lambda _config: TeamProvider(),
        providers=(provider_config,),
    )
    team_manager = Manager(repo / ".tuicodingagent" / "teams")
    worktrees = WorktreeManager(repo)
    service = TeamService(team_manager, subagents, worktrees, repo)
    team_manager.cleanup_member = service.cleanup_member
    team = await team_manager.create("integration")
    launched = await service.spawn(
        team_name=team.name,
        name="alice",
        prompt="在隔离目录写文件",
        subagent_type="general-purpose",
    )
    task = task_manager.get(str(launched["task_id"]))
    assert task is not None and task.handle is not None
    await task.handle
    await asyncio.sleep(0)
    member = team.find_member("alice")
    assert member is not None and member.is_active is False
    isolated_content = await asyncio.to_thread(
        Path(member.worktree_path, "team-result.txt").read_text, encoding="utf-8"
    )
    assert isolated_content == "isolated\n"
    assert not await asyncio.to_thread((repo / "team-result.txt").exists)
    lead_messages = []
    for _ in range(50):
        lead_messages = await Mailbox(team.config_dir / "mailbox").read("lead")
        if lead_messages:
            break
        await asyncio.sleep(0.01)
    assert any("[idle] alice" in message.text for message in lead_messages)
    await team_manager.delete(team.name, force=True)
    assert team_manager.get(team.name) is None
    assert not await asyncio.to_thread(Path(member.worktree_path).exists)
    await parent.close()


@pytest.mark.asyncio
async def test_team_runtime_reads_mailbox_before_model_call(tmp_path: Path) -> None:
    manager = Manager(tmp_path / "teams")
    team = await manager.create("runtime-mail")
    member = TeammateInfo("alice", "agent-a", plan_mode_required=True)
    await team.add_member(member)
    mailbox = Mailbox(team.config_dir / "mailbox")
    await mailbox.write("agent-a", Message("lead", "继续实现功能"))
    await mailbox.write(
        "agent-a",
        Message(
            "lead",
            "计划已批准",
            type="plan_approval_response",
            request_id="request-1",
            approve=True,
        ),
    )

    class RuntimeStub:
        provider = None
        mode = PermissionMode.PLAN

        def set_reminder_provider(self, provider) -> None:
            self.provider = provider

        def set_permission_mode(self, mode: PermissionMode) -> None:
            self.mode = mode

    runtime = RuntimeStub()
    service = object.__new__(TeamService)
    service._attach_mailbox(runtime, team, member)
    assert runtime.provider is not None
    reminder = await runtime.provider()
    assert reminder.startswith("<incoming-messages>")
    assert "继续实现功能" in reminder
    assert "plan_approval_response" in reminder
    assert runtime.mode == PermissionMode.DEFAULT
    assert await mailbox.read("agent-a") == []


def test_agent_name_registry_bidirectional_and_overwrite() -> None:
    registry = AgentNameRegistry()
    registry.register("alice", "agent-1")
    assert registry.resolve("alice") == "agent-1"
    assert registry.resolve("agent-1") == "agent-1"
    assert registry.name_of("agent-1") == "alice"
    registry.register("alice", "agent-2")
    assert registry.resolve("agent-1") is None
    assert registry.name_of("agent-2") == "alice"
    registry.unregister("alice")
    assert registry.resolve("alice") is None


def test_coordinator_double_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = ProviderConfig("p", "openai", "m", "secret")
    disabled = AppConfig((provider,), features={})
    enabled = AppConfig((provider,), features={"coordinator_mode": True})
    monkeypatch.setenv("TUICODINGAGENT_COORDINATOR_MODE", "yes")
    assert not is_enabled(disabled)
    assert is_enabled(enabled)
    assert "write_file" not in COORDINATOR_ALLOWED_TOOLS
    assert "bash" in COORDINATOR_ALLOWED_TOOLS
    assert env_truthy("TRUE") and not env_truthy("on")


class CoordinatorProvider:
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    async def stream(
        self,
        system_prompt: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="完成")

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_coordinator_applies_runtime_allowlist_and_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider_config = ProviderConfig("p", "openai", "m", "secret")
    config = AppConfig((provider_config,), features={"coordinator_mode": True})
    monkeypatch.setenv("TUICODINGAGENT_COORDINATOR_MODE", "1")
    app = TUICodingAgentApp(
        config,
        workspace=tmp_path,
        provider_factory=lambda value: CoordinatorProvider(value),
    )
    async with app.run_test():
        assert app.runtime is not None
        assert app.runtime.allowed_tools == frozenset(COORDINATOR_ALLOWED_TOOLS)
        assert not app.runtime._tool_allowed("write_file")
        assert "[COORDINATOR]" in str(app.query_one("#status-bar", Static).render())
