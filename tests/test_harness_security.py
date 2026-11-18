"""最坏模型行为下的权限执行合同。"""

from pathlib import Path

import pytest

from tuicodingagent.eval.harness_security import injection_case


@pytest.mark.parametrize("index", range(6))
async def test_injected_text_cannot_override_permission_denial(tmp_path: Path, index: int) -> None:
    assert (await injection_case(tmp_path, index))["passed"]
