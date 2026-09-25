"""代码修改后的语法、Lint、测试与 diff 验证。"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path


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

    async def run(self, paths: set[Path]) -> VerificationResult:
        if not (self.workspace / ".git").exists():
            return VerificationResult(True, "非 Git 工作区，跳过自动验证", 0)
        relative = sorted(
            str(path.resolve().relative_to(self.workspace))
            for path in paths
            if path.exists() and path.resolve().is_relative_to(self.workspace)
        )
        if not relative:
            return VerificationResult(True, "没有可验证的修改文件", 0)
        commands: list[tuple[str, list[str]]] = []
        python_files = [path for path in relative if path.endswith(".py")]
        if python_files:
            commands.append(("syntax", [sys.executable, "-m", "py_compile", *python_files]))
            if importlib.util.find_spec("ruff") is not None:
                commands.append(("lint", [sys.executable, "-m", "ruff", "check", *python_files]))
        if (self.workspace / "tests").is_dir():
            commands.append(
                ("test", [sys.executable, "-m", "pytest", "-q", "--maxfail=1"])
            )
        commands.append(("diff", ["git", "diff", "--check", "--", *relative]))
        commands = commands[: self.max_steps]
        try:
            async with asyncio.timeout(self.total_timeout):
                reports = [await self._command(name, args) for name, args in commands]
        except TimeoutError:
            return VerificationResult(False, "verification 总超时", len(commands))
        success = all(code == 0 for _, code, _ in reports)
        output = "\n".join(
            f"[{name}] exit={code}\n{text[-4000:]}" for name, code, text in reports
        )
        return VerificationResult(success, output, len(reports))

    async def _command(self, name: str, args: list[str]) -> tuple[str, int, str]:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=self.workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(
                process.communicate(), timeout=self.step_timeout
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            return name, 124, f"步骤超时（{self.step_timeout:g}s）"
        return name, process.returncode or 0, output.decode("utf-8", errors="replace")


__all__ = ["VerificationResult", "VerificationRunner"]
