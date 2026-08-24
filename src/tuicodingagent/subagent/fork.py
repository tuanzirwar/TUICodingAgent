"""Fork 上下文复制与嵌套标记。"""

from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

from ..models import ChatMessage, ToolResult

FORK_BOILERPLATE = """<fork_boilerplate>
你是从主对话 Fork 出来的后台子 Agent。
不能再启动 Agent；不要对话、提问或请求确认，直接使用工具。
严格限制在分配的任务范围内。最终报告必须以 `Scope:` 开头，且不超过 500 字。
</fork_boilerplate>"""


def build_forked_messages(
    parent_messages: tuple[ChatMessage, ...], task: str
) -> tuple[ChatMessage, ...]:
    """深拷贝父历史、补齐悬空工具结果并追加 Fork 任务。"""

    messages = list(deepcopy(parent_messages))
    pending: dict[str, str] = {}
    for message in messages:
        for call in message.tool_calls:
            pending[call.id] = call.name
        for result in message.tool_results:
            pending.pop(result.tool_call_id, None)
    if pending:
        messages.append(
            ChatMessage(
                "tool",
                tool_results=tuple(
                    ToolResult(
                        identifier,
                        name,
                        False,
                        error="Fork 时父调用尚未完成，已使用占位结果闭合历史",
                        metadata={"fork_placeholder": True},
                    )
                    for identifier, name in pending.items()
                ),
            )
        )
    messages.append(ChatMessage("user", f"{FORK_BOILERPLATE}\n\n任务：{task}"))
    return tuple(messages)


def contains_fork_marker(messages: tuple[ChatMessage, ...]) -> bool:
    return any("<fork_boilerplate>" in message.content for message in messages)


def placeholder_call_id() -> str:
    return f"fork-{uuid4().hex[:12]}"


__all__ = ["FORK_BOILERPLATE", "build_forked_messages", "contains_fork_marker"]
