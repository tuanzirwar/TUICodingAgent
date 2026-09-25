from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tuicodingagent.models import ToolCall
from tuicodingagent.tools import create_default_registry
from tuicodingagent.tools.files import EditFileTool, ReadFileTool, WriteFileTool
from tuicodingagent.tools.search import GlobTool, GrepTool
from tuicodingagent.tools.shell import BashTool


def call(name: str, **arguments: object) -> ToolCall:
    return ToolCall(f"call-{name}", name, arguments)


def test_default_registry_has_tools_and_both_protocol_schemas(tmp_path: Path) -> None:
    registry = create_default_registry(tmp_path)
    names = [item.name for item in registry.definitions()]
    assert names == [
        "read_file",
        "write_file",
        "edit_file",
        "bash",
        "glob",
        "grep",
        "read_artifact",
        "search_artifact",
    ]
    assert [item["function"]["name"] for item in registry.api_definitions("openai")] == names
    assert [item["name"] for item in registry.api_definitions("anthropic")] == names


@pytest.mark.asyncio
async def test_read_write_and_edit_file_contract(tmp_path: Path) -> None:
    write = WriteFileTool(tmp_path)
    read = ReadFileTool(tmp_path)
    edit = EditFileTool(tmp_path)

    result = await write.execute(call("write_file", path="nested/demo.txt", content="甲\n乙"))
    assert result.success
    assert (tmp_path / "nested/demo.txt").read_text(encoding="utf-8") == "甲\n乙"
    result = await write.execute(call("write_file", path="nested/demo.txt", content="甲\n丙"))
    assert result.success

    result = await read.execute(call("read_file", path="nested/demo.txt"))
    assert result.success and "     1\t甲" in result.output and "     2\t丙" in result.output
    missing = await read.execute(call("read_file", path="missing.txt"))
    assert not missing.success and "读取文件失败" in missing.error

    result = await edit.execute(
        call("edit_file", path="nested/demo.txt", old_text="丙", new_text="丁")
    )
    assert result.success and result.metadata["matches"] == 1
    zero = await edit.execute(
        call("edit_file", path="nested/demo.txt", old_text="不存在", new_text="x")
    )
    assert not zero.success and zero.metadata["matches"] == 0
    (tmp_path / "nested/demo.txt").write_text("同同", encoding="utf-8")
    many = await edit.execute(
        call("edit_file", path="nested/demo.txt", old_text="同", new_text="异")
    )
    assert not many.success and many.metadata["matches"] == 2


@pytest.mark.asyncio
async def test_read_and_search_limits(tmp_path: Path) -> None:
    (tmp_path / "large.txt").write_text(
        "\n".join(f"line-{index}" for index in range(2_010)), encoding="utf-8"
    )
    read = await ReadFileTool(tmp_path).execute(call("read_file", path="large.txt"))
    assert read.success and read.truncated and "已截断 10 行" in read.output

    for index in range(205):
        (tmp_path / f"item-{index:03}.py").write_text("needle\n", encoding="utf-8")
    globbed = await GlobTool(tmp_path).execute(call("glob", pattern="*.py"))
    assert globbed.success and globbed.truncated and globbed.metadata["matches"] == 205
    assert len(globbed.output.splitlines()) == 200

    grepped = await GrepTool(tmp_path).execute(call("grep", pattern="needle", path="."))
    assert grepped.success and grepped.truncated and grepped.metadata["matches"] == 205
    assert len(grepped.output.splitlines()) == 200
    invalid = await GrepTool(tmp_path).execute(call("grep", pattern="[", path="."))
    assert not invalid.success and "正则表达式无效" in invalid.error


@pytest.mark.asyncio
async def test_bash_success_failure_timeout_and_truncation(tmp_path: Path) -> None:
    tool = BashTool(tmp_path)
    python = f'"{sys.executable}"'
    success = await tool.execute(call("bash", command=f'{python} -c "print(123)"'))
    assert success.success and "exit_code: 0" in success.output and "123" in success.output

    failed = await tool.execute(
        call(
            "bash",
            command=f"{python} -c \"import sys; print('bad', file=sys.stderr); sys.exit(3)\"",
        )
    )
    assert not failed.success and failed.metadata["exit_code"] == 3 and "bad" in failed.output

    timeout = await BashTool(tmp_path, timeout=0.05).execute(
        call("bash", command=f'{python} -c "import time; time.sleep(5)"')
    )
    assert not timeout.success and timeout.metadata["timed_out"] is True

    long_output = await tool.execute(call("bash", command=f"{python} -c \"print('x'*100100)\""))
    assert long_output.success and long_output.truncated and "已截断" in long_output.output


@pytest.mark.asyncio
async def test_registry_wraps_bad_calls(tmp_path: Path) -> None:
    registry = create_default_registry(tmp_path)
    unknown = await registry.execute(call("unknown"))
    malformed = await registry.execute(ToolCall("bad", "read_file", {}, "{", "解析失败"))
    missing_arg = await registry.execute(call("read_file"))
    assert not unknown.success and "未知工具" in unknown.error
    assert not malformed.success and "JSON 无效" in malformed.error
    assert not missing_arg.success and "参数 path" in missing_arg.error
