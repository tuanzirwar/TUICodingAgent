"""追加式 JSONL 会话存档、扫描、恢复和过期清理。"""

from __future__ import annotations

import hashlib
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
from .redaction import redact

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
        self.persistence_healthy = False
        try:
            context.session_dir.mkdir(parents=True, exist_ok=True)
            self._first_message = (
                not context.conversation_path.exists()
                or context.conversation_path.stat().st_size == 0
            )
            self._handle = context.conversation_path.open("a", encoding="utf-8", newline="\n")
            self.persistence_healthy = True
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
            checkpoint_id = secrets.token_hex(8)
            if not self._write(
                {
                    "type": "compact",
                    "ts": int(time.time()),
                    "checkpoint_id": checkpoint_id,
                    "expected_messages": len(messages),
                }
            ):
                return
            for message in messages:
                if not self._write(message_to_record(message)):
                    return
            self._write({"type": "compact_commit", "checkpoint_id": checkpoint_id})

    def _write(self, record: dict[str, object]) -> bool:
        if self._handle is None:
            return False
        safe = redact(record)
        # 恢复时不能将掩码参数当作原始操作执行。
        for original, call in zip(
            record.get("tool_calls", []), safe.get("tool_calls", []), strict=True
        ):
            if original != call:
                call["parse_error"] = "存档调用已脱敏，需要重新读取实际状态，禁止直接重放掩码参数"
        try:
            self._handle.write(json.dumps(safe, ensure_ascii=False, separators=(",", ":")) + "\n")
            self._handle.flush()
            os.fsync(self._handle.fileno())
        except OSError as exc:
            self.persistence_healthy = False
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
    tool_outcomes: tuple[tuple[ToolCall, ToolResult | None], ...] = ()


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
                    if not isinstance(record, dict) or record.get("type") in {
                        "compact",
                        "compact_commit",
                    }:
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
    pending: list[dict[str, object]] | None = None
    checkpoint: dict[str, object] = {}
    try:
        lines = info.context.conversation_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        lines = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("type") == "compact":
            # 未提交的新快照不覆盖上一份完整状态。
            pending = []
            checkpoint = record
            continue
        if record.get("type") == "compact_commit":
            if (
                pending is not None
                and record.get("checkpoint_id") == checkpoint.get("checkpoint_id")
                and len(pending) == checkpoint.get("expected_messages")
            ):
                records = pending
                pending = None
            continue
        if pending is not None:
            pending.append(record)
        else:
            records.append(record)
    # 兼容旧版 compact；孤立 marker 不清空可恢复状态。
    if pending and "checkpoint_id" not in checkpoint:
        records = pending
    messages: list[ChatMessage] = []
    uncertain_effects = False
    last_ts = 0
    for record in records:
        try:
            messages.append(record_to_message(record))
            last_ts = max(last_ts, int(record.get("ts") or 0))
        except (KeyError, TypeError, ValueError):
            continue
    # 在修复协议历史之前保留操作记录，部分结果和孤立调用仍需要重放保护。
    outcomes: dict[str, tuple[ToolCall, ToolResult | None]] = {}
    for message in messages:
        for call in message.tool_calls:
            outcomes[call.id] = (call, None)
        for result in message.tool_results:
            previous = outcomes.get(result.tool_call_id)
            if previous is not None and previous[0].name == result.name:
                outcomes[result.tool_call_id] = (previous[0], result)
    for index, message in enumerate(messages):
        if message.role != "assistant" or not message.tool_calls:
            continue
        if index + 1 >= len(messages) or messages[index + 1].role != "tool":
            uncertain_effects = any(
                call.name in {"write_file", "edit_file", "bash"} for call in message.tool_calls
            )
            messages = messages[:index]
            break
        expected = {call.id for call in message.tool_calls}
        actual = {result.tool_call_id for result in messages[index + 1].tool_results}
        if not expected.issubset(actual):
            uncertain_effects = any(
                call.id not in actual and call.name in {"write_file", "edit_file", "bash"}
                for call in message.tool_calls
            )
            messages = messages[:index]
            break
    current = now or int(time.time())
    if last_ts and current - last_ts > 6 * 3600:
        messages.append(ChatMessage("user", _pause_notice(current - last_ts)))
    if uncertain_effects:
        messages.append(
            ChatMessage(
                "user",
                "[恢复边界] 未完成工具调用可能已经产生副作用。"
                "请先检查文件与外部状态，禁止盲目重放写入或命令。",
            )
        )
    drift = _workspace_drift(messages, info.context.workspace)
    if drift:
        messages.append(
            ChatMessage(
                "user",
                "[工作区变化] 已读取文件的状态已失效：\n"
                + "\n".join(drift)
                + "\n请重新读取相关文件，禁止依据旧文件结果继续修改。",
            )
        )
    return RestoredSession(info, tuple(messages), last_ts, tuple(outcomes.values()))


def _workspace_drift(messages: list[ChatMessage], workspace: Path) -> list[str]:
    latest: dict[str, str] = {}
    for message in messages:
        for result in message.tool_results:
            path = result.metadata.get("path")
            digest = result.metadata.get("content_sha256")
            if result.success and result.name == "read_file" and isinstance(path, str):
                latest[path] = digest if isinstance(digest, str) else ""
    changed = []
    for raw_path, digest in latest.items():
        path = Path(raw_path)
        path = (path if path.is_absolute() else workspace / path).resolve(strict=False)
        if not path.is_relative_to(workspace.resolve()):
            changed.append("文件已越出工作区")
            continue
        if not digest:
            changed.append(f"{path}：旧版本记录无指纹，内容未经重新核验")
            continue
        try:
            current = hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
        except (OSError, UnicodeError):
            changed.append(f"{path}：已删除或不可读")
            continue
        if current != digest:
            changed.append(f"{path}：内容已改变")
    return changed


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
