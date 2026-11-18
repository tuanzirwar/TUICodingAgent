"""工具注册、协议 Schema 导出与安全执行。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from contextvars import ContextVar
from pathlib import Path

from ..models import ProtocolName, ToolCall, ToolDefinition, ToolResult
from ..trace import TraceEvent, TraceRecorder, token_estimate
from .artifacts import ReadArtifactTool, SearchArtifactTool
from .base import Tool, ToolArgumentError, failure
from .files import EditFileTool, ReadFileTool, WriteFileTool
from .search import GlobTool, GrepTool
from .shell import BashTool

TOOL_TIMEOUT = 30.0


class ToolRegistry:
    def __init__(
        self,
        tools: Iterable[Tool] = (),
        timeout: float = TOOL_TIMEOUT,
        trace_recorder: TraceRecorder | None = None,
    ) -> None:
        self.timeout = timeout
        self.trace_recorder = trace_recorder
        self.trace_session_id = ""
        self.trace_agent_id = ""
        self.trace_model = ""
        self._trace_context: ContextVar[tuple[str, str, str]] = ContextVar(
            "tool_trace_context", default=("", "", "")
        )
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def set_trace_context(self, session_id: str, agent_id: str, model: str) -> None:
        self.trace_session_id = session_id
        self.trace_agent_id = agent_id
        self.trace_model = model
        self._trace_context.set((session_id, agent_id, model))

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
        started = time.perf_counter()
        session_id, agent_id, model = self._trace_context.get()
        event = TraceEvent(
            "tool_call",
            request_id=call.id,
            session_id=session_id,
            agent_id=agent_id,
            model=model,
            tool_name=call.name,
            request_bytes=len(str(call.arguments).encode("utf-8")),
        )
        try:
            if call.parse_error:
                result = failure(call, f"工具参数 JSON 无效：{call.parse_error}")
            elif (tool := self.get(call.name)) is None:
                result = failure(call, f"未知工具：{call.name}")
            else:
                result = await asyncio.wait_for(tool.execute(call), timeout=self.timeout)
        except TimeoutError as exc:
            result = failure(call, f"工具执行超时（{self.timeout:g} 秒）", timed_out=True)
            event.error_type = type(exc).__name__
        except ToolArgumentError as exc:
            result = failure(call, f"工具参数错误：{exc}")
            event.error_type = type(exc).__name__
        except Exception as exc:  # 工具边界必须把意外异常转成可回灌结果
            result = failure(call, f"工具执行异常：{type(exc).__name__}: {exc}")
            event.error_type = type(exc).__name__
        output = result.output if result.success else result.error
        event.tool_latency_ms = round((time.perf_counter() - started) * 1000, 3)
        event.tool_output_bytes = len(output.encode("utf-8"))
        event.tool_output_tokens = token_estimate(output)
        event.response_bytes = event.tool_output_bytes
        event.finish(success=result.success)
        if self.trace_recorder is not None:
            self.trace_recorder.record(event)
        return result


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
            ReadArtifactTool(root),
            SearchArtifactTool(root),
        ),
        trace_recorder=TraceRecorder.for_workspace(root),
    )
