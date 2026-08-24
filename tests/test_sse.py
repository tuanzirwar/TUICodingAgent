from __future__ import annotations

import pytest

from tuicodingagent.providers.sse import SSEEvent, iter_sse


async def _lines(values: list[str]):
    for value in values:
        yield value


@pytest.mark.asyncio
async def test_sse_parses_comments_multiline_and_final_event() -> None:
    lines = [
        ": keep-alive",
        "event: delta",
        "data: first",
        "data: second",
        "",
        "data: final",
    ]
    events = [event async for event in iter_sse(_lines(lines))]
    assert events == [SSEEvent("first\nsecond", "delta"), SSEEvent("final")]
