from __future__ import annotations

from pathlib import Path

from tuicodingagent.cli import main


def test_missing_config_returns_safe_error(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    assert main([]) == 2
    captured = capsys.readouterr()
    assert "配置错误" in captured.err
    assert "Traceback" not in captured.err
