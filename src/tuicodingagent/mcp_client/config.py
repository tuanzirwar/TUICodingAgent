"""MCP Server 两层配置加载与变量展开。"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True, slots=True)
class StdioServerConfig:
    name: str
    type: Literal["stdio"]
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class HttpServerConfig:
    name: str
    type: Literal["http"]
    url: str
    headers: dict[str, str] | None = field(default=None, repr=False)


MCPServerConfig = StdioServerConfig | HttpServerConfig


def mcp_config_paths(workspace: Path) -> tuple[Path, Path]:
    """返回用户级与项目级 MCP 配置路径。"""

    return (
        Path.home() / ".tuicodingagent" / "config.yaml",
        workspace / ".tuicodingagent.yaml",
    )


def load_mcp_servers(
    workspace: Path,
    paths: tuple[Path, Path] | None = None,
    environ: dict[str, str] | None = None,
) -> tuple[MCPServerConfig, ...]:
    """按 Server 名合并配置；项目级同名定义完整覆盖用户级。"""

    merged: dict[str, object] = {}
    for path in paths or mcp_config_paths(workspace):
        raw = _load_mapping(path)
        servers = raw.get("mcp_servers") if raw is not None else None
        if servers is None:
            continue
        if not isinstance(servers, dict):
            _warn(f"MCP 配置 {path} 的 mcp_servers 必须是 map，已跳过")
            continue
        merged.update(servers)

    result: list[MCPServerConfig] = []
    selected_env = dict(os.environ if environ is None else environ)
    for name, definition in merged.items():
        parsed = _parse_server(name, definition, selected_env)
        if parsed is not None:
            result.append(parsed)
    return tuple(result)


def _load_mapping(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        # YAML 解析器异常可能包含原始行，不能把潜在凭据回显到终端。
        _warn(f"MCP 配置 {path} YAML 格式错误，已跳过")
        return None
    except (OSError, UnicodeError) as exc:
        _warn(f"MCP 配置 {path} 无法读取：{type(exc).__name__}，已跳过")
        return None
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        _warn(f"MCP 配置 {path} 顶层必须是 map，已跳过")
        return None
    return raw


def _parse_server(
    name: object, definition: object, environ: dict[str, str]
) -> MCPServerConfig | None:
    if not isinstance(name, str) or not name or not isinstance(definition, dict):
        _warn(f"MCP Server {name!r} 定义无效，已跳过")
        return None
    server_type = definition.get("type")
    if server_type == "stdio":
        command = definition.get("command")
        args = definition.get("args", [])
        env = definition.get("env", {})
        if not isinstance(command, str) or not command:
            _warn(f"MCP Server {name} 缺少非空 command，已跳过")
            return None
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            _warn(f"MCP Server {name} 的 args 必须是字符串数组，已跳过")
            return None
        expanded = _string_map(env, environ, name, "env")
        if expanded is None:
            return None
        return StdioServerConfig(name, "stdio", command, tuple(args), expanded or None)
    if server_type == "http":
        url = definition.get("url")
        if not isinstance(url, str) or not url:
            _warn(f"MCP Server {name} 缺少非空 url，已跳过")
            return None
        headers = _string_map(definition.get("headers", {}), environ, name, "headers")
        if headers is None:
            return None
        return HttpServerConfig(name, "http", url, headers or None)
    _warn(f"MCP Server {name} 的 type 必须是 stdio 或 http，已跳过")
    return None


def _string_map(
    value: object, environ: dict[str, str], server: str, field: str
) -> dict[str, str] | None:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        _warn(f"MCP Server {server} 的 {field} 必须是字符串 map，已跳过")
        return None
    return {
        key: _VARIABLE.sub(lambda match: _environment_value(match.group(1), environ, server), item)
        for key, item in value.items()
    }


def _environment_value(name: str, environ: dict[str, str], server: str) -> str:
    if name not in environ:
        _warn(f"MCP Server {server} 引用了未定义环境变量 {name}，已展开为空串")
    return environ.get(name, "")


def _warn(message: str) -> None:
    print(f"警告：{message}", file=sys.stderr)
