from __future__ import annotations

from pathlib import Path

from tuicodingagent.instructions import load_instructions


def test_three_levels_and_missing_files(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    home = tmp_path / "home"
    (workspace / ".tuicodingagent").mkdir(parents=True)
    (home / ".tuicodingagent").mkdir(parents=True)
    (workspace / "TUICODINGAGENT.md").write_text("项目根", encoding="utf-8")
    (workspace / ".tuicodingagent" / "TUICODINGAGENT.md").write_text("项目配置", encoding="utf-8")
    (home / ".tuicodingagent" / "TUICODINGAGENT.md").write_text("用户配置", encoding="utf-8")

    result = load_instructions(workspace, home)

    assert result == "项目根\n\n项目配置\n\n用户配置"


def test_include_nested_depth_loop_escape_and_inline(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    rules = workspace / "rules"
    rules.mkdir(parents=True)
    (workspace / "TUICODINGAGENT.md").write_text(
        "@include rules/a.md\n段落 @include rules/no.md\n@include ../secret.md\n",
        encoding="utf-8",
    )
    (rules / "a.md").write_text("A\n@include b.md\n", encoding="utf-8")
    (rules / "b.md").write_text("B\n@include a.md\n", encoding="utf-8")

    result = load_instructions(workspace, tmp_path / "home")

    assert "A" in result and "B" in result
    assert "检测到环路" in result
    assert "路径超出允许范围" in result
    assert "段落 @include rules/no.md" in result


def test_include_sixth_level_kept_with_warning(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "TUICODINGAGENT.md").write_text("@include 2.md\n", encoding="utf-8")
    for level in range(2, 6):
        (workspace / f"{level}.md").write_text(
            f"L{level}\n@include {level + 1}.md\n", encoding="utf-8"
        )
    (workspace / "6.md").write_text("不应展开", encoding="utf-8")

    result = load_instructions(workspace, tmp_path / "home")

    assert "L5" in result
    assert "@include 6.md" in result
    assert "超过最大嵌套深度" in result
    assert "不应展开" not in result


def test_binary_include_is_replaced_by_warning(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "TUICODINGAGENT.md").write_text("@include binary.md\n", encoding="utf-8")
    (workspace / "binary.md").write_bytes(b"abc\x00def")
    assert "二进制指令文件不可读" in load_instructions(workspace, tmp_path / "home")
