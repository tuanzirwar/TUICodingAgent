"""按当前请求选择少量工具 Schema 的本地目录。"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from ..models import ToolDefinition

_WORD = re.compile(r"[\w.-]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class ToolMatch:
    definition: ToolDefinition
    score: float


class LazyToolCatalog:
    """保存轻量索引，并在需要时返回完整 Schema。"""

    def __init__(self, definitions: Iterable[ToolDefinition]) -> None:
        self._definitions = tuple(definitions)
        self._terms = {
            item.name: self._tokenize(f"{item.name} {item.description}")
            for item in self._definitions
        }

    def select(self, query: str, *, limit: int = 5) -> tuple[ToolDefinition, ...]:
        if limit < 1:
            raise ValueError("limit 必须为正数")
        query_terms = self._tokenize(query)
        matches = []
        folded = query.casefold()
        for index, item in enumerate(self._definitions):
            overlap = len(query_terms & self._terms[item.name])
            exact = 1000 if item.name.casefold() in folded else 0
            matches.append((-(exact + overlap), index, item))
        matches.sort()
        return tuple(item for _, _, item in matches[:limit])

    def search(self, query: str, *, limit: int = 5) -> tuple[ToolMatch, ...]:
        selected = self.select(query, limit=limit)
        terms = self._tokenize(query)
        return tuple(
            ToolMatch(item, float(len(terms & self._terms[item.name]))) for item in selected
        )

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        return {match.group(0).casefold() for match in _WORD.finditer(text)}


__all__ = ["LazyToolCatalog", "ToolMatch"]
