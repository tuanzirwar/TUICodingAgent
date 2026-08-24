from __future__ import annotations

import asyncio

from mcp import types

from tuicodingagent.mcp_client import MCPTool
from tuicodingagent.models import ToolCall


class FakeSession:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = []

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        if self.error:
            raise self.error
        return self.result


def _remote(read_only: bool | None = None) -> types.Tool:
    annotations = types.ToolAnnotations(readOnlyHint=read_only) if read_only is not None else None
    return types.Tool(
        name="lookup",
        description=None,
        inputSchema={"type": "object", "properties": {"query": {"type": "string"}}},
        annotations=annotations,
    )


async def test_mcp_tool_adapts_schema_text_and_remote_error(capsys) -> None:
    success = types.CallToolResult(
        content=[
            types.TextContent(type="text", text="第一段"),
            types.ImageContent(type="image", data="AA==", mimeType="image/png"),
            types.TextContent(type="text", text="第二段"),
        ]
    )
    tool = MCPTool("demo", _remote(True), FakeSession(success))
    call = ToolCall("1", "mcp__demo__lookup", {"query": "x"})
    result = await tool.execute(call)
    assert tool.read_only is True
    assert tool.definition.input_schema["properties"] == {"query": {"type": "string"}}
    assert tool.definition.description == "由 MCP Server demo 提供的工具"
    assert result.success and result.output == "第一段\n第二段"
    assert "非文本内容" in capsys.readouterr().err

    remote_error = types.CallToolResult(
        content=[types.TextContent(type="text", text="远端拒绝")], isError=True
    )
    failed = await MCPTool("demo", _remote(), FakeSession(remote_error)).execute(call)
    assert not failed.success and failed.error == "远端拒绝"


async def test_mcp_tool_converts_protocol_error_and_timeout(monkeypatch) -> None:
    call = ToolCall("1", "mcp__demo__lookup", {})
    failed = await MCPTool("demo", _remote(), FakeSession(error=ConnectionError("断开"))).execute(
        call
    )
    assert not failed.success and "ConnectionError" in failed.error

    async def immediate_timeout(awaitable, **kwargs):
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", immediate_timeout)
    timed_out = await MCPTool("demo", _remote(), FakeSession(None)).execute(call)
    assert not timed_out.success and timed_out.metadata["timed_out"] is True
