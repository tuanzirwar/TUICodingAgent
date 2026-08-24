"""当前 Team 队员身份上下文。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TeammateContext:
    team_name: str
    agent_id: str
    member_name: str


_CURRENT: ContextVar[TeammateContext | None] = ContextVar("team_context", default=None)


def current_teammate() -> TeammateContext | None:
    return _CURRENT.get()


@contextmanager
def with_teammate(context: TeammateContext) -> Iterator[None]:
    token = _CURRENT.set(context)
    try:
        yield
    finally:
        _CURRENT.reset(token)


__all__ = ["TeammateContext", "current_teammate", "with_teammate"]
