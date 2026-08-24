"""工具注册、协议 Schema 导出与安全执行。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from pathlib import Path

from ..models import ProtocolName, ToolCall, ToolDefinition, ToolResult
from .base import Tool, ToolArgumentError, failure
from .files import EditFileTool, ReadFileTool, WriteFileTool
from .search import GlobTool, GrepTool
from .shell import BashTool

TOOL_TIMEOUT = 30.0


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = (), timeout: float = TOOL_TIMEOUT) -> None:
        self.timeout = timeout
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool, *, replace: bool = False) -> None:
        name = tool.definition.name
        if name in self._tools and not replace:
            raise ValueError(f"工具已注册：{name}")
        self._tools[name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(tool.definition for tool in self._tools.values())

    def names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def definitions_for_mode(self, *, read_only: bool) -> tuple[ToolDefinition, ...]:
        """计划模式只向模型公开无副作用工具。"""

        if not read_only:
            return self.definitions()
        return tuple(tool.definition for tool in self._tools.values() if tool.read_only)

    def filtered_definitions(
        self,
        allowed: frozenset[str] | None,
        *,
        read_only: bool,
        system_tools: frozenset[str] = frozenset(),
    ) -> tuple[ToolDefinition, ...]:
        """按 Skill 白名单收窄可见工具，同时始终保留系统工具。"""

        return tuple(
            tool.definition
            for name, tool in self._tools.items()
            if (allowed is None or name in allowed or name in system_tools)
            and (not read_only or tool.read_only)
        )

    def is_read_only(self, name: str) -> bool:
        tool = self.get(name)
        return bool(tool and tool.read_only)

    def api_definitions(self, protocol: ProtocolName) -> list[dict[str, object]]:
        if protocol == "openai":
            return [
                {
                    "type": "function",
                    "function": {
                        "name": item.name,
                        "description": item.description,
                        "parameters": item.input_schema,
                    },
                }
                for item in self.definitions()
            ]
        return [
            {
                "name": item.name,
                "description": item.description,
                "input_schema": item.input_schema,
            }
            for item in self.definitions()
        ]

    async def execute(self, call: ToolCall) -> ToolResult:
        if call.parse_error:
            return failure(call, f"工具参数 JSON 无效：{call.parse_error}")
        tool = self.get(call.name)
        if tool is None:
            return failure(call, f"未知工具：{call.name}")
        try:
            return await asyncio.wait_for(tool.execute(call), timeout=self.timeout)
        except TimeoutError:
            return failure(call, f"工具执行超时（{self.timeout:g} 秒）", timed_out=True)
        except ToolArgumentError as exc:
            return failure(call, f"工具参数错误：{exc}")
        except Exception as exc:  # 工具边界必须把意外异常转成可回灌结果
            return failure(call, f"工具执行异常：{type(exc).__name__}: {exc}")


def create_default_registry(workspace: Path) -> ToolRegistry:
    root = workspace.resolve()
    return ToolRegistry(
        (
            ReadFileTool(root),
            WriteFileTool(root),
            EditFileTool(root),
            BashTool(root),
            GlobTool(root),
            GrepTool(root),
        )
    )
