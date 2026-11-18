"""异步 shell 命令工具。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..models import ToolCall, ToolDefinition, ToolResult
from ..process_cleanup import decode_process_output, process_group_options, terminate_process_tree
from .base import failure, require_string, truncate_text
from .context import cwd_from_ctx

COMMAND_TIMEOUT = 30.0
MAX_COMMAND_CHARS = 100_000


class BashTool:
    read_only = False
    definition = ToolDefinition(
        "bash",
        "在当前工作目录执行 shell 命令，返回 stdout、stderr 和退出码；"
        "已有专用工具可完成时必须优先使用专用工具",
        {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "要执行的 shell 命令"}},
            "required": ["command"],
            "additionalProperties": False,
        },
    )

    def __init__(self, workspace: Path, timeout: float = COMMAND_TIMEOUT) -> None:
        self.workspace = workspace
        self.timeout = timeout

    async def execute(self, call: ToolCall) -> ToolResult:
        try:
            command = require_string(call, "command")
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd_from_ctx() or self.workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **process_group_options(),
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(), timeout=self.timeout
                )
            except TimeoutError:
                await terminate_process_tree(process)
                return failure(call, f"命令执行超时（{self.timeout:g} 秒）", timed_out=True)
            except BaseException:
                await terminate_process_tree(process)
                raise
            stdout, out_cut = truncate_text(
                decode_process_output(stdout_bytes), MAX_COMMAND_CHARS
            )
            stderr, err_cut = truncate_text(
                decode_process_output(stderr_bytes), MAX_COMMAND_CHARS
            )
            output = f"exit_code: {process.returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}"
            success = process.returncode == 0
            return ToolResult(
                call.id,
                call.name,
                success,
                output=output,
                error="" if success else f"命令以非零退出码 {process.returncode} 结束",
                metadata={"exit_code": process.returncode},
                truncated=out_cut or err_cut,
            )
        except (OSError, ValueError) as exc:
            return failure(call, f"命令执行失败：{exc}")
