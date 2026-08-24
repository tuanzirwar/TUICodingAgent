"""项目单根路径沙箱。"""

from __future__ import annotations

import os
from pathlib import Path

from ..models import ToolCall

FILE_TOOLS = {"read_file", "write_file", "edit_file", "glob", "grep"}
TEMP_ROOTS = (Path("/tmp"), Path("/private/tmp"))


def sandbox_denial(call: ToolCall, workspace: Path) -> str | None:
    if call.name not in FILE_TOOLS:
        return None
    value = call.arguments.get("path", "." if call.name in {"glob", "grep"} else None)
    if not isinstance(value, str) or not value:
        return "路径沙箱拒绝：缺少有效 path 参数"
    try:
        root = workspace.resolve(strict=True)
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = _resolve_through_existing_ancestor(candidate)
        allowed = os.path.commonpath((str(root), str(resolved))) == str(root)
        if not allowed:
            allowed = any(
                temp.exists()
                and os.path.commonpath((str(temp.resolve()), str(resolved))) == str(temp.resolve())
                for temp in TEMP_ROOTS
            )
        if not allowed:
            return f"路径沙箱拒绝：目标位于项目目录之外（{value}）"
    except (OSError, ValueError):
        return f"路径沙箱拒绝：无法安全解析路径（{value}）"
    return None


def _resolve_through_existing_ancestor(path: Path) -> Path:
    missing: list[str] = []
    current = path
    while not current.exists():
        if current.parent == current:
            raise OSError("找不到已存在的祖先目录")
        missing.append(current.name)
        current = current.parent
    resolved = current.resolve(strict=True)
    for name in reversed(missing):
        resolved /= name
    return resolved
