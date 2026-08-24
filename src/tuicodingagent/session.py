"""追加式 JSONL 会话存档、扫描、恢复和过期清理。"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .models import ChatMessage, ToolCall, ToolResult

logger = logging.getLogger(__name__)
SESSION_RE = re.compile(r"^(\d{8}-\d{6})-([0-9a-f]{4})$")


def new_session_id(now: datetime | None = None) -> str:
    local = now or datetime.now()
    return f"{local:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


@dataclass(frozen=True, slots=True)
class SessionContext:
    workspace: Path
    session_id: str
    session_dir: Path
    spill_dir: Path
    conversation_path: Path

    @classmethod
    def create(cls, workspace: Path, session_id: str | None = None) -> SessionContext:
        root = workspace.resolve()
        identifier = session_id or new_session_id()
        directory = root / ".tuicodingagent" / "sessions" / identifier
        return cls(
            root,
            identifier,
            directory,
            directory / "tool-results",
            directory / "conversation.jsonl",
        )


class Writer:
    """持有追加句柄；每行 flush + fsync，写入失败不影响主会话。"""

    def __init__(self, context: SessionContext, model: str) -> None:
        self.context = context
        self.model = model
        self._lock = threading.Lock()
        self._handle = None
        self._first_message = True
        try:
            context.session_dir.mkdir(parents=True, exist_ok=True)
            self._first_message = (
                not context.conversation_path.exists()
                or context.conversation_path.stat().st_size == 0
            )
            self._handle = context.conversation_path.open("a", encoding="utf-8", newline="\n")
        except OSError as exc:
            logger.warning("无法打开会话存档，继续以内存模式运行：%s", type(exc).__name__)

    def append(self, message: ChatMessage, *, ts: int | None = None) -> None:
        record = message_to_record(message, ts=ts)
        with self._lock:
            if self._first_message:
                record["model"] = self.model
            if self._write(record):
                self._first_message = False

    def replace(self, messages: list[ChatMessage] | tuple[ChatMessage, ...]) -> None:
        with self._lock:
            if not self._write({"type": "compact", "ts": int(time.time())}):
                return
            for message in messages:
                self._write(message_to_record(message))

    def _write(self, record: dict[str, object]) -> bool:
        if self._handle is None:
            return False
        try:
            self._handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            self._handle.flush()
            os.fsync(self._handle.fileno())
        except OSError as exc:
            logger.warning("会话存档写入失败：%s", type(exc).__name__)
            return False
        return True

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                with suppress(OSError):
                    self._handle.close()
                self._handle = None

    def __enter__(self) -> Writer:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class SessionInfo:
    context: SessionContext
    title: str
    model: str
    modified_at: float
    size: int
    message_count: int


@dataclass(frozen=True, slots=True)
class RestoredSession:
    info: SessionInfo
    messages: tuple[ChatMessage, ...]
    last_ts: int


def scan_sessions(workspace: Path) -> list[SessionInfo]:
    root = workspace.resolve() / ".tuicodingagent" / "sessions"
    if not root.is_dir():
        return []
    found: list[SessionInfo] = []
    for directory in root.iterdir():
        if not directory.is_dir() or SESSION_RE.fullmatch(directory.name) is None:
            continue
        context = SessionContext.create(workspace, directory.name)
        path = context.conversation_path
        if not path.is_file():
            continue
        title = "（无标题）"
        model = "unknown"
        count = 0
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except (json.JSONDecodeError, UnicodeError):
                        continue
                    if record.get("type") == "compact":
                        continue
                    count += 1
                    model = str(record.get("model") or model)
                    if title == "（无标题）" and record.get("role") == "user":
                        title = _truncate_title(str(record.get("content") or ""))
            stat = path.stat()
        except OSError:
            continue
        if count == 0:
            continue
        found.append(SessionInfo(context, title, model, stat.st_mtime, stat.st_size, count))
    return sorted(found, key=lambda item: item.modified_at, reverse=True)


def restore_session(info: SessionInfo, *, now: int | None = None) -> RestoredSession:
    records: list[dict[str, object]] = []
    try:
        lines = info.context.conversation_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        lines = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") == "compact":
            records = []
            continue
        if isinstance(record, dict):
            records.append(record)
    messages: list[ChatMessage] = []
    last_ts = 0
    for record in records:
        try:
            messages.append(record_to_message(record))
            last_ts = max(last_ts, int(record.get("ts") or 0))
        except (KeyError, TypeError, ValueError):
            continue
    for index, message in enumerate(messages):
        if message.role != "assistant" or not message.tool_calls:
            continue
        if index + 1 >= len(messages) or messages[index + 1].role != "tool":
            messages = messages[:index]
            break
        expected = {call.id for call in message.tool_calls}
        actual = {result.tool_call_id for result in messages[index + 1].tool_results}
        if not expected.issubset(actual):
            messages = messages[:index]
            break
    current = now or int(time.time())
    if last_ts and current - last_ts > 6 * 3600:
        messages.append(ChatMessage("user", _pause_notice(current - last_ts)))
    return RestoredSession(info, tuple(messages), last_ts)


def cleanup_expired_sessions(
    workspace: Path, *, now: datetime | None = None, days: int = 30
) -> int:
    root = workspace.resolve() / ".tuicodingagent" / "sessions"
    if not root.is_dir():
        return 0
    current = now or datetime.now()
    removed = 0
    for directory in root.iterdir():
        match = SESSION_RE.fullmatch(directory.name)
        if not directory.is_dir() or match is None:
            continue
        try:
            created = datetime.strptime(match.group(1), "%Y%m%d-%H%M%S")
            if (current - created).total_seconds() <= days * 86400:
                continue
            shutil.rmtree(directory)
            removed += 1
        except (OSError, ValueError):
            continue
    return removed


def message_to_record(message: ChatMessage, *, ts: int | None = None) -> dict[str, object]:
    record: dict[str, object] = {"role": message.role, "ts": ts or int(time.time())}
    if message.content:
        record["content"] = message.content
    if message.tool_calls:
        record["tool_calls"] = [
            {
                "id": c.id,
                "name": c.name,
                "arguments": c.arguments,
                "raw_arguments": c.raw_arguments,
                "parse_error": c.parse_error,
            }
            for c in message.tool_calls
        ]
    if message.tool_results:
        record["tool_results"] = [
            {
                "tool_call_id": r.tool_call_id,
                "name": r.name,
                "success": r.success,
                "output": r.output,
                "error": r.error,
                "metadata": r.metadata,
                "truncated": r.truncated,
            }
            for r in message.tool_results
        ]
    if message.provider_items:
        record["provider_items"] = list(message.provider_items)
    return record


def record_to_message(record: dict[str, object]) -> ChatMessage:
    role = record["role"]
    if role not in {"user", "assistant", "tool"}:
        raise ValueError("未知消息角色")
    calls = tuple(
        ToolCall(**item) for item in record.get("tool_calls", []) if isinstance(item, dict)
    )
    results = tuple(
        ToolResult(**item) for item in record.get("tool_results", []) if isinstance(item, dict)
    )
    provider_items = tuple(
        item for item in record.get("provider_items", []) if isinstance(item, dict)
    )
    return ChatMessage(role, str(record.get("content") or ""), calls, results, provider_items)


def _truncate_title(text: str) -> str:
    compact = " ".join(text.split()) or "（无标题）"
    return compact if len(compact) <= 50 else f"{compact[:49]}…"


def _pause_notice(seconds: int) -> str:
    duration = f"{seconds // 86400} 天" if seconds >= 86400 else f"{seconds // 3600} 小时"
    return (
        f"[系统提示] 本会话已暂停 {duration}。部分上下文可能已过时，"
        "如需最新信息请重新读取相关文件。"
    )


__all__ = [
    "RestoredSession",
    "SessionContext",
    "SessionInfo",
    "Writer",
    "cleanup_expired_sessions",
    "message_to_record",
    "new_session_id",
    "record_to_message",
    "restore_session",
    "scan_sessions",
]
