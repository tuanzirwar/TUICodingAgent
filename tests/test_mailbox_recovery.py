"""SQLite 提交后的兼容镜像失败不能破坏真实投递。"""
# ruff: noqa: ASYNC240

import asyncio
import json
from pathlib import Path

from tuicodingagent.team import mailbox as mailbox_module
from tuicodingagent.team.mailbox import Mailbox, Message


async def test_mirror_failure_keeps_durable_message(tmp_path: Path, monkeypatch) -> None:
    mailbox = Mailbox(tmp_path)
    original = mailbox_module.os.replace

    def denied(source, target):
        raise PermissionError("人工模拟临时文件锁")

    monkeypatch.setattr(mailbox_module.os, "replace", denied)
    identifier = await mailbox.write("alice", Message("lead", "durable", dedup_key="unique"))
    assert [message.message_id for message in await mailbox.read("alice")] == [identifier]
    assert not list(tmp_path.glob("*.tmp"))
    monkeypatch.setattr(mailbox_module.os, "replace", original)
    restarted = Mailbox(tmp_path)
    assert [message.message_id for message in await restarted.claim("alice")] == [identifier]
    await restarted.ack("alice", [identifier])
    assert not await restarted.read("alice")


async def test_transient_mirror_lock_is_retried(tmp_path: Path, monkeypatch) -> None:
    mailbox = Mailbox(tmp_path)
    original = mailbox_module.os.replace
    attempts = 0

    def transient(source, target):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("人工模拟一次锁冲突")
        return original(source, target)

    monkeypatch.setattr(mailbox_module.os, "replace", transient)
    await mailbox.write("alice", Message("lead", "retried"))
    assert attempts == 2
    assert json.loads(mailbox.path("alice").read_text(encoding="utf-8"))[0]["text"] == "retried"
    assert not list(tmp_path.glob("*.tmp"))


async def test_multiple_instances_do_not_share_temporary_file(tmp_path: Path) -> None:
    boxes = [Mailbox(tmp_path), Mailbox(tmp_path)]
    await asyncio.gather(
        *(boxes[i % 2].write("alice", Message("lead", f"message-{i}")) for i in range(40))
    )
    messages = await Mailbox(tmp_path).read("alice")
    assert len(messages) == 40
    assert len({message.message_id for message in messages}) == 40
    # 镜像是兼容缓存；SQL 才是权威源。最后再同步并检查完整 JSON。
    boxes[0]._sync_mirror("alice")
    assert len(json.loads(boxes[0].path("alice").read_text(encoding="utf-8"))) == 40
    assert not list(tmp_path.glob("*.tmp"))
