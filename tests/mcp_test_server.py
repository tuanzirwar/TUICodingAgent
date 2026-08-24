"""供自动化与 PTY 验收使用的真实 MCP Server。"""

from __future__ import annotations

import os
import sys

from mcp import types
from mcp.server.fastmcp import FastMCP


def build_server() -> FastMCP:
    port = int(os.environ.get("MCP_TEST_PORT", "8765"))
    server = FastMCP(
        "tuicodingagent-test",
        host="127.0.0.1",
        port=port,
        streamable_http_path="/mcp",
        json_response=True,
    )

    @server.tool(annotations=types.ToolAnnotations(readOnlyHint=True))
    def echo(text: str) -> str:
        """原样返回文本与测试环境变量。"""

        marker = os.environ.get("MCP_TEST_MARKER", "")
        return f"echo:{text};marker:{marker};pid:{os.getpid()}"

    @server.tool()
    def mutate(value: str) -> str:
        """模拟一个具有副作用的远端操作。"""

        return f"mutated:{value}"

    @server.tool(annotations=types.ToolAnnotations(readOnlyHint=True))
    def large_result() -> str:
        """返回足以触发上下文落盘的确定性大文本。"""

        return "L" * 60_000

    return server


if __name__ == "__main__":
    transport = "streamable-http" if len(sys.argv) > 1 and sys.argv[1] == "http" else "stdio"
    build_server().run(transport=transport)
