"""租约归属、失败释放、持久化失败与跨迭代消息保留。"""
# ruff: noqa: ASYNC240

import asyncio
from pathlib import Path

import pytest

from tuicodingagent.eval.harness_regression import ScriptedProvider
from tuicodingagent.eval.harness_team_workflow import git
from tuicodingagent.models import ProviderEvent, ProviderEventType, RuntimeEventType, ToolCall
from tuicodingagent.runtime import ConversationRuntime
from tuicodingagent.session import SessionContext, Writer
from tuicodingagent.subagent.manager import Manager as SubAgentManager
from tuicodingagent.team import Manager, TeammateInfo
from tuicodingagent.team.context import TeammateContext, with_teammate
from tuicodingagent.team.delivery import MailboxDelivery
from tuicodingagent.team.mailbox import Mailbox, Message
from tuicodingagent.team.tasks import TaskStore
from tuicodingagent.tools.registry import create_default_registry
from tuicodingagent.tools.team import TaskUpdateTool


async def test_expired_receiver_cannot_ack_new_owner(tmp_path: Path) -> None:
    mailbox = Mailbox(tmp_path)
    identifier = await mailbox.write("alice", Message("lead", "must survive"))
    old = await mailbox.claim("alice", ack_timeout=0.03)
    await asyncio.sleep(0.05)
    new = await Mailbox(tmp_path).claim("alice", ack_timeout=5)
    assert [m.message_id for m in new] == [identifier]
    assert await mailbox.settle_claims("alice", old, "ack") == set()
    assert await mailbox.settle_claims("alice", new, "ack") == {identifier}


async def test_message_survives_later_model_iterations(tmp_path: Path) -> None:
    mailbox = Mailbox(tmp_path / "mailbox")
    await mailbox.write("alice", Message("lead", "PINNED: known fact 37"))
    (tmp_path / "input.txt").write_text("input", encoding="utf-8")
    provider = ScriptedProvider([ToolCall("read", "read_file", {"path": "input.txt"}), "done"])
    runtime = ConversationRuntime(
        provider,
        workspace=tmp_path,
        tool_registry=create_default_registry(tmp_path),
        session_writer=Writer(SessionContext.create(tmp_path), "fixture"),
    )
    delivery = MailboxDelivery(runtime, mailbox, "alice")
    delivery.bind()
    try:
        _ = [e async for e in runtime.chat("handle")]
        assert len(provider.requests) == 2
        assert all(
            any("known fact 37" in message.content for message in messages)
            for _, messages, _ in provider.requests
        )
        assert await mailbox.read("alice") == []
        assert not delivery.claimed and delivery._heartbeat is None
    finally:
        await runtime.close()


async def test_storage_failure_does_not_ack_input(tmp_path: Path, monkeypatch) -> None:
    mailbox = Mailbox(tmp_path / "mailbox")
    await mailbox.write("alice", Message("lead", "durable input required"))
    writer = Writer(SessionContext.create(tmp_path), "fixture")

    def failed(_record):
        writer.persistence_healthy = False
        return False

    monkeypatch.setattr(writer, "_write", failed)
    runtime = ConversationRuntime(
        ScriptedProvider(["done"]), workspace=tmp_path, session_writer=writer
    )
    MailboxDelivery(runtime, mailbox, "alice").bind()
    try:
        events = [e async for e in runtime.chat("handle")]
        assert events[-1].type == RuntimeEventType.FAILED
        assert len(await mailbox.read("alice")) == 1
        assert sum(message.content == "handle" for message in runtime.history) == 1
    finally:
        await runtime.close()


async def test_cancelled_receiver_releases_claim(tmp_path: Path) -> None:
    started = asyncio.Event()

    class WaitingProvider(ScriptedProvider):
        async def stream(self, *args, **kwargs):
            started.set()
            await asyncio.Event().wait()
            yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="")

    mailbox = Mailbox(tmp_path / "mailbox")
    await mailbox.write("alice", Message("lead", "retry after cancel"))
    runtime = ConversationRuntime(WaitingProvider(), workspace=tmp_path)
    delivery = MailboxDelivery(runtime, mailbox, "alice")
    delivery.bind()

    async def consume():
        return [e async for e in runtime.chat("handle")]

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert len(await mailbox.read("alice")) == 1
    assert not runtime._busy and delivery._heartbeat is None
    await runtime.close()


async def test_background_limit_releases_mail_before_reporting_done(tmp_path: Path) -> None:
    mailbox = Mailbox(tmp_path / "mailbox")
    await mailbox.write("alice", Message("lead", "任务达到预算后必须及时归还"))
    runtime = ConversationRuntime(
        ScriptedProvider([ToolCall("read", "read_file", {"path": "missing.txt"})]),
        workspace=tmp_path,
        tool_registry=create_default_registry(tmp_path),
        max_iterations=1,
    )
    delivery = MailboxDelivery(runtime, mailbox, "alice")
    delivery.bind()
    manager = SubAgentManager()
    task = manager.launch(runtime, "处理并归还消息")
    try:
        await task.handle
        assert task.status == "failed"
        assert not runtime._busy
        assert delivery._heartbeat is None and not delivery.claimed
        assert len(await mailbox.read("alice")) == 1
    finally:
        await runtime.close()


async def test_heartbeat_keeps_slow_receiver_exclusive(tmp_path: Path) -> None:
    started = asyncio.Event()
    finish = asyncio.Event()

    class SlowProvider(ScriptedProvider):
        async def stream(self, *args, **kwargs):
            started.set()
            await finish.wait()
            yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="done")

    mailbox = Mailbox(tmp_path / "mailbox")
    await mailbox.write("alice", Message("lead", "slow input"))
    runtime = ConversationRuntime(SlowProvider(), workspace=tmp_path)
    delivery = MailboxDelivery(runtime, mailbox, "alice", ack_timeout=0.3)
    delivery.bind()

    async def consume():
        return [e async for e in runtime.chat("handle")]

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(started.wait(), 5)
        await asyncio.sleep(0.6)
        assert await Mailbox(mailbox.root).claim("alice") == []
        finish.set()
        events = await asyncio.wait_for(task, 5)
        assert events[-1].type == RuntimeEventType.COMPLETED
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


async def test_reopening_dependency_cannot_invalidate_completed_consumer(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.json")
    first = await store.create("upstream")
    second = await store.create("downstream", blocked_by=[first.id])
    await store.update(first.id, status="completed")
    await store.update(second.id, status="completed")
    with pytest.raises(ValueError):
        await store.update(first.id, status="pending")
    assert store.get(first.id).status == "completed"


async def test_committing_broken_code_cannot_bypass_task_completion(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    await git(repo, "init", "-q")
    await git(repo, "config", "user.name", "Fixture")
    await git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    await git(repo, "add", "broken.py")
    await git(repo, "commit", "-qm", "broken tracked artifact")
    manager = Manager(tmp_path / "teams")
    team = await manager.create("complete-gate")
    await team.add_member(TeammateInfo("alice", "agent-a", worktree_path=str(repo)))
    store = TaskStore(team.config_dir / "tasks.json")
    task = await store.create("repair", assignee="alice")
    with with_teammate(TeammateContext(team.name, "agent-a", "alice")):
        result = await TaskUpdateTool(manager).execute(
            ToolCall("complete", "TaskUpdate", {"task_id": task.id, "status": "completed"})
        )
    assert not result.success and result.metadata["verification_failed"]
    assert store.get(task.id).status == "pending"
