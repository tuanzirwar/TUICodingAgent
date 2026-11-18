"""以真实磁盘计数验证调用重放与合法重复的边界。"""

from pathlib import Path

import pytest

from tuicodingagent.eval.harness_side_effects import side_effect_case


@pytest.mark.parametrize("index", range(8))
async def test_side_effect_replay_contract(tmp_path: Path, index: int) -> None:
    assert (await side_effect_case(tmp_path, index))["passed"]
