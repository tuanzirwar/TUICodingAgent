"""与模型协议无关的 Server-Sent Events 解析。"""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SSEEvent:
    data: str
    event: str | None = None


async def iter_sse(lines: AsyncIterable[str]) -> AsyncIterator[SSEEvent]:
    """从异步文本行流解析 SSE；支持多行 data 与末尾无空行的事件。"""

    event_name: str | None = None
    data_lines: list[str] = []
    async for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if not line:
            if data_lines:
                yield SSEEvent(data="\n".join(data_lines), event=event_name)
            event_name = None
            data_lines.clear()
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)
    if data_lines:
        yield SSEEvent(data="\n".join(data_lines), event=event_name)
