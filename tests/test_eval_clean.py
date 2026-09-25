from __future__ import annotations

import subprocess
from pathlib import Path

from tuicodingagent.eval.runner import _git_is_clean


def test_git_clean_guard_detects_untracked_artifact(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert _git_is_clean(tmp_path)

    (tmp_path / "artifact.pyc").write_bytes(b"cache")

    assert not _git_is_clean(tmp_path)
