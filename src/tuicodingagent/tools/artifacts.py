"""读取和搜索被上下文控制器落盘的工具输出。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..models import ToolCall, ToolDefinition, ToolResult
from .base import ToolArgumentError, failure, require_string, truncate_text


def _schema(properties: dict[str, dict[str, object]], required: list[str]) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


class ArtifactStore:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.roots = (
            self.workspace / ".tuicodingagent" / "sessions",
            self.workspace / ".tuicodingagent" / "artifacts",
        )

    def files(self) -> tuple[Path, ...]:
        found: list[Path] = []
        for root in self.roots:
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if path.is_file() and ("tool-results" in path.parts or root.name == "artifacts"):
                    found.append(path.resolve())
        return tuple(sorted(found, key=lambda path: path.stat().st_mtime_ns, reverse=True))

    def resolve(self, value: str) -> Path:
        requested = Path(value)
        if requested.is_absolute():
            candidate = requested.resolve(strict=False)
            if candidate in self.files():
                return candidate
            raise ValueError("artifact 路径不在受管目录中")
        matches = [path for path in self.files() if path.name == value or path.stem == value]
        if not matches:
            raise FileNotFoundError(value)
        if len(matches) > 1:
            raise ValueError("artifact id 不唯一，请使用落盘预览中的完整路径")
        return matches[0]


class ReadArtifactTool:
    read_only = True
    definition = ToolDefinition(
        "read_artifact",
        "按 artifact id 或落盘预览中的完整路径读取工具输出，可指定行偏移和行数",
        _schema(
            {
                "artifact": {"type": "string", "description": "artifact id 或完整路径"},
                "offset": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000},
            },
            ["artifact"],
        ),
    )

    def __init__(self, workspace: Path) -> None:
        self.store = ArtifactStore(workspace)

    async def execute(self, call: ToolCall) -> ToolResult:
        try:
            path = self.store.resolve(require_string(call, "artifact"))
            offset = _integer(call, "offset", 1, minimum=1)
            limit = _integer(call, "limit", 200, minimum=1, maximum=2000)
            text = await asyncio.to_thread(path.read_text, encoding="utf-8")
            lines = text.splitlines()
            selected = lines[offset - 1 : offset - 1 + limit]
            output = "\n".join(
                f"{number:>6}\t{line}" for number, line in enumerate(selected, start=offset)
            )
            output, truncated = truncate_text(output)
            return ToolResult(
                call.id,
                call.name,
                True,
                output,
                metadata={"path": str(path), "lines": len(lines), "offset": offset},
                truncated=truncated or offset - 1 + limit < len(lines),
            )
        except (OSError, UnicodeError, ValueError) as exc:
            return failure(call, f"读取 artifact 失败：{exc}")


class SearchArtifactTool:
    read_only = True
    definition = ToolDefinition(
        "search_artifact",
        "在所有受管工具输出 artifact 中进行不区分大小写的文字搜索",
        _schema(
            {
                "query": {"type": "string", "description": "要查找的文字"},
                "max_matches": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            ["query"],
        ),
    )

    def __init__(self, workspace: Path) -> None:
        self.store = ArtifactStore(workspace)

    async def execute(self, call: ToolCall) -> ToolResult:
        try:
            query = require_string(call, "query")
            maximum = _integer(call, "max_matches", 100, minimum=1, maximum=500)
            matches = await asyncio.to_thread(self._search, query, maximum)
            return ToolResult(
                call.id,
                call.name,
                True,
                "\n".join(matches) if matches else "未找到匹配内容",
                metadata={"matches": len(matches), "artifacts": len(self.store.files())},
                truncated=len(matches) >= maximum,
            )
        except (OSError, UnicodeError, ValueError) as exc:
            return failure(call, f"搜索 artifact 失败：{exc}")

    def _search(self, query: str, maximum: int) -> list[str]:
        needle = query.casefold()
        matches: list[str] = []
        for path in self.store.files():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if needle in line.casefold():
                    matches.append(f"{path}:{number}:{line}")
                    if len(matches) >= maximum:
                        return matches
        return matches


def _integer(
    call: ToolCall,
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    value = call.arguments.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ToolArgumentError(f"参数 {name} 必须是至少 {minimum} 的整数")
    if maximum is not None and value > maximum:
        raise ToolArgumentError(f"参数 {name} 不能超过 {maximum}")
    return value


__all__ = ["ArtifactStore", "ReadArtifactTool", "SearchArtifactTool"]
