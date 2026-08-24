"""文件模式匹配与代码内容搜索工具。"""

from __future__ import annotations

import asyncio
import glob as glob_module
import re
from pathlib import Path

from ..models import ToolCall, ToolDefinition, ToolResult
from .base import failure, optional_string, require_string
from .context import resolve_path

MAX_MATCHES = 200


class GlobTool:
    read_only = True
    definition = ToolDefinition(
        "glob",
        "按 glob 模式递归查找文件",
        {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "如 **/*.py"},
                "path": {"type": "string", "description": "可选搜索根目录"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    )

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    async def execute(self, call: ToolCall) -> ToolResult:
        try:
            pattern = require_string(call, "pattern")
            root = resolve_path(optional_string(call, "path", "."), self.workspace)
            matches = await asyncio.to_thread(_glob_files, root, pattern)
            selected = matches[:MAX_MATCHES]
            truncated = len(matches) > MAX_MATCHES
            output = "\n".join(str(path) for path in selected) or "未找到匹配文件"
            return ToolResult(
                call.id,
                call.name,
                True,
                output,
                metadata={"matches": len(matches)},
                truncated=truncated,
            )
        except (OSError, ValueError) as exc:
            return failure(call, f"查找文件失败：{exc}")


class GrepTool:
    read_only = True
    definition = ToolDefinition(
        "grep",
        "用正则表达式搜索文件内容，返回文件、行号和内容",
        {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "正则表达式"},
                "path": {"type": "string", "description": "可选文件或目录范围"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    )

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    async def execute(self, call: ToolCall) -> ToolResult:
        try:
            pattern = require_string(call, "pattern")
            root = resolve_path(optional_string(call, "path", "."), self.workspace)
            regex = re.compile(pattern)
            matches, total = await asyncio.to_thread(_grep_files, root, regex)
            return ToolResult(
                call.id,
                call.name,
                True,
                "\n".join(matches) or "未找到匹配内容",
                metadata={"matches": total},
                truncated=total > MAX_MATCHES,
            )
        except re.error as exc:
            return failure(call, f"搜索正则表达式无效：{exc}")
        except (OSError, ValueError) as exc:
            return failure(call, f"搜索内容失败：{exc}")


def _glob_files(root: Path, pattern: str) -> list[Path]:
    query = pattern if Path(pattern).is_absolute() else str(root / pattern)
    return sorted(
        Path(item).resolve()
        for item in glob_module.glob(query, recursive=True)
        if Path(item).is_file()
    )


def _grep_files(root: Path, regex: re.Pattern[str]) -> tuple[list[str], int]:
    files = [root] if root.is_file() else root.rglob("*")
    selected: list[str] = []
    total = 0
    for file in files:
        if not file.is_file() or ".git" in file.parts:
            continue
        try:
            for number, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
                if regex.search(line):
                    total += 1
                    if len(selected) < MAX_MATCHES:
                        selected.append(f"{file}:{number}:{line}")
        except (OSError, UnicodeError):
            continue
    return selected, total
