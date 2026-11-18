"""实验计量合同：不调用真实 API。"""

import json
from pathlib import Path

import pytest

from tuicodingagent.errors import ProviderError
from tuicodingagent.eval import harness_live
from tuicodingagent.eval.harness_regression import ScriptedProvider


async def test_budget_refusal_never_sends_request(tmp_path: Path) -> None:
    ledger = tmp_path / "usage.json"
    ledger.write_text(json.dumps({"attempted": 1, "maximum": 1}), encoding="utf-8")
    inner = ScriptedProvider()
    provider = harness_live.BudgetProvider(inner, ledger, 1)
    with pytest.raises(ProviderError):
        _ = [event async for event in provider.stream("系统", ())]
    assert provider.exhausted
    assert not inner.requests
    assert json.loads(ledger.read_text(encoding="utf-8"))["attempted"] == 1


async def test_episode_classifies_external_budget_as_exhausted(tmp_path: Path, monkeypatch) -> None:
    ledger = tmp_path / "usage.json"
    ledger.write_text(json.dumps({"attempted": 0, "maximum": 0}), encoding="utf-8")
    inner = ScriptedProvider()
    monkeypatch.setattr(harness_live, "create_provider", lambda config: inner)
    result = await harness_live.episode(tmp_path, inner.config, ledger, 0, "开始任务")
    assert result["budget_exhausted"]
    assert result["error"]
    assert result["trajectory"]
    assert not inner.requests
