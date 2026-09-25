from pathlib import Path

from tuicodingagent.verification import VerificationRunner


async def test_verification_skips_non_git_workspace(tmp_path: Path) -> None:
    path = tmp_path / "a.py"
    path.write_text("broken =", encoding="utf-8")
    result = await VerificationRunner(tmp_path).run({path})
    assert result.success and result.steps == 0


async def test_verification_detects_python_syntax_error(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    path = tmp_path / "broken.py"
    path.write_text("broken =", encoding="utf-8")
    runner = VerificationRunner(tmp_path, step_timeout=10, total_timeout=20)
    result = await runner.run({path})
    assert not result.success and "[syntax] exit=" in result.output
