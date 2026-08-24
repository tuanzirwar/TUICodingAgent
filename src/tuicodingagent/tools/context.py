"""工具调用的显式工作目录上下文。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_CWD: ContextVar[Path | None] = ContextVar("tuicodingagent_tool_cwd", default=None)


@contextmanager
def with_cwd(directory: str | Path) -> Iterator[None]:
    path = Path(directory).expanduser().resolve()
    token = _CWD.set(path)
    try:
        yield
    finally:
        _CWD.reset(token)


def cwd_from_ctx() -> Path | None:
    return _CWD.get()


def resolve_path(value: str, fallback: Path | None = None) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    base = cwd_from_ctx() or fallback or Path.cwd()
    return (base / path).resolve()


__all__ = ["cwd_from_ctx", "resolve_path", "with_cwd"]
