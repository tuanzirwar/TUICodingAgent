"""TUICodingAgent 命令行启动边界。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from . import __version__
from .config import ConfigLoader
from .errors import ConfigError
from .hooks import HookEngine, load_hooks
from .instructions import load_instructions
from .mcp_client import MCPClientManager, MCPPermissionManager, load_mcp_servers
from .memory import MemoryManager
from .session import cleanup_expired_sessions
from .tools import create_default_registry
from .tui import TUICodingAgentApp


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tuicodingagent", description="终端 AI 编程助手")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--team-member", action="store_true", help=argparse.SUPPRESS)
    for option in (
        "team",
        "member",
        "agent-id",
        "session-dir",
        "worktree",
        "agent-type",
        "model",
    ):
        parser.add_argument(f"--{option}", help=argparse.SUPPRESS)
    parser.add_argument("--plan-mode", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.team_member:
        required = ("team", "member", "agent_id", "session_dir", "worktree")
        missing = [name.replace("_", "-") for name in required if not getattr(args, name)]
        if missing:
            build_parser().error("--team-member 缺少参数：" + ", ".join(missing))
        from .team.member import run_team_member

        return asyncio.run(run_team_member(args))
    config_path = Path.cwd() / "config.yaml"
    try:
        config = ConfigLoader.load(config_path)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    workspace = Path.cwd().resolve()
    asyncio.run(_run_app(config, workspace))
    return 0


async def _run_app(config, workspace: Path) -> None:
    registry = create_default_registry(workspace)
    mcp_manager = MCPClientManager()
    instructions = await asyncio.to_thread(load_instructions, workspace)
    memory_manager = MemoryManager(workspace)
    hook_rules, hook_sources = load_hooks(workspace)
    hook_engine = HookEngine(hook_rules, hook_sources)
    cleanup_task = asyncio.create_task(asyncio.to_thread(cleanup_expired_sessions, workspace))
    try:
        await mcp_manager.start(load_mcp_servers(workspace), registry)
        permission_manager = MCPPermissionManager.load(workspace, registry)
        app = TUICodingAgentApp(
            config,
            workspace=workspace,
            tool_registry=registry,
            permission_manager=permission_manager,
            instructions=instructions,
            memory_manager=memory_manager,
            hook_engine=hook_engine,
        )
        await app.run_async()
    finally:
        await asyncio.gather(cleanup_task, return_exceptions=True)
        await mcp_manager.close()


if __name__ == "__main__":
    raise SystemExit(main())
