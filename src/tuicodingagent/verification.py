"""代码修改后的语法、Lint、测试与 diff 验证。"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path

from .process_cleanup import decode_process_output, process_group_options, terminate_process_tree


@dataclass(frozen=True, slots=True)
class VerificationResult:
    success: bool
    output: str
    steps: int


class VerificationRunner:
    def __init__(
        self,
        workspace: Path,
        *,
        max_steps: int = 4,
        step_timeout: float = 120.0,
        total_timeout: float = 300.0,
    ) -> None:
        self.workspace = workspace.resolve()
        self.max_steps = max_steps
        self.step_timeout = step_timeout
        self.total_timeout = total_timeout

    async def working_state(self) -> dict[Path, str]:
        """记录 Git 工作区变化指纹，识别 shell 修改和部分成功。"""
        if not (self.workspace / ".git").exists():
            return {}
        paths: set[Path] = set()
        for arguments in (
            ["diff", "--name-only", "-z"],
            ["diff", "--cached", "--name-only", "-z"],
            ["ls-files", "--others", "--exclude-standard", "-z"],
        ):
            process = await asyncio.create_subprocess_exec(
                "git",
                *arguments,
                cwd=self.workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                **process_group_options(),
            )
            try:
                output, _ = await asyncio.wait_for(process.communicate(), self.step_timeout)
            except BaseException:
                await terminate_process_tree(process)
                raise
            if process.returncode:
                raise RuntimeError("无法检查 shell 操作后的 Git 状态")
            for name in output.decode("utf-8", errors="replace").split("\0"):
                if name:
                    path = (self.workspace / name).resolve(strict=False)
                    if path.is_relative_to(self.workspace):
                        paths.add(path)

        def fingerprint() -> dict[Path, str]:
            state = {}
            for path in paths:
                try:
                    state[path] = hashlib.sha256(path.read_bytes()).hexdigest()
                except FileNotFoundError:
                    state[path] = "deleted"
                except OSError:
                    state[path] = "unreadable"
            return state

        return await asyncio.to_thread(fingerprint)

    async def run(self, paths: set[Path]) -> VerificationResult:
        if not (self.workspace / ".git").exists():
            return VerificationResult(True, "非 Git 工作区，跳过自动验证", 0)
        relative = sorted(
            str(path.resolve().relative_to(self.workspace))
            for path in paths
            if path.resolve().is_relative_to(self.workspace)
        )
        if not relative:
            return VerificationResult(True, "没有可验证的修改文件", 0)
        commands: list[tuple[str, list[str]]] = []
        python_files = [
            path for path in relative if path.endswith(".py") and (self.workspace / path).is_file()
        ]
        if python_files:
            commands.append(("syntax", [sys.executable, "-m", "py_compile", *python_files]))
            if importlib.util.find_spec("ruff") is not None:
                commands.append(("lint", [sys.executable, "-m", "ruff", "check", *python_files]))
        if (self.workspace / "tests").is_dir():
            commands.append(("test", [sys.executable, "-m", "pytest", "-q", "--maxfail=1"]))
        commands.append(("diff", ["git", "diff", "--check", "--", *relative]))
        commands = commands[: self.max_steps]
        try:
            async with asyncio.timeout(self.total_timeout):
                reports = [await self._command(name, args) for name, args in commands]
        except TimeoutError:
            return VerificationResult(False, "verification 总超时", len(commands))
        success = all(code == 0 for _, code, _ in reports)
        output = "\n".join(f"[{name}] exit={code}\n{text[-4000:]}" for name, code, text in reports)
        return VerificationResult(success, output, len(reports))

    async def _command(self, name: str, args: list[str]) -> tuple[str, int, str]:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=self.workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **process_group_options(),
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=self.step_timeout)
        except TimeoutError:
            await terminate_process_tree(process)
            return name, 124, f"步骤超时（{self.step_timeout:g}s）"
        except BaseException:
            # 总超时和用户取消都以 CancelledError 进入这里，返回前回收进程树。
            await terminate_process_tree(process)
            raise
        return name, process.returncode or 0, decode_process_output(output)


__all__ = ["VerificationResult", "VerificationRunner"]
