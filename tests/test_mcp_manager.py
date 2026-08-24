from __future__ import annotations

import asyncio
import os
import socket
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp import types

import tuicodingagent.mcp_client.manager as manager_module
from tuicodingagent.mcp_client import (
    HttpServerConfig,
    MCPClientManager,
    StdioServerConfig,
)
from tuicodingagent.models import ToolCall
from tuicodingagent.tools import create_default_registry


async def test_real_stdio_mcp_discovery_call_env_and_clean_close(tmp_path: Path) -> None:
    registry = create_default_registry(tmp_path)
    server_script = Path(__file__).with_name("mcp_test_server.py")
    config = StdioServerConfig(
        "local",
        "stdio",
        sys.executable,
        (str(server_script),),
        {"MCP_TEST_MARKER": "injected"},
    )
    manager = MCPClientManager()
    await manager.start((config,), registry)

    assert registry.get("mcp__local__echo") is not None
    assert registry.get("mcp__local__mutate") is not None
    assert registry.is_read_only("mcp__local__echo") is True
    result = await registry.execute(ToolCall("1", "mcp__local__echo", {"text": "你好"}))
    assert result.success
    assert "echo:你好" in result.output
    assert "marker:injected" in result.output

    owner_tasks = [item.task for item in manager._live]
    await manager.close()
    assert owner_tasks and all(task.done() for task in owner_tasks)
    assert manager._live == []


async def test_stdio_start_failure_is_isolated(tmp_path: Path, capsys) -> None:
    registry = create_default_registry(tmp_path)
    manager = MCPClientManager()
    missing = StdioServerConfig("missing", "stdio", "definitely-not-a-command")
    server_script = Path(__file__).with_name("mcp_test_server.py")
    working = StdioServerConfig("working", "stdio", sys.executable, (str(server_script),))
    await manager.start((missing, working), registry)
    assert registry.get("mcp__working__echo") is not None
    assert registry.get("mcp__missing__echo") is None
    assert "missing 启动失败" in capsys.readouterr().err
    await manager.close()


async def test_real_streamable_http_discovery_and_call(tmp_path: Path) -> None:
    server_script = Path(__file__).with_name("mcp_test_server.py")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {**os.environ, "MCP_TEST_PORT": str(port), "MCP_TEST_MARKER": "http"}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(server_script),
        "http",
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        _wait_for_port(port)
        registry = create_default_registry(tmp_path)
        manager = MCPClientManager()
        await manager.start(
            (HttpServerConfig("web", "http", f"http://127.0.0.1:{port}/mcp"),),
            registry,
        )
        result = await registry.execute(ToolCall("1", "mcp__web__echo", {"text": "HTTP"}))
        assert result.success and "echo:HTTP" in result.output and "marker:http" in result.output
        await manager.close()
    finally:
        process.terminate()
        await asyncio.wait_for(process.wait(), timeout=5)


async def test_http_transport_forwards_headers(monkeypatch) -> None:
    captured = {}

    @asynccontextmanager
    async def fake_http(url, **kwargs):
        captured.update(url=url, **kwargs)
        yield "read", "write", lambda: "session"

    monkeypatch.setattr(manager_module, "streamable_http_client", fake_http)
    config = HttpServerConfig(
        "web", "http", "https://example.test/mcp", {"Authorization": "Bearer hidden"}
    )
    async with manager_module._transport(config) as streams:
        assert streams == ("read", "write")
    assert captured["http_client"].headers["Authorization"] == "Bearer hidden"
    assert captured["terminate_on_close"] is True


async def test_stdio_start_timeout_cleans_owner_task(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(manager_module, "MCP_TIMEOUT", 0.1)
    registry = create_default_registry(tmp_path)
    manager = MCPClientManager()
    sleeping = StdioServerConfig(
        "sleeping",
        "stdio",
        sys.executable,
        ("-c", "import time; time.sleep(10)"),
    )
    await manager.start((sleeping,), registry)
    assert manager._live == []
    assert "启动超时" in capsys.readouterr().err


def _wait_for_port(port: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with socket.socket() as client:
            client.settimeout(0.1)
            if client.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise AssertionError("HTTP MCP 测试服务未能启动")


async def test_invalid_and_duplicate_remote_tool_names_are_isolated(tmp_path: Path, capsys) -> None:
    class FakeManager(MCPClientManager):
        async def _own_connection(self, server, ready, stop):
            tools = [
                types.Tool(name="bad.name", description="坏", inputSchema={"type": "object"}),
                types.Tool(name="same", description="旧", inputSchema={"type": "object"}),
                types.Tool(name="same", description="新", inputSchema={"type": "object"}),
            ]
            ready.set_result((object(), tools))
            await stop.wait()

    registry = create_default_registry(tmp_path)
    manager = FakeManager()
    config = StdioServerConfig("demo", "stdio", "unused")
    await manager.start((config,), registry)
    assert registry.get("mcp__demo__bad.name") is None
    same = registry.get("mcp__demo__same")
    assert same is not None and same.definition.description == "新"
    warning = capsys.readouterr().err
    assert "禁用字符" in warning and "重名" in warning
    await manager.close()


async def test_cancelled_start_cleans_connection_owner(tmp_path: Path) -> None:
    class BlockingManager(MCPClientManager):
        def __init__(self):
            super().__init__()
            self.owner_task = None
            self.started = asyncio.Event()

        async def _own_connection(self, server, ready, stop):
            self.owner_task = asyncio.current_task()
            self.started.set()
            await asyncio.Event().wait()

    registry = create_default_registry(tmp_path)
    manager = BlockingManager()
    start = asyncio.create_task(
        manager.start((StdioServerConfig("blocked", "stdio", "unused"),), registry)
    )
    await manager.started.wait()
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    assert manager.owner_task is not None and manager.owner_task.done()
