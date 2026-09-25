"""项目级与用户级长期记忆索引和异步更新。"""

from __future__ import annotations

import asyncio
import hashlib
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
_EXPLICIT_MEMORY_PATTERNS = (
    re.compile(r"(?:请你?|帮我|请帮我)?\s*记住(?:一下)?[\s：:，,]*(.+)", re.IGNORECASE),
    re.compile(r"(?:请你?)?\s*别忘(?:了)?[\s：:，,]*(.+)", re.IGNORECASE),
    re.compile(r"(?:please\s+)?remember(?:\s+that)?[\s:，,-]*(.+)", re.IGNORECASE),
    re.compile(r"\bmemo[\s:，,-]+(.+)", re.IGNORECASE),
)
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
        explicit_user_text: str | None = None,
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
                operations = _parse_operations("".join(chunks))
                applied = 0
                for operation in operations:
                    if isinstance(operation, dict) and self._apply(operation):
                        applied += 1
                if applied == 0 and explicit_user_text:
                    applied += int(self._apply_explicit_fallback(explicit_user_text))
                self._rebuild_index(self.project_dir)
                self._rebuild_index(self.user_dir)
                self.index_text = self.load_indexes()
                if operations and applied == 0:
                    logger.warning("长期记忆更新未应用任何有效操作")
                return applied > 0 or not operations
            except Exception as exc:
                try:
                    fallback_saved = bool(
                        explicit_user_text
                        and self._apply_explicit_fallback(explicit_user_text)
                    )
                except Exception:
                    fallback_saved = False
                if fallback_saved:
                    self._rebuild_index(self.project_dir)
                    self._rebuild_index(self.user_dir)
                    self.index_text = self.load_indexes()
                    logger.warning("长期记忆模型更新失败，已保存明确记忆：%s", type(exc).__name__)
                    return True
                logger.warning("长期记忆更新失败：%s", type(exc).__name__)
                return False

    def _apply(self, operation: dict[str, object]) -> bool:
        action = operation.get("action")
        level = operation.get("level")
        if level not in {"project", "user"} or action not in {"create", "update", "delete"}:
            return False
        directory = self.project_dir if level == "project" else self.user_dir
        directory.mkdir(parents=True, exist_ok=True)
        if action == "create":
            note_type = str(operation.get("type") or "")
            slug = str(operation.get("slug") or "").lower().strip("_")
            filename = f"{note_type}_{slug}.md"
            if note_type not in MEMORY_TYPES or not _safe_filename(filename):
                return False
            path = directory / filename
            if path.exists():
                return True
            return self._write_note(
                path,
                note_type,
                str(operation.get("title") or "").strip(),
                str(operation.get("content") or "").strip(),
                None,
            )
        filename = str(operation.get("filename") or "")
        if not _safe_filename(filename):
            return False
        path = directory / filename
        if action == "delete":
            existed = path.is_file()
            with suppress(OSError):
                path.unlink(missing_ok=True)
            return existed and not path.exists()
        if not path.is_file():
            return False
        existing = _parse_note(path)
        note_type = str(operation.get("type") or existing.get("type") or "")
        if note_type not in MEMORY_TYPES:
            return False
        return self._write_note(
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
    ) -> bool:
        if not title or not content:
            return False
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
        return True

    def _apply_explicit_fallback(self, user_text: str) -> bool:
        content = explicit_memory_content(user_text)
        if not content:
            return False
        project_terms = ("项目", "仓库", "代码库", "代码仓", "project", "repository", "repo")
        level = "project" if any(term in content.casefold() for term in project_terms) else "user"
        note_type = "project_knowledge" if level == "project" else "user_preference"
        digest = hashlib.sha256(content.casefold().encode("utf-8")).hexdigest()[:12]
        title = content if len(content) <= 40 else f"{content[:39]}…"
        return self._apply(
            {
                "action": "create",
                "level": level,
                "type": note_type,
                "title": title,
                "slug": f"explicit_{digest}",
                "content": content,
            }
        )

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


def explicit_memory_content(user_text: str) -> str:
    """提取用户明确要求长期保存的内容，普通的“记忆”讨论不会命中。"""

    for pattern in _EXPLICIT_MEMORY_PATTERNS:
        match = pattern.search(user_text)
        if match:
            return match.group(1).strip().rstrip("。.!！")
    return ""


def _safe_filename(filename: str) -> bool:
    return filename != "MEMORY.md" and _SAFE_NAME.fullmatch(filename) is not None


def _parse_operations(text: str) -> list[object]:
    value = text.strip()
    if value.startswith("```") and value.endswith("```"):
        lines = value.splitlines()
        value = "\n".join(lines[1:-1]).strip()
    try:
        operations = json.loads(value)
    except json.JSONDecodeError:
        start = value.find("[")
        if start < 0:
            raise
        operations, _ = json.JSONDecoder().raw_decode(value[start:])
    if not isinstance(operations, list):
        raise ValueError("记忆更新响应不是数组")
    return operations


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


__all__ = [
    "INDEX_BYTE_LIMIT",
    "MemoryManager",
    "explicit_memory_content",
    "should_update_memory",
]
