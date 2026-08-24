from __future__ import annotations

from pathlib import Path

from mcp import types

from tuicodingagent.mcp_client import MCPPermissionManager, MCPTool
from tuicodingagent.models import PermissionDecision, PermissionMode, ToolCall
from tuicodingagent.runtime import _execution_batches
from tuicodingagent.tools import create_default_registry


class UnusedSession:
    async def call_tool(self, name, arguments=None):
        raise AssertionError("不应执行")


def _register_mcp(registry, name: str, *, read_only: bool) -> None:
    remote = types.Tool(
        name=name,
        description="测试",
        inputSchema={"type": "object"},
        annotations=types.ToolAnnotations(readOnlyHint=read_only),
    )
    registry.register(MCPTool("demo", remote, UnusedSession()))


def test_mcp_permissions_rules_modes_and_safe_default(tmp_path: Path) -> None:
    settings = tmp_path / ".tuicodingagent" / "settings.local.yaml"
    settings.parent.mkdir()
    settings.write_text(
        """
permissions:
  allow: ["mcp__demo__*"]
  deny: ["mcp__demo__mutate"]
""",
        encoding="utf-8",
    )
    registry = create_default_registry(tmp_path)
    _register_mcp(registry, "lookup", read_only=True)
    _register_mcp(registry, "mutate", read_only=False)
    manager = MCPPermissionManager.load(tmp_path, registry)

    lookup = ToolCall("1", "mcp__demo__lookup", {})
    mutate = ToolCall("2", "mcp__demo__mutate", {})
    unknown = ToolCall("3", "mcp__demo__missing", {})
    assert manager.evaluate(lookup).decision == PermissionDecision.ALLOW
    assert manager.evaluate(mutate).decision == PermissionDecision.DENY
    assert manager.evaluate(unknown).decision == PermissionDecision.DENY

    settings.write_text("permissions: {}\n", encoding="utf-8")
    manager = MCPPermissionManager.load(tmp_path, registry)
    assert manager.evaluate(lookup).decision == PermissionDecision.ALLOW
    assert manager.evaluate(mutate).decision == PermissionDecision.ASK
    manager.set_mode(PermissionMode.BYPASS)
    assert manager.evaluate(mutate).decision == PermissionDecision.ALLOW


def test_mcp_read_only_tools_batch_and_permanent_rule_reload(tmp_path: Path) -> None:
    registry = create_default_registry(tmp_path)
    _register_mcp(registry, "first", read_only=True)
    _register_mcp(registry, "second", read_only=True)
    _register_mcp(registry, "mutate", read_only=False)
    manager = MCPPermissionManager.load(tmp_path, registry)
    calls = [
        ToolCall("1", "mcp__demo__first", {}),
        ToolCall("2", "mcp__demo__second", {}),
        ToolCall("3", "mcp__demo__mutate", {}),
    ]
    assert [[call.id for call in batch] for batch in _execution_batches(calls, registry)] == [
        ["1", "2"],
        ["3"],
    ]
    assert manager.allow_always("mcp__demo__mutate") is True
    reloaded = MCPPermissionManager.load(tmp_path, registry)
    assert reloaded.evaluate(calls[2]).decision == PermissionDecision.ALLOW
