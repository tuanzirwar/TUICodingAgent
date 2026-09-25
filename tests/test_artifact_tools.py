from pathlib import Path

from tuicodingagent.models import ToolCall
from tuicodingagent.tools.artifacts import ReadArtifactTool, SearchArtifactTool


async def test_read_and_search_managed_artifact(tmp_path: Path) -> None:
    path = tmp_path / ".tuicodingagent" / "sessions" / "one" / "tool-results" / "call-1"
    path.parent.mkdir(parents=True)
    path.write_text("第一行\n关键错误 E_CONN_RESET\n第三行", encoding="utf-8")

    read = await ReadArtifactTool(tmp_path).execute(
        ToolCall("read", "read_artifact", {"artifact": "call-1", "offset": 2, "limit": 1})
    )
    assert read.success and "E_CONN_RESET" in read.output and read.truncated

    search = await SearchArtifactTool(tmp_path).execute(
        ToolCall("search", "search_artifact", {"query": "e_conn_reset"})
    )
    assert search.success and "call-1:2:" in search.output


async def test_read_artifact_rejects_unmanaged_path(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    result = await ReadArtifactTool(tmp_path).execute(
        ToolCall("read", "read_artifact", {"artifact": str(outside)})
    )
    assert not result.success and "不在受管目录" in result.error
