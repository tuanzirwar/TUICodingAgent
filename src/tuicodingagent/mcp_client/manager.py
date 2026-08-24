"""MCP 多 Server 连接、发现与生命周期管理。"""

from __future__ import annotations

import asyncio
import re
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from ..tools.registry import ToolRegistry
from .config import HttpServerConfig, MCPServerConfig, StdioServerConfig
from .tool import MCP_TIMEOUT, MCPTool

CLOSE_TIMEOUT = 5.0
_VALID_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(slots=True)
class _LiveServer:
    name: str
    task: asyncio.Task[None]
    stop: asyncio.Event


class MCPClientManager:
    def __init__(self) -> None:
        self._live: list[_LiveServer] = []

    async def start(self, servers: tuple[MCPServerConfig, ...], registry: ToolRegistry) -> None:
        """并发连接全部 Server，并把成功发现的工具注册进中心。"""

        attempts = [self._start_one(server, registry) for server in servers]
        if attempts:
            await asyncio.gather(*attempts)

    async def _start_one(self, server: MCPServerConfig, registry: ToolRegistry) -> None:
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[tuple[ClientSession, list[types.Tool]]] = loop.create_future()
        stop = asyncio.Event()
        task = asyncio.create_task(
            self._own_connection(server, ready, stop), name=f"mcp:{server.name}"
        )
        try:
            session, tools = await asyncio.wait_for(asyncio.shield(ready), MCP_TIMEOUT)
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        except TimeoutError:
            _warn(server.name, f"启动超时（{MCP_TIMEOUT:g} 秒）")
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return
        except Exception as exc:
            _warn(server.name, f"启动失败：{type(exc).__name__}: {exc}")
            await asyncio.gather(task, return_exceptions=True)
            return

        self._live.append(_LiveServer(server.name, task, stop))
        for remote in tools:
            full_name = f"mcp__{server.name}__{remote.name}"
            if _VALID_TOOL_NAME.fullmatch(full_name) is None:
                _warn(server.name, f"工具名 {remote.name!r} 含禁用字符，已跳过")
                continue
            replace = registry.get(full_name) is not None
            if replace:
                _warn(server.name, f"工具 {full_name} 重名，保留后注册者")
            registry.register(MCPTool(server.name, remote, session), replace=replace)

    async def _own_connection(
        self,
        server: MCPServerConfig,
        ready: asyncio.Future[tuple[ClientSession, list[types.Tool]]],
        stop: asyncio.Event,
    ) -> None:
        """在同一任务内创建并关闭 SDK 上下文，满足 AnyIO 约束。"""

        try:
            async with (
                _transport(server) as (read_stream, write_stream),
                ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=MCP_TIMEOUT),
                ) as session,
            ):
                await session.initialize()
                listing = await session.list_tools()
                if not ready.done():
                    ready.set_result((session, list(listing.tools)))
                await stop.wait()
        except asyncio.CancelledError:
            if not ready.done():
                ready.cancel()
            raise
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)
            else:
                _warn(server.name, f"连接已终止：{type(exc).__name__}: {exc}")

    async def close(self) -> None:
        """通知所有 Server 退出，并以五秒总超时兜底。"""

        live, self._live = self._live, []
        for item in live:
            item.stop.set()
        tasks = [item.task for item in live]
        if not tasks:
            return
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), CLOSE_TIMEOUT)
        except TimeoutError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            print("警告：MCP 连接关闭超过 5 秒，已强制取消", file=sys.stderr)


@asynccontextmanager
async def _transport(server: MCPServerConfig) -> AsyncIterator[tuple[Any, Any]]:
    if isinstance(server, StdioServerConfig):
        params = StdioServerParameters(
            command=server.command,
            args=list(server.args),
            env=server.env,
        )
        # pytest/capture 等环境中的 sys.stderr 可能没有 fileno，子进程必须使用真实句柄。
        errlog = sys.stderr if _has_fileno(sys.stderr) else sys.__stderr__
        async with stdio_client(params, errlog=errlog) as streams:
            yield streams
        return
    assert isinstance(server, HttpServerConfig)
    timeout = httpx.Timeout(MCP_TIMEOUT, read=MCP_TIMEOUT)
    async with (
        httpx.AsyncClient(
            headers=server.headers, timeout=timeout, follow_redirects=True
        ) as http_client,
        streamable_http_client(
            server.url, http_client=http_client, terminate_on_close=True
        ) as streams,
    ):
        yield streams[0], streams[1]


def _warn(server: str, message: str) -> None:
    print(f"警告：MCP Server {server} {message}", file=sys.stderr)


def _has_fileno(stream: Any) -> bool:
    try:
        stream.fileno()
    except (AttributeError, OSError, ValueError):
        return False
    return True
