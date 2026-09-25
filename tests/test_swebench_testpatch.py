from __future__ import annotations

import subprocess
from pathlib import Path

from tuicodingagent.eval.swebench_testpatch import _agent_diff, _apply_test_patch


def _git(checkout: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(checkout), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _repo(tmp_path: Path) -> Path:
    checkout = tmp_path / "repo"
    checkout.mkdir()
    _git(checkout, "init", "-q")
    (checkout / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (checkout / "tests").mkdir()
    (checkout / "tests" / "test_app.py").write_text(
        "def test_value():\n    assert True\n", encoding="utf-8"
    )
    _git(checkout, "add", ".")
    _git(
        checkout,
        "-c",
        "user.email=codex@example.com",
        "-c",
        "user.name=Codex",
        "commit",
        "-qm",
        "base",
    )
    return checkout


def test_test_patch_is_applied_and_recorded(tmp_path: Path) -> None:
    checkout = _repo(tmp_path)
    patch = """diff --git a/tests/test_app.py b/tests/test_app.py
index 3d0cf5f..8d1e2dc 100644
--- a/tests/test_app.py
+++ b/tests/test_app.py
@@ -1,2 +1,2 @@
 def test_value():
-    assert True
+    assert 1 == 1
"""

    result = _apply_test_patch(checkout, patch)

    assert result.applied is True
    assert result.returncode == 0
    assert result.files == ("tests/test_app.py",)
    assert "assert 1 == 1" in (checkout / "tests" / "test_app.py").read_text(encoding="utf-8")


def test_invalid_test_patch_fails_closed(tmp_path: Path) -> None:
    result = _apply_test_patch(_repo(tmp_path), "this is not a git patch\n")

    assert result.applied is False
    assert result.returncode != 0


def test_agent_patch_excludes_test_patch_files(tmp_path: Path) -> None:
    checkout = _repo(tmp_path)
    patch = """diff --git a/tests/test_app.py b/tests/test_app.py
--- a/tests/test_app.py
+++ b/tests/test_app.py
@@ -1,2 +1,2 @@
 def test_value():
-    assert True
+    assert 1 == 1
"""
    assert _apply_test_patch(checkout, patch).applied is True
    (checkout / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    (checkout / "tests" / "test_app.py").write_text(
        "def test_value():\n    assert 2 == 2\n", encoding="utf-8"
    )

    diff = _agent_diff(checkout, ("tests/test_app.py",))

    assert "app.py" in diff
    assert "tests/test_app.py" not in diff
