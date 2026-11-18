"""读取、写入与唯一匹配编辑工具。"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from ..models import ToolCall, ToolDefinition, ToolResult
from .base import failure, require_string, truncate_text
from .context import resolve_path

MAX_READ_LINES = 2_000


def _schema(properties: dict[str, dict[str, str]], required: list[str]) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


class ReadFileTool:
    read_only = True
    definition = ToolDefinition(
        "read_file",
        "读取文本文件并返回带行号的内容；读取文件时优先使用本工具，不要用 shell 拼凑",
        _schema({"path": {"type": "string", "description": "文件路径"}}, ["path"]),
    )

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    async def execute(self, call: ToolCall) -> ToolResult:
        try:
            path = resolve_path(require_string(call, "path"), self.workspace)
            text = await asyncio.to_thread(path.read_text, encoding="utf-8")
        except (OSError, UnicodeError, ValueError) as exc:
            return failure(call, f"读取文件失败：{exc}")
        lines = text.splitlines()
        selected = lines[:MAX_READ_LINES]
        numbered = "\n".join(f"{index:>6}\t{line}" for index, line in enumerate(selected, 1))
        numbered, char_truncated = truncate_text(numbered)
        truncated = len(lines) > MAX_READ_LINES or char_truncated
        if len(lines) > MAX_READ_LINES:
            numbered += f"\n… 已截断 {len(lines) - MAX_READ_LINES} 行"
        return ToolResult(
            call.id,
            call.name,
            True,
            numbered,
            metadata={
                "path": str(path),
                "lines": len(lines),
                "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            },
            truncated=truncated,
        )


class WriteFileTool:
    read_only = False
    definition = ToolDefinition(
        "write_file",
        "创建或覆盖 UTF-8 文本文件，自动创建父目录；写入前先读取已有文件，优先使用本工具而非 shell",
        _schema(
            {
                "path": {"type": "string", "description": "文件路径"},
                "content": {"type": "string", "description": "完整文件内容"},
            },
            ["path", "content"],
        ),
    )

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    async def execute(self, call: ToolCall) -> ToolResult:
        try:
            path = resolve_path(require_string(call, "path"), self.workspace)
            content = call.arguments.get("content")
            if not isinstance(content, str):
                raise ValueError("参数 content 必须是字符串")
            await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(path.write_text, content, encoding="utf-8")
            return ToolResult(
                call.id,
                call.name,
                True,
                f"已写入 {path}（{len(content)} 个字符）",
                metadata={"path": str(path), "characters": len(content)},
            )
        except (OSError, UnicodeError, ValueError) as exc:
            return failure(call, f"写入文件失败：{exc}")


class EditFileTool:
    read_only = False
    definition = ToolDefinition(
        "edit_file",
        "用新文本替换文件中唯一出现的原文本；"
        "编辑前必须先用 read_file 读取当前内容，不要用 shell 替代",
        _schema(
            {
                "path": {"type": "string", "description": "文件路径"},
                "old_text": {"type": "string", "description": "必须唯一匹配的原文本"},
                "new_text": {"type": "string", "description": "替换后的新文本"},
            },
            ["path", "old_text", "new_text"],
        ),
    )

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    async def execute(self, call: ToolCall) -> ToolResult:
        try:
            path = resolve_path(require_string(call, "path"), self.workspace)
            old_text = require_string(call, "old_text")
            new_text = call.arguments.get("new_text")
            if not isinstance(new_text, str):
                raise ValueError("参数 new_text 必须是字符串")
            content = await asyncio.to_thread(path.read_text, encoding="utf-8")
            matches = content.count(old_text)
            if matches != 1:
                return failure(call, f"原文本应唯一匹配，实际匹配 {matches} 次", matches=matches)
            updated = content.replace(old_text, new_text, 1)
            await asyncio.to_thread(path.write_text, updated, encoding="utf-8")
            return ToolResult(
                call.id,
                call.name,
                True,
                f"已更新 {path}",
                metadata={"path": str(path), "matches": 1},
            )
        except (OSError, UnicodeError, ValueError) as exc:
            return failure(call, f"编辑文件失败：{exc}")
