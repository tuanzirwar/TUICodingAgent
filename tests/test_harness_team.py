"""任务依赖与真实 Runtime 消息处理的恢复合同。"""

from pathlib import Path

import pytest

from tuicodingagent.eval.harness_team import team_case


@pytest.mark.parametrize("index", range(7))
async def test_team_delivery_and_dependencies(tmp_path: Path, index: int) -> None:
    assert (await team_case(tmp_path, index))["passed"]
