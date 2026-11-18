"""取消后检查真实后代进程的延迟副作用。"""

from pathlib import Path

import pytest

from tuicodingagent.eval.harness_process_tree import process_case


@pytest.mark.parametrize("index", range(10))
async def test_process_tree_cleanup(tmp_path: Path, index: int) -> None:
    assert (await process_case(tmp_path, index))["passed"]
