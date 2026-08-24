"""可选持久化回调的对话消息容器。"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from .models import ChatMessage, ToolCall, ToolResult

AppendCallback = Callable[[ChatMessage], None]
ReplaceCallback = Callable[[list[ChatMessage]], None]


class Conversation:
    """集中提供消息追加/替换语义，未传回调时保持纯内存行为。"""

    def __init__(
        self,
        messages: Iterable[ChatMessage] = (),
        *,
        on_append: AppendCallback | None = None,
        on_replace: ReplaceCallback | None = None,
    ) -> None:
        self._messages = list(messages)
        self.on_append = on_append
        self.on_replace = on_replace

    @property
    def messages(self) -> tuple[ChatMessage, ...]:
        return tuple(self._messages)

    def add_user(self, content: str) -> ChatMessage:
        return self._append(ChatMessage("user", content))

    def add_assistant(self, content: str) -> ChatMessage:
        return self._append(ChatMessage("assistant", content))

    def add_assistant_with_tool_calls(
        self, content: str, tool_calls: Iterable[ToolCall]
    ) -> ChatMessage:
        return self._append(ChatMessage("assistant", content, tool_calls=tuple(tool_calls)))

    def add_tool_results(self, tool_results: Iterable[ToolResult]) -> ChatMessage:
        return self._append(ChatMessage("tool", tool_results=tuple(tool_results)))

    def replace_messages(self, messages: Iterable[ChatMessage]) -> None:
        self._messages = list(messages)
        if self.on_replace is not None:
            self.on_replace(list(self._messages))

    def _append(self, message: ChatMessage) -> ChatMessage:
        self._messages.append(message)
        if self.on_append is not None:
            self.on_append(message)
        return message


__all__ = ["AppendCallback", "Conversation", "ReplaceCallback"]
