from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from tuicodingagent.models import ChatMessage, ToolCall, ToolResult
from tuicodingagent.session import (
    SessionContext,
    Writer,
    cleanup_expired_sessions,
    new_session_id,
    restore_session,
    scan_sessions,
)


def test_session_id_and_writer_round_trip(tmp_path: Path) -> None:
    session_id = new_session_id(datetime(2026, 6, 1, 14, 30, 22))
    assert session_id.startswith("20260601-143022-") and len(session_id) == 20
    context = SessionContext.create(tmp_path, session_id)
    with Writer(context, "model-x") as writer:
        writer.append(ChatMessage("user", "一个足够明确的会话标题"), ts=100)
        writer.append(
            ChatMessage(
                "assistant",
                tool_calls=(ToolCall("c1", "read_file", {"path": "a.py"}),),
            ),
            ts=101,
        )
        writer.append(
            ChatMessage(
                "tool",
                tool_results=(ToolResult("c1", "read_file", True, output="ok"),),
            ),
            ts=102,
        )
        writer.replace((ChatMessage("assistant", "摘要"), ChatMessage("user", "继续")))

    rows = [
        json.loads(line)
        for line in context.conversation_path.read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0]["model"] == "model-x"
    assert rows[3]["type"] == "compact"
    info = scan_sessions(tmp_path)[0]
    restored = restore_session(info, now=103)
    assert [item.content for item in restored.messages] == ["摘要", "继续"]


def test_scan_skips_old_format_bad_lines_and_truncates_orphan(tmp_path: Path) -> None:
    old = tmp_path / ".tuicodingagent" / "sessions" / "1717000000-abc123"
    old.mkdir(parents=True)
    (old / "conversation.jsonl").write_text("{}\n", encoding="utf-8")
    context = SessionContext.create(tmp_path, "20260601-143022-a1b2")
    context.session_dir.mkdir(parents=True)
    context.conversation_path.write_text(
        '{"role":"user","content":"标题","ts":1,"model":"m"}\n'
        "bad json\n"
        '{"role":"assistant","tool_calls":[{"id":"x","name":"bad","arguments":{}}],"ts":2}\n',
        encoding="utf-8",
    )

    sessions = scan_sessions(tmp_path)
    assert len(sessions) == 1 and sessions[0].model == "m"
    restored = restore_session(sessions[0], now=2)
    assert restored.messages == (ChatMessage("user", "标题"),)


def test_restore_adds_pause_notice(tmp_path: Path) -> None:
    context = SessionContext.create(tmp_path, "20260601-143022-a1b2")
    context.session_dir.mkdir(parents=True)
    context.conversation_path.write_text(
        '{"role":"user","content":"标题","ts":1,"model":"m"}\n', encoding="utf-8"
    )
    restored = restore_session(scan_sessions(tmp_path)[0], now=8 * 3600)
    assert restored.messages[-1].content.startswith("[系统提示] 本会话已暂停")


def test_cleanup_only_new_format(tmp_path: Path) -> None:
    sessions = tmp_path / ".tuicodingagent" / "sessions"
    old_new = sessions / "20260101-000000-a1b2"
    recent = sessions / "20260601-000000-a1b2"
    legacy = sessions / "1717000000-abc123"
    for path in (old_new, recent, legacy):
        path.mkdir(parents=True)
    removed = cleanup_expired_sessions(tmp_path, now=datetime(2026, 6, 15))
    assert removed == 1
    assert not old_new.exists()
    assert recent.exists() and legacy.exists()


def test_provider_items_round_trip(tmp_path: Path) -> None:
    context = SessionContext.create(tmp_path, "20260601-143022-a1b2")
    item = {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"}
    with Writer(context, "gpt-5.5") as writer:
        writer.append(ChatMessage("user", "任务"), ts=1)
        writer.append(ChatMessage("assistant", "完成", provider_items=(item,)), ts=2)

    restored = restore_session(scan_sessions(tmp_path)[0], now=2)
    assert restored.messages[-1].provider_items == (item,)
