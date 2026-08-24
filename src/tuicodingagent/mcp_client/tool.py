"""把 MCP 远端工具适配为 TUICodingAgent Tool。"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, Protocol

from mcp import types

from ..models import ToolCall, ToolDefinition, ToolResult

MCP_TIMEOUT = 30.0


class ToolSession(Protocol):
    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> types.CallToolResult: ...


class MCPTool:
    def __init__(self, server: str, remote: types.Tool, session: ToolSession) -> None:
        self.server = server
        self.remote_name = remote.name
        self.session = session
        self.definition = ToolDefinition(
            f"mcp__{server}__{remote.name}",
            remote.description or f"由 MCP Server {server} 提供的工具",
            dict(remote.inputSchema),
        )
        annotations = remote.annotations
        self.read_only = bool(annotations and annotations.readOnlyHint is True)
        self._warned_non_text = False

    async def execute(self, call: ToolCall) -> ToolResult:
        try:
            result = await asyncio.wait_for(
                self.session.call_tool(self.remote_name, dict(call.arguments)),
                timeout=MCP_TIMEOUT,
            )
        except TimeoutError:
            return _failure(call, f"MCP 工具调用超时（{MCP_TIMEOUT:g} 秒）", timed_out=True)
        except Exception as exc:
            return _failure(call, f"MCP 工具调用失败：{type(exc).__name__}: {exc}")

        texts: list[str] = []
        saw_non_text = False
        for block in result.content:
            if isinstance(block, types.TextContent):
                texts.append(block.text)
            else:
                saw_non_text = True
        if saw_non_text and not self._warned_non_text:
            print(
                f"警告：MCP 工具 {self.definition.name} 返回了非文本内容，已忽略",
                file=sys.stderr,
            )
            self._warned_non_text = True
        content = "\n".join(texts)
        if result.isError:
            return _failure(call, content or "MCP Server 返回工具错误", remote_error=True)
        return ToolResult(call.id, call.name, True, output=content)


def _failure(call: ToolCall, error: str, **metadata: object) -> ToolResult:
    return ToolResult(call.id, call.name, False, error=error, metadata=metadata)
