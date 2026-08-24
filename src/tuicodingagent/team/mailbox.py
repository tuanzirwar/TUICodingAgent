"""跨进程安全的 JSON 邮箱。"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

MESSAGE_TYPES = frozenset(
    {
        "text",
        "shutdown_request",
        "shutdown_response",
        "plan_approval_request",
        "plan_approval_response",
    }
)


@dataclass(slots=True)
class Message:
    sender: str
    text: str
    type: str = "text"
    request_id: str = ""
    approve: bool | None = None
    read: bool = False
    timestamp: str = ""

    def __post_init__(self) -> None:
        self.timestamp = self.timestamp or datetime.now(UTC).isoformat()

    @property
    def content(self) -> str:
        return self.text

    def to_json(self) -> dict[str, object]:
        item: dict[str, object] = {
            "from": self.sender,
            "text": self.text,
            "timestamp": self.timestamp,
            "read": self.read,
        }
        if self.type != "text" or self.request_id or self.approve is not None:
            item["type"] = self.type
            item["requestId"] = self.request_id
            item["approve"] = self.approve
        return item

    @classmethod
    def from_json(cls, item: dict[str, object]) -> Message:
        # 同时读取早期内部字段，保证升级后的旧邮箱可恢复。
        return cls(
            sender=str(item.get("from", item.get("sender", ""))),
            text=str(item.get("text", item.get("content", ""))),
            type=str(item.get("type", "text")),
            request_id=str(item.get("requestId", item.get("request_id", ""))),
            approve=item.get("approve") if isinstance(item.get("approve"), bool) else None,
            read=bool(item.get("read", False)),
            timestamp=str(item.get("timestamp", "")),
        )


class Mailbox:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, agent_id: str) -> Path:
        return self.root / f"{agent_id}.json"

    async def write(self, agent_id: str, message: Message) -> None:
        await asyncio.to_thread(self._write_sync, agent_id, message)

    async def read(self, agent_id: str) -> list[Message]:
        return await asyncio.to_thread(self.unread, agent_id)

    async def mark_read(self, agent_id: str, indices: list[int]) -> None:
        await asyncio.to_thread(self._mark_read_sync, agent_id, set(indices))

    def _write_sync(self, agent_id: str, message: Message) -> None:
        path = self.path(agent_id)
        lock = path.with_suffix(".lock")
        self._acquire(lock)
        try:
            items = self._load_raw(path)
            items.append(message.to_json())
            self._atomic_write(path, items)
        finally:
            lock.unlink(missing_ok=True)

    def unread(self, agent_id: str, *, mark_read: bool = False) -> list[Message]:
        path = self.path(agent_id)
        raw = self._load_raw(path)
        selected = [Message.from_json(item) for item in raw if not item.get("read", False)]
        if mark_read and selected:
            self._mark_read_sync(agent_id, set(range(len(selected))))
        return selected

    def _mark_read_sync(self, agent_id: str, indices: set[int]) -> None:
        path = self.path(agent_id)
        lock = path.with_suffix(".lock")
        self._acquire(lock)
        try:
            raw = self._load_raw(path)
            unread_positions = [
                index for index, item in enumerate(raw) if not item.get("read", False)
            ]
            for index in indices:
                if 0 <= index < len(unread_positions):
                    raw[unread_positions[index]]["read"] = True
            self._atomic_write(path, raw)
        finally:
            lock.unlink(missing_ok=True)

    @staticmethod
    def _load_raw(path: Path) -> list[dict[str, object]]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
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
