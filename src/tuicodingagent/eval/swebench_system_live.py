"""使用当前 codex-cli 参数布局运行评测系统。"""

from __future__ import annotations

from . import swebench_system as _system
from .adapters_live import CodexCLIAdapter

_system.CodexCLIAdapter = CodexCLIAdapter
run_system = _system.run_system
SystemOptions = _system.SystemOptions
SystemResult = _system.SystemResult
TestResult = _system.TestResult
write_reports = _system.write_reports

__all__ = ["SystemOptions", "SystemResult", "TestResult", "run_system", "write_reports"]
