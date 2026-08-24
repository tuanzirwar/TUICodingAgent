"""工具接口、参数校验和结果帮助函数。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import ToolCall, ToolDefinition, ToolResult

MAX_TEXT_CHARS = 200_000


@runtime_checkable
class Tool(Protocol):
    definition: ToolDefinition
    read_only: bool

    async def execute(self, call: ToolCall) -> ToolResult: ...


class ToolArgumentError(ValueError):
    """模型提供的工具参数不符合 Schema。"""


def require_string(call: ToolCall, name: str) -> str:
    value = call.arguments.get(name)
    if not isinstance(value, str) or not value:
        raise ToolArgumentError(f"参数 {name} 必须是非空字符串")
    return value


def optional_string(call: ToolCall, name: str, default: str) -> str:
    value = call.arguments.get(name, default)
    if not isinstance(value, str) or not value:
        raise ToolArgumentError(f"参数 {name} 必须是非空字符串")
    return value


def truncate_text(text: str, limit: int = MAX_TEXT_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    omitted = len(text) - limit
    return f"{text[:limit]}\n… 已截断 {omitted} 个字符", True


def failure(call: ToolCall, error: str, **metadata: object) -> ToolResult:
    return ToolResult(
        tool_call_id=call.id,
        name=call.name,
        success=False,
        error=error,
        metadata=metadata,
    )
