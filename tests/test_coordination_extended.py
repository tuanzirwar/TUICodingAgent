"""UI 消息生命周期与双进程任务领取。"""

from pathlib import Path

import pytest

from tuicodingagent.eval.harness_coordination_extended import process_case, tui_case


async def test_cross_process_claim_is_exclusive(tmp_path: Path) -> None:
    assert (await process_case(tmp_path))["passed"]


@pytest.mark.parametrize("fail", [False, True])
async def test_tui_durable_lead_handoff(tmp_path: Path, fail: bool) -> None:
    assert (await tui_case(tmp_path, fail=fail))["passed"]
