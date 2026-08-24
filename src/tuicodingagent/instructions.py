"""TUICODINGAGENT.md 多层项目指令加载器。"""

from __future__ import annotations

import re
from pathlib import Path

INSTRUCTION_FILE = "TUICODINGAGENT.md"
MAX_INCLUDE_DEPTH = 5
_INCLUDE = re.compile(r"^@include ([^\r\n]+)$")


def load_instructions(workspace: Path, user_home: Path | None = None) -> str:
    """按项目根、项目配置、用户配置的优先级加载一次。"""

    project = workspace.resolve()
    home = (user_home or Path.home()).resolve()
    user_root = home / ".tuicodingagent"
    sources = (
        (project / INSTRUCTION_FILE, project),
        (project / ".tuicodingagent" / INSTRUCTION_FILE, project),
        (user_root / INSTRUCTION_FILE, user_root),
    )
    parts: list[str] = []
    for path, boundary in sources:
        content = _expand(path, boundary.resolve(), 1, set())
        if content:
            parts.append(content.strip())
    return "\n\n".join(parts)


def _expand(path: Path, boundary: Path, depth: int, chain: set[Path]) -> str:
    absolute = path.resolve(strict=False)
    if not _within(absolute, boundary) or not absolute.is_file():
        return ""
    try:
        raw = absolute.read_bytes()
    except OSError:
        return ""
    if b"\x00" in raw[:512]:
        return f"<!-- 二进制指令文件不可读，已跳过: {path} -->"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"<!-- 指令文件不是 UTF-8，已跳过: {path} -->"

    current_chain = {*chain, absolute}
    output: list[str] = []
    for line in text.splitlines(keepends=True):
        match = _INCLUDE.fullmatch(line.rstrip("\r\n"))
        if match is None:
            output.append(line)
            continue
        relative = match.group(1)
        target = (absolute.parent / relative).resolve(strict=False)
        if not _within(target, boundary):
            output.append(f"<!-- @include 路径超出允许范围，已跳过: {relative} -->\n")
        elif depth >= MAX_INCLUDE_DEPTH:
            output.append(line)
            output.append(f"<!-- @include 超过最大嵌套深度，已跳过: {relative} -->\n")
        elif target in current_chain:
            output.append(f"<!-- @include 检测到环路，已跳过: {relative} -->\n")
        elif not target.is_file():
            # 找不到引用文件同样静默跳过。
            continue
        else:
            expanded = _expand(target, boundary, depth + 1, current_chain)
            output.append(expanded)
            if expanded and not expanded.endswith(("\n", "\r")):
                output.append("\n")
    return "".join(output)


def _within(path: Path, boundary: Path) -> bool:
    try:
        path.relative_to(boundary)
    except ValueError:
        return False
    return True


__all__ = ["INSTRUCTION_FILE", "load_instructions"]
