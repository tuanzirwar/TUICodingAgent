"""Team 队员运行后端。"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class BackendType(StrEnum):
    TMUX = "tmux"
    ITERM2 = "iterm2"
    IN_PROCESS = "in-process"


@dataclass(frozen=True, slots=True)
class SpawnRequest:
    team_name: str
    member_name: str
    agent_id: str
    worktree_path: Path
    session_dir: Path
    agent_type: str = ""
    model: str = ""
    initial_prompt: str = ""
    plan_mode_required: bool = False
    sub_agent: object | None = None
    conv: object | None = None
    task_mgr: object | None = None


class Backend(Protocol):
    def type(self) -> BackendType: ...
    async def spawn(self, request: SpawnRequest) -> tuple[str, str]: ...
    async def wake(self, pane_id: str, agent_id: str) -> None: ...
    async def kill(self, pane_id: str, agent_id: str) -> None: ...


class InProcessBackend:
    """由 TeamService 注入 asyncio task 的同进程后端。"""

    def __init__(self) -> None:
        self.tasks: dict[str, asyncio.Task[object]] = {}

    def type(self) -> BackendType:
        return BackendType.IN_PROCESS

    async def spawn(self, request: SpawnRequest) -> tuple[str, str]:
        return "", request.agent_id

    async def wake(self, pane_id: str, agent_id: str) -> None:
        return None

    async def kill(self, pane_id: str, agent_id: str) -> None:
        task = self.tasks.get(agent_id)
        if task is not None and not task.done():
            task.cancel()


class TmuxBackend:
    def type(self) -> BackendType:
        return BackendType.TMUX

    async def spawn(self, request: SpawnRequest) -> tuple[str, str]:
        command = self._member_command(request)
        tmux_args = (
            ("split-window", "-h", "-P", "-F", "#{pane_id}", "--")
            if os.environ.get("TMUX")
            else ("new-session", "-d", "-P", "-F", "#{pane_id}")
        )
        process = await asyncio.create_subprocess_exec(
            "tmux",
            *tmux_args,
            *command,
            cwd=request.worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode:
            raise RuntimeError(stderr.decode(errors="replace").strip() or "tmux spawn 失败")
        return stdout.decode().strip(), request.agent_id

    async def wake(self, pane_id: str, agent_id: str) -> None:
        del agent_id
        await _run("tmux", "send-keys", "-t", pane_id, "", "Enter")

    async def kill(self, pane_id: str, agent_id: str) -> None:
        del agent_id
        if pane_id:
            await _run("tmux", "kill-pane", "-t", pane_id, check=False)

    @staticmethod
    def _member_command(request: SpawnRequest) -> tuple[str, ...]:
        args = [
            sys.executable,
            "-m",
            "tuicodingagent",
            "--team-member",
            "--team",
            request.team_name,
            "--member",
            request.member_name,
            "--agent-id",
            request.agent_id,
            "--session-dir",
            str(request.session_dir),
            "--worktree",
            str(request.worktree_path),
        ]
        if request.agent_type:
            args.extend(("--agent-type", request.agent_type))
        if request.model:
            args.extend(("--model", request.model))
        if request.plan_mode_required:
            args.append("--plan-mode")
        return tuple(args)


class ITerm2Backend:
    def type(self) -> BackendType:
        return BackendType.ITERM2

    async def spawn(self, request: SpawnRequest) -> tuple[str, str]:
        command = shlex.join(TmuxBackend._member_command(request))
        process = await asyncio.create_subprocess_exec(
            "it2",
            "split",
            "--new-pane",
            "--command",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode:
            raise RuntimeError(stderr.decode(errors="replace").strip() or "iTerm2 spawn 失败")
        return stdout.decode().strip(), request.agent_id

    async def wake(self, pane_id: str, agent_id: str) -> None:
        del agent_id
        await _run("it2", "send-text", "--pane", pane_id, "")

    async def kill(self, pane_id: str, agent_id: str) -> None:
        del agent_id
        if pane_id:
            await _run("it2", "close-pane", "--pane", pane_id, check=False)


def detect_backend() -> BackendType:
    if os.environ.get("TMUX"):
        return BackendType.TMUX
    if os.environ.get("TERM_PROGRAM") == "iTerm.app" and shutil.which("it2"):
        return BackendType.ITERM2
    if shutil.which("tmux"):
        return BackendType.TMUX
    return BackendType.IN_PROCESS


def new_backend(kind: BackendType, in_process: InProcessBackend | None = None) -> Backend:
    if kind == BackendType.TMUX:
        return TmuxBackend()
    if kind == BackendType.ITERM2:
        return ITerm2Backend()
    return in_process or InProcessBackend()


async def _run(*args: str, check: bool = True) -> None:
    process = await asyncio.create_subprocess_exec(*args)
    code = await process.wait()
    if code and check:
        raise RuntimeError(f"后端命令失败：{' '.join(args)}")


__all__ = [
    "Backend",
    "BackendType",
    "ITerm2Backend",
    "InProcessBackend",
    "SpawnRequest",
    "TmuxBackend",
    "detect_backend",
    "new_backend",
]
