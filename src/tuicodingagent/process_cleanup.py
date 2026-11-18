"""只针对本次启动进程的取消清理，不按进程名批量终止。"""

from __future__ import annotations

import asyncio
import ctypes
import os
import signal
import subprocess
from contextlib import suppress
from pathlib import Path


def process_group_options() -> dict:
    if os.name == "nt":
        # 命令继承当前控制台编码；只有回收辅助进程使用 CREATE_NO_WINDOW。
        return {}
    return {"start_new_session": True}


def decode_process_output(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        if os.name == "nt":
            # cmd 原生命令默认使用 OEM 代码页，Python/现代 CLI 则常输出 UTF-8。
            codepage = ctypes.windll.kernel32.GetOEMCP()
            return data.decode(f"cp{codepage}", errors="replace")
        return data.decode("utf-8", errors="replace")


async def _terminate_group(process: asyncio.subprocess.Process) -> None:
    if os.name == "nt":
        if process.returncode is None:
            executable = str(
                Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "System32/taskkill.exe"
            )
            killer = await asyncio.create_subprocess_exec(
                executable,
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            try:
                await asyncio.wait_for(killer.wait(), 5)
            except TimeoutError:
                killer.kill()
                await killer.wait()
            if killer.returncode and process.returncode is None:
                raise OSError("无法回收当前命令进程树")
    else:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)


async def _terminate_tree(process: asyncio.subprocess.Process) -> None:
    try:
        await _terminate_group(process)
    finally:
        # 平台清理命令不可用时也回收直接子进程，同时保留原错误供调用方审计。
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        await process.wait()


async def terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    """重复取消不会中断回收，完成后仍将取消传播给调用方。"""
    await await_cleanup(_terminate_tree(process))


async def await_cleanup(awaitable) -> None:
    """保留清理任务，收到额外取消时等待清理结束再传播取消。"""
    cleanup = asyncio.ensure_future(awaitable)
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    cleanup.result()
    if cancelled:
        raise asyncio.CancelledError
