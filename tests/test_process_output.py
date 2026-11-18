"""Windows 原生代码页与 UTF-8 输出都要保持实际中文证据。"""

import ctypes
import os
import sys
from pathlib import Path

import pytest

from tuicodingagent.models import ToolCall
from tuicodingagent.tools.shell import BashTool


@pytest.mark.parametrize("encoding", ["utf-8", "native"])
async def test_bash_preserves_unicode_output(tmp_path: Path, encoding: str) -> None:
    expected = "中文工具输出，事实编号：青鸟-37"
    codec = (
        f"cp{ctypes.windll.kernel32.GetOEMCP()}"
        if encoding == "native" and os.name == "nt"
        else "utf-8"
    )
    payload = list(expected.encode(codec))
    command = f'"{sys.executable}" -c "import sys; sys.stdout.buffer.write(bytes({payload}))"'
    result = await BashTool(tmp_path).execute(ToolCall("encoding", "bash", {"command": command}))
    assert result.success and expected in result.output
