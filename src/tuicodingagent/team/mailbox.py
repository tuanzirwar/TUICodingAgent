"""SQLite 持久邮箱与进程内优先级调度器。"""

from __future__ import annotations

import asyncio
import heapq
import json
import os
import random
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

MESSAGE_TYPES = frozenset(
    {
        "text",
        "shutdown_request",
        "shutdown_response",
        "plan_approval_request",
        "plan_approval_response",
    }
)
TERMINAL_STATUSES = frozenset({"acked", "cancelled", "dead"})


@dataclass(slots=True)
class Message:
    sender: str
    text: str
    type: str = "text"
    request_id: str = ""
    approve: bool | None = None
    read: bool = False
    timestamp: str = ""
    message_id: str = field(default_factory=lambda: uuid4().hex)
    priority: int = 0
    dedup_key: str = ""
    retry_count: int = 0
    max_retry: int = 3
    plan_version: int = 0
    status: str = "pending"
    available_at: float = 0.0
    ack_deadline: float = 0.0

    def __post_init__(self) -> None:
        self.timestamp = self.timestamp or datetime.now(UTC).isoformat()
        if self.read:
            self.status = "acked"

    @property
    def content(self) -> str:
        return self.text

    def to_json(self) -> dict[str, object]:
        item: dict[str, object] = {
            "from": self.sender,
            "text": self.text,
            "timestamp": self.timestamp,
            "read": self.status == "acked" or self.read,
        }
        if self.type != "text" or self.request_id or self.approve is not None:
            item.update(type=self.type, requestId=self.request_id, approve=self.approve)
        advanced = (
            self.priority
            or self.dedup_key
            or self.retry_count
            or self.plan_version
            or self.status not in {"pending", "acked"}
        )
        if advanced:
            item.update(
                messageId=self.message_id,
                priority=self.priority,
                dedupKey=self.dedup_key,
                retryCount=self.retry_count,
                maxRetry=self.max_retry,
                planVersion=self.plan_version,
                status=self.status,
            )
        return item

    @classmethod
    def from_json(cls, item: dict[str, object]) -> Message:
        read = bool(item.get("read", False))
        return cls(
            sender=str(item.get("from", item.get("sender", ""))),
            text=str(item.get("text", item.get("content", ""))),
            type=str(item.get("type", "text")),
            request_id=str(item.get("requestId", item.get("request_id", ""))),
            approve=item.get("approve") if isinstance(item.get("approve"), bool) else None,
            read=read,
            timestamp=str(item.get("timestamp", "")),
            message_id=str(item.get("messageId", uuid4().hex)),
            priority=int(item.get("priority", 0)),
            dedup_key=str(item.get("dedupKey", "")),
            retry_count=int(item.get("retryCount", 0)),
            max_retry=int(item.get("maxRetry", 3)),
            plan_version=int(item.get("planVersion", 0)),
            status=str(item.get("status", "acked" if read else "pending")),
        )


