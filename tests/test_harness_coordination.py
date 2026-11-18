"""Lead 交接、共享任务 CAS 与真实合并冲突的合同。"""

from pathlib import Path

import pytest

from tuicodingagent.eval.harness_coordination import coordination_case


@pytest.mark.parametrize("index", range(8))
async def test_coordination_regression(tmp_path: Path, index: int) -> None:
    assert (await coordination_case(tmp_path, index))["passed"]
