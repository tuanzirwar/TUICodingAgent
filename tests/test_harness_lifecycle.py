"""副作用前租约校验与真实验证子进程清理。"""

from pathlib import Path

import pytest

from tuicodingagent.eval.harness_lifecycle import lease_case, verification_case


@pytest.mark.parametrize("index", range(3))
async def test_lease_side_effect_boundary(tmp_path: Path, index: int) -> None:
    assert (await lease_case(tmp_path, index))["passed"]


@pytest.mark.parametrize("total_timeout", [False, True])
async def test_verification_child_cleanup(tmp_path: Path, total_timeout: bool) -> None:
    assert (await verification_case(tmp_path, total_timeout=total_timeout))["passed"]