class Mailbox:
    """SQLite 是状态源；JSON 文件只作为旧版本兼容镜像。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.database_path = root / "mailbox.sqlite3"
        self._heap: dict[str, list[tuple[int, float, str]]] = {}
        self._heap_lock = threading.Lock()
        self._mirror_lock = threading.Lock()
        self._initialize()
        self._migrate_json()
        self._recover_expired()
        self._reload_scheduler()

    def path(self, agent_id: str) -> Path:
        return self.root / f"{agent_id}.json"

    async def write(self, agent_id: str, message: Message) -> str:
        return await asyncio.to_thread(self._write_sync, agent_id, message)

    async def read(self, agent_id: str) -> list[Message]:
        return await asyncio.to_thread(self.unread, agent_id)

    async def mark_read(self, agent_id: str, indices: list[int]) -> None:
        await asyncio.to_thread(self._mark_read_sync, agent_id, set(indices))

    async def claim(
        self, agent_id: str, *, limit: int = 1, ack_timeout: float = 30.0
    ) -> list[Message]:
        return await asyncio.to_thread(self._claim_sync, agent_id, limit, ack_timeout)

    async def ack(self, agent_id: str, message_ids: list[str]) -> None:
        await asyncio.to_thread(self._set_status, agent_id, message_ids, "acked")

    async def retry(self, agent_id: str, message_id: str, *, delay: float = 0.0) -> bool:
        return await asyncio.to_thread(self._retry_sync, agent_id, message_id, delay)

    async def cancel(self, agent_id: str, message_id: str) -> bool:
        return await asyncio.to_thread(self._cancel_sync, agent_id, message_id)

    def unread(self, agent_id: str, *, mark_read: bool = False) -> list[Message]:
        self._recover_expired()
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM messages WHERE agent_id=? AND status='pending'
                   AND available_at<=? ORDER BY priority DESC, created_at, id""",
                (agent_id, time.time()),
            ).fetchall()
        selected = [self._from_row(row) for row in rows]
        if mark_read and selected:
            self._set_status(agent_id, [item.message_id for item in selected], "acked")
        return selected

    def _write_sync(self, agent_id: str, message: Message) -> str:
        available_at = message.available_at or time.time()
        created_at = self._timestamp(message.timestamp)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """INSERT INTO messages(
                       id,agent_id,sender,text,type,request_id,approve,priority,dedup_key,
                       status,retry_count,max_retry,plan_version,available_at,ack_deadline,
                       created_at,updated_at,timestamp)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        message.message_id,
                        agent_id,
                        message.sender,
                        message.text,
                        message.type,
                        message.request_id,
                        None if message.approve is None else int(message.approve),
                        message.priority,
                        message.dedup_key,
                        message.status,
                        message.retry_count,
                        message.max_retry,
                        message.plan_version,
                        available_at,
                        message.ack_deadline,
                        created_at,
                        time.time(),
                        message.timestamp,
                    ),
                )
        except sqlite3.IntegrityError:
            if not message.dedup_key:
                raise
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT id FROM messages WHERE agent_id=? AND dedup_key=?",
                    (agent_id, message.dedup_key),
                ).fetchone()
            return str(row["id"])
        self._push(agent_id, message.priority, available_at, message.message_id)
        self._sync_mirror(agent_id)
        return message.message_id

    def _claim_sync(self, agent_id: str, limit: int, ack_timeout: float) -> list[Message]:
        if limit < 1 or ack_timeout <= 0:
            raise ValueError("limit 和 ack_timeout 必须为正数")
        self._recover_expired()
        claimed: list[Message] = []
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT * FROM messages WHERE agent_id=? AND status='pending'
                   AND available_at<=? ORDER BY priority DESC, created_at, id LIMIT ?""",
                (agent_id, now, limit),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE messages SET status='inflight',ack_deadline=?,updated_at=? WHERE id=?",
                    (now + ack_timeout, now, row["id"]),
                )
                item = self._from_row(row)
                item.status = "inflight"
                item.ack_deadline = now + ack_timeout
                claimed.append(item)
        self._sync_mirror(agent_id)
        return claimed

    def _mark_read_sync(self, agent_id: str, indices: set[int]) -> None:
        pending = self.unread(agent_id)
        ids = [pending[index].message_id for index in sorted(indices) if 0 <= index < len(pending)]
        self._set_status(agent_id, ids, "acked")

    def _set_status(self, agent_id: str, message_ids: list[str], status: str) -> None:
        if not message_ids:
            return
        placeholders = ",".join("?" for _ in message_ids)
        with self._connect() as connection:
            connection.execute(
                "UPDATE messages SET status=?,updated_at=? "
                f"WHERE agent_id=? AND id IN ({placeholders})",
                (status, time.time(), agent_id, *message_ids),
            )
        self._sync_mirror(agent_id)

    def _retry_sync(self, agent_id: str, message_id: str, delay: float) -> bool:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT retry_count,max_retry,priority FROM messages WHERE agent_id=? AND id=?",
                (agent_id, message_id),
            ).fetchone()
            if row is None:
                return False
            retries = int(row["retry_count"]) + 1
            status = "dead" if retries > int(row["max_retry"]) else "pending"
            connection.execute(
                """UPDATE messages SET status=?,retry_count=?,available_at=?,ack_deadline=0,
                   updated_at=? WHERE id=?""",
                (status, retries, now + max(0.0, delay), now, message_id),
            )
        if status == "pending":
            self._push(agent_id, int(row["priority"]), now + max(0.0, delay), message_id)
        self._sync_mirror(agent_id)
        return True

    def _cancel_sync(self, agent_id: str, message_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE messages SET status='cancelled',updated_at=?
                   WHERE agent_id=? AND id=? AND status NOT IN ('acked','cancelled','dead')""",
                (time.time(), agent_id, message_id),
            )
        self._sync_mirror(agent_id)
        return cursor.rowcount > 0

    def _recover_expired(self) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT id,agent_id,retry_count,max_retry,priority FROM messages "
                "WHERE status='inflight' AND ack_deadline>0 AND ack_deadline<=?",
                (now,),
            ).fetchall()
            for row in rows:
                retries = int(row["retry_count"]) + 1
                status = "dead" if retries > int(row["max_retry"]) else "pending"
                connection.execute(
                    "UPDATE messages SET status=?,retry_count=?,ack_deadline=0,"
                    "updated_at=? WHERE id=?",
                    (status, retries, now, row["id"]),
                )
                if status == "pending":
                    self._push(str(row["agent_id"]), int(row["priority"]), now, str(row["id"]))

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """CREATE TABLE IF NOT EXISTS messages(
                   id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, sender TEXT NOT NULL,
                   text TEXT NOT NULL, type TEXT NOT NULL, request_id TEXT NOT NULL,
                   approve INTEGER, priority INTEGER NOT NULL, dedup_key TEXT NOT NULL,
                   status TEXT NOT NULL, retry_count INTEGER NOT NULL, max_retry INTEGER NOT NULL,
                   plan_version INTEGER NOT NULL, available_at REAL NOT NULL,
                   ack_deadline REAL NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                   timestamp TEXT NOT NULL);
                   CREATE INDEX IF NOT EXISTS mailbox_pending
                   ON messages(agent_id,status,priority DESC,created_at);
                   CREATE UNIQUE INDEX IF NOT EXISTS mailbox_dedup
                   ON messages(agent_id,dedup_key) WHERE dedup_key<>'';"""
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _migrate_json(self) -> None:
        with self._connect() as connection:
            count = int(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
        if count:
            return
        for path in self.root.glob("*.json"):
            for item in self._load_raw(path):
                self._write_sync(path.stem, Message.from_json(item))

    def _reload_scheduler(self) -> None:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT agent_id,priority,available_at,id FROM messages WHERE status='pending'"
            ).fetchall()
        for row in rows:
            self._push(
                str(row["agent_id"]),
                int(row["priority"]),
                float(row["available_at"]),
                str(row["id"]),
            )

    def _push(self, agent_id: str, priority: int, available_at: float, message_id: str) -> None:
        with self._heap_lock:
            heapq.heappush(
                self._heap.setdefault(agent_id, []), (-priority, available_at, message_id)
            )

    def _sync_mirror(self, agent_id: str) -> None:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM messages WHERE agent_id=? ORDER BY created_at,id", (agent_id,)
            ).fetchall()
        items = [self._from_row(row).to_json() for row in rows]
        with self._mirror_lock:
            self._atomic_write(self.path(agent_id), items)

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Message:
        return Message(
            sender=str(row["sender"]), text=str(row["text"]), type=str(row["type"]),
            request_id=str(row["request_id"]),
            approve=None if row["approve"] is None else bool(row["approve"]),
            read=str(row["status"]) == "acked", timestamp=str(row["timestamp"]),
            message_id=str(row["id"]), priority=int(row["priority"]),
            dedup_key=str(row["dedup_key"]), retry_count=int(row["retry_count"]),
            max_retry=int(row["max_retry"]), plan_version=int(row["plan_version"]),
            status=str(row["status"]), available_at=float(row["available_at"]),
            ack_deadline=float(row["ack_deadline"]),
        )

    @staticmethod
    def _timestamp(value: str) -> float:
        try:
            return datetime.fromisoformat(value).timestamp()
        except (TypeError, ValueError):
            return time.time()

    @staticmethod
    def _load_raw(path: Path) -> list[dict[str, object]]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        return raw if isinstance(raw, list) else []

    @staticmethod
    def _atomic_write(path: Path, items: list[dict[str, object]]) -> None:
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)

    @staticmethod
    def _acquire(lock: Path) -> None:
        for _ in range(10):
            try:
                descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(descriptor)
                return
            except (FileExistsError, PermissionError):
                try:
                    if time.time() - lock.stat().st_mtime > 10:
                        lock.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                time.sleep(random.uniform(0.005, 0.1))
        raise TimeoutError(f"邮箱锁竞争超时：{lock}")


Box = Mailbox

__all__ = ["MESSAGE_TYPES", "Box", "Mailbox", "Message"]
