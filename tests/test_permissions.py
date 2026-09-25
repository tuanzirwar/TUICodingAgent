from __future__ import annotations

import os
from pathlib import Path

import pytest

from tuicodingagent.models import (
    PermissionDecision,
    PermissionMode,
    ToolCall,
)
from tuicodingagent.permissions import (
    MODE_FALLBACK,
    PermissionManager,
    call_target,
    decide_rules,
    load_merged_settings,
    parse_rule,
)
from tuicodingagent.permissions.blacklist import blacklist_denial
from tuicodingagent.permissions.config import persist_local_allow
from tuicodingagent.permissions.models import ToolCategory
from tuicodingagent.permissions.sandbox import sandbox_denial


def _call(name: str, **arguments: object) -> ToolCall:
    return ToolCall("id", name, arguments)


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "rm -fr ~",
        "mkfs.ext4 /dev/sda1",
        "dd if=/dev/zero of=/dev/sda",
        ":(){ :|:& };:",
    ],
)
def test_dangerous_command_blacklist_catches_variants(command: str) -> None:
    assert blacklist_denial(command)


def test_blacklist_and_sandbox_cannot_be_bypassed(tmp_path: Path) -> None:
    manager = PermissionManager(tmp_path, mode=PermissionMode.BYPASS)
    dangerous = manager.evaluate(_call("bash", command="rm -rf /"))
    outside = manager.evaluate(_call("read_file", path="../outside.txt"))
    inside_new = manager.evaluate(_call("write_file", path="new/deep/file.txt"))
    assert dangerous.decision == PermissionDecision.DENY
    assert outside.decision == PermissionDecision.DENY
    assert inside_new.decision == PermissionDecision.ALLOW


def test_sandbox_resolves_symlink_before_boundary_check(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    link = tmp_path / "escape"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError:
        pytest.skip("当前 Windows 环境不允许创建符号链接")
    assert sandbox_denial(_call("write_file", path="escape/file.txt"), tmp_path)


def test_rule_exact_glob_friendly_routes_and_global_priority(tmp_path: Path) -> None:
    allow_exact = parse_rule("Bash(git status)", PermissionDecision.ALLOW, "user")
    allow_git = parse_rule("Bash(git *)", PermissionDecision.ALLOW, "project")
    deny_push = parse_rule("Bash(git push)", PermissionDecision.DENY, "local")
    ask_git = parse_rule("Bash(git *)", PermissionDecision.ASK, "user")
    write_src = parse_rule("Write(src/**)", PermissionDecision.ALLOW)

    status = _call("bash", command="git status")
    push = _call("bash", command="git push")
    assert decide_rules(status, "git status", (allow_exact,)) == PermissionDecision.ALLOW
    assert decide_rules(push, "git push", (allow_exact,)) is None
    assert decide_rules(push, "git push", (allow_git, deny_push)) == PermissionDecision.DENY
    assert decide_rules(status, "git status", (allow_git, ask_git)) == PermissionDecision.ASK
    assert decide_rules(_call("write_file"), "src/a/b.py", (write_src,)) == PermissionDecision.ALLOW
    assert decide_rules(_call("write_file"), "docs/x.md", (write_src,)) is None

    for friendly, internal in (
        ("Read", "read_file"),
        ("Write", "write_file"),
        ("Edit", "edit_file"),
        ("Glob", "glob"),
        ("Grep", "grep"),
        ("ReadArtifact", "read_artifact"),
        ("SearchArtifact", "search_artifact"),
    ):
        rule = parse_rule(friendly, PermissionDecision.ALLOW)
        assert decide_rules(
            _call(internal), call_target(_call(internal, path="."), tmp_path), (rule,)
        )


def test_mode_fallback_matrix_and_safe_unknowns(tmp_path: Path) -> None:
    expected = {
        PermissionMode.DEFAULT: ("allow", "ask", "ask"),
        PermissionMode.ACCEPT_EDITS: ("allow", "allow", "ask"),
        PermissionMode.PLAN: ("allow", "ask", "ask"),
        PermissionMode.BYPASS: ("allow", "allow", "allow"),
    }
    categories = (ToolCategory.READ_ONLY, ToolCategory.FILE_WRITE, ToolCategory.COMMAND)
    for mode, values in expected.items():
        assert tuple(MODE_FALLBACK[mode][item].value for item in categories) == values

    manager = PermissionManager(tmp_path, mode=PermissionMode.BYPASS)
    assert manager.evaluate(_call("unknown")).decision == PermissionDecision.DENY
    broken = ToolCall("bad", "bash", {}, parse_error="bad json")
    assert manager.evaluate(broken).decision == PermissionDecision.DENY


def test_three_level_config_degrades_and_default_mode_uses_nearest(tmp_path: Path) -> None:
    user = tmp_path / "user.yaml"
    project = tmp_path / "project.yaml"
    local = tmp_path / "local.yaml"
    user.write_text(
        "defaultMode: acceptEdits\npermissions:\n  allow: ['Bash(git *)']\n",
        encoding="utf-8",
    )
    project.write_text("not: [valid", encoding="utf-8")
    local.write_text(
        "defaultMode: plan\npermissions:\n  deny: ['Bash(git push)']\n",
        encoding="utf-8",
    )
    settings = load_merged_settings(tmp_path, (user, project, local))
    assert settings.default_mode == PermissionMode.PLAN
    assert len(settings.rules) == 2
    call = _call("bash", command="git push")
    assert decide_rules(call, "git push", settings.rules) == PermissionDecision.DENY

    missing = load_merged_settings(tmp_path, tuple(tmp_path / f"none-{n}" for n in range(3)))
    assert missing.rules == () and missing.default_mode is None


def test_persist_local_allow_is_exact_deduplicated_and_reloadable(tmp_path: Path) -> None:
    local = tmp_path / "settings.local.yaml"
    persist_local_allow(local, "Bash(git status)")
    persist_local_allow(local, "Bash(git status)")
    settings = load_merged_settings(tmp_path, (tmp_path / "u", tmp_path / "p", local))
    assert len(settings.rules) == 1
    assert settings.rules[0].pattern is not None
    assert settings.rules[0].pattern.value == "git status"


def test_allow_always_degrades_to_allow_once_when_persistence_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_persistence(_path: Path, _rule: str) -> None:
        raise PermissionError("只读文件系统")

    monkeypatch.setattr("tuicodingagent.permissions.manager.persist_local_allow", fail_persistence)
    manager = PermissionManager(tmp_path)
    assert manager.allow_always("Bash(git status)") is False
    assert manager.rules == ()
