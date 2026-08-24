"""项目级与用户级长期记忆索引和异步更新。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from .models import ChatMessage, ProviderEventType, SystemPrompt
from .providers.base import ChatProvider

logger = logging.getLogger(__name__)
MEMORY_TYPES = {
    "user_preference",
    "correction_feedback",
    "project_knowledge",
    "reference_material",
}
INDEX_BYTE_LIMIT = 25_000
INDEX_LINE_LIMIT = 200
MEMORY_KEYWORDS = ("记住", "记忆", "别忘", "remember", "memo")
_SAFE_NAME = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*\.md$")
MEMORY_UPDATE_PROMPT = """你负责更新编程助手的长期记忆。严禁调用工具，只返回 JSON 数组。
根据本轮对话和现有索引判断去重，操作可为 create/update/delete。
create 字段：action、level(project/user)、type、title、slug、content。
update 字段：action、level、filename、title、content，可选 type。
delete 字段：action、level、filename。无须更新时返回 []。
项目知识和参考资料通常放 project；用户偏好和纠正反馈通常放 user。
slug 仅用小写字母、数字和下划线。不要输出 Markdown 代码围栏。"""


class MemoryManager:
    def __init__(self, workspace: Path, user_home: Path | None = None) -> None:
        self.workspace = workspace.resolve()
        home = (user_home or Path.home()).resolve()
        self.project_dir = self.workspace / ".tuicodingagent" / "memory"
        self.user_dir = home / ".tuicodingagent" / "memory"
        self._lock = asyncio.Lock()
        self.index_text = self.load_indexes()

    def load_indexes(self) -> str:
        parts = []
        for label, directory in (("项目记忆", self.project_dir), ("用户记忆", self.user_dir)):
            path = directory / "MEMORY.md"
            try:
                content = path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                continue
            if content:
                parts.append(f"[{label}]\n{content}")
        return _truncate_utf8("\n\n".join(parts), INDEX_BYTE_LIMIT)

    def note_filenames(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """只返回两级笔记文件名，不展开内容。"""

        return self._filenames(self.project_dir), self._filenames(self.user_dir)

    @staticmethod
    def _filenames(directory: Path) -> tuple[str, ...]:
        try:
            return tuple(
                sorted(path.name for path in directory.glob("*.md") if path.name != "MEMORY.md")
            )
        except OSError:
            return ()

    async def update(
        self,
        provider: ChatProvider,
        turn_messages: tuple[ChatMessage, ...],
    ) -> bool:
        """让 LLM 判断去重并执行一次更新；任何失败只记日志。"""

        async with self._lock:
            try:
                request = ChatMessage(
                    "user",
                    "现有索引：\n"
                    + (self.index_text or "（空）")
                    + "\n\n本轮对话：\n"
                    + _messages_json(turn_messages),
                )
                chunks: list[str] = []
                async for event in provider.stream(
                    SystemPrompt(MEMORY_UPDATE_PROMPT), (request,), ()
                ):
                    if event.type == ProviderEventType.TEXT_DELTA:
                        chunks.append(event.text)
                    elif event.type == ProviderEventType.TOOL_CALL:
                        raise ValueError("记忆更新模型请求了工具")
                operations = json.loads("".join(chunks).strip())
                if not isinstance(operations, list):
                    raise ValueError("记忆更新响应不是数组")
                for operation in operations:
                    if isinstance(operation, dict):
                        self._apply(operation)
                self._rebuild_index(self.project_dir)
                self._rebuild_index(self.user_dir)
                self.index_text = self.load_indexes()
                return True
            except Exception as exc:
                logger.warning("长期记忆更新失败：%s", type(exc).__name__)
                return False

    def _apply(self, operation: dict[str, object]) -> None:
        action = operation.get("action")
        level = operation.get("level")
        if level not in {"project", "user"} or action not in {"create", "update", "delete"}:
            return
        directory = self.project_dir if level == "project" else self.user_dir
        directory.mkdir(parents=True, exist_ok=True)
        if action == "create":
            note_type = str(operation.get("type") or "")
            slug = str(operation.get("slug") or "").lower().strip("_")
            filename = f"{note_type}_{slug}.md"
            if note_type not in MEMORY_TYPES or not _safe_filename(filename):
                return
            path = directory / filename
            if path.exists():
                return
            self._write_note(
                path,
                note_type,
                str(operation.get("title") or "").strip(),
                str(operation.get("content") or "").strip(),
                None,
            )
            return
        filename = str(operation.get("filename") or "")
        if not _safe_filename(filename):
            return
        path = directory / filename
        if action == "delete":
            with suppress(OSError):
                path.unlink(missing_ok=True)
            return
        if not path.is_file():
            return
        existing = _parse_note(path)
        note_type = str(operation.get("type") or existing.get("type") or "")
        if note_type not in MEMORY_TYPES:
            return
        self._write_note(
            path,
            note_type,
            str(operation.get("title") or existing.get("title") or "").strip(),
            str(operation.get("content") or existing.get("content") or "").strip(),
            str(existing.get("created") or "") or None,
        )

    def _write_note(
        self,
        path: Path,
        note_type: str,
        title: str,
        content: str,
        created: str | None,
    ) -> None:
        if not title or not content:
            return
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        safe_title = title.replace("\r", " ").replace("\n", " ").replace(":", "：")
        text = (
            "---\n"
            f"type: {note_type}\n"
            f"title: {safe_title}\n"
            f"created: {created or now}\n"
            f"updated: {now}\n"
            "---\n"
            f"{content}\n"
        )
        path.write_text(text, encoding="utf-8")

    def _rebuild_index(self, directory: Path) -> None:
        try:
            notes = sorted(path for path in directory.glob("*.md") if path.name != "MEMORY.md")
        except OSError:
            return
        rows: list[str] = []
        byte_count = 0
        for path in notes:
            parsed = _parse_note(path)
            note_type = parsed.get("type", "")
            title = parsed.get("title", "")
            content = " ".join(parsed.get("content", "").split())
            if note_type not in MEMORY_TYPES or not title or not content:
                continue
            description = content if len(content) <= 120 else f"{content[:119]}…"
            row = f"- [{note_type}] {title} — {description}"
            row_bytes = len((row + "\n").encode("utf-8"))
            if len(rows) >= INDEX_LINE_LIMIT or byte_count + row_bytes > INDEX_BYTE_LIMIT:
                break
            rows.append(row)
            byte_count += row_bytes
        if not rows and not directory.exists():
            return
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "MEMORY.md").write_text(
            "\n".join(rows) + ("\n" if rows else ""), encoding="utf-8"
        )


def should_update_memory(turn_count: int, user_text: str) -> bool:
    lowered = user_text.lower()
    return turn_count % 5 == 0 or any(keyword in lowered for keyword in MEMORY_KEYWORDS)


def _safe_filename(filename: str) -> bool:
    return filename != "MEMORY.md" and _SAFE_NAME.fullmatch(filename) is not None


def _parse_note(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {}
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        return {}
    front, content = text[4:].split("\n---\n", 1)
    parsed: dict[str, str] = {"content": content.strip()}
    for line in front.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            parsed[key.strip()] = value.strip()
    return parsed


def _truncate_utf8(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    suffix = "\n(index truncated)"
    room = limit - len(suffix.encode("utf-8"))
    return raw[:room].decode("utf-8", errors="ignore") + suffix


def _messages_json(messages: tuple[ChatMessage, ...]) -> str:
    return json.dumps(
        [{"role": message.role, "content": message.content} for message in messages],
        ensure_ascii=False,
    )


__all__ = ["INDEX_BYTE_LIMIT", "MemoryManager", "should_update_memory"]
