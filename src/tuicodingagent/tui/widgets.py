"""TUICodingAgent 的可复用 TUI 组件。"""

from __future__ import annotations

from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Markdown, Static, TextArea

from ..models import ToolCall, ToolResult


class PromptInput(TextArea):
    """Enter 提交、Alt+Enter 换行的多行输入框。"""

    BINDINGS = [
        Binding("enter", "submit", "发送", show=False, priority=True),
        Binding("alt+enter", "newline", "换行", show=False, priority=True),
        Binding("up", "completion_up", "上一命令", show=False, priority=True),
        Binding("down", "completion_down", "下一命令", show=False, priority=True),
        Binding("tab", "completion_tab", "补全", show=False, priority=True),
        Binding("escape", "completion_escape", "关闭补全", show=False, priority=True),
    ]

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class CompletionKey(Message):
        def __init__(self, key: str) -> None:
            super().__init__()
            self.key = key

    completion_active = False

    def action_submit(self) -> None:
        if self.disabled:
            return
        if self.completion_active:
            self.post_message(self.CompletionKey("enter"))
            return
        text = self.text
        if text.strip():
            self.post_message(self.Submitted(text))

    def action_newline(self) -> None:
        self.insert("\n")

    def action_completion_up(self) -> None:
        if self.completion_active:
            self.post_message(self.CompletionKey("up"))
        else:
            super().action_cursor_up()

    def action_completion_down(self) -> None:
        if self.completion_active:
            self.post_message(self.CompletionKey("down"))
        else:
            super().action_cursor_down()

    def action_completion_tab(self) -> None:
        if self.completion_active:
            self.post_message(self.CompletionKey("tab"))
        else:
            self.insert("\t")

    def action_completion_escape(self) -> None:
        if self.completion_active:
            self.post_message(self.CompletionKey("escape"))


class ChatEntry(Vertical):
    """一条可从流式纯文本定型为 Markdown 的消息。"""

    def __init__(self, role: str, text: str = "", *, classes: str = "") -> None:
        super().__init__(classes=f"chat-entry {classes}".strip())
        self.role = role
        self.initial_text = text

    def compose(self):
        yield Static(self.role, classes="message-role")
        yield Static(self.initial_text, classes="message-body")

    def append_text(self, complete_text: str) -> None:
        self.query_one(".message-body", Static).update(complete_text)

    async def finalize_markdown(self, text: str, elapsed: float) -> None:
        body = self.query_one(".message-body", Static)
        await body.remove()
        await self.mount(Markdown(text, classes="message-markdown"))
        await self.mount(Static(f"完成 · {elapsed:.1f}s", classes="message-timing"))


class ErrorEntry(Vertical):
    def __init__(self, text: str, elapsed: float) -> None:
        super().__init__(classes="chat-entry error-entry")
        self.text = text
        self.elapsed = elapsed

    def compose(self):
        yield Static("错误", classes="message-role")
        yield Static(self.text, classes="message-body")
        yield Static(f"失败 · {self.elapsed:.1f}s", classes="message-timing")


_TOOL_LABELS = {
    "read_file": "Read",
    "write_file": "Write",
    "edit_file": "Edit",
    "bash": "Bash",
    "glob": "Glob",
    "grep": "Grep",
}


class ToolEntry(Vertical):
    """Claude Code 风格的工具调用行和结果摘要。"""

    def __init__(self, call: ToolCall) -> None:
        super().__init__(classes="tool-entry running")
        self.call = call

    def compose(self):
        label = _TOOL_LABELS.get(self.call.name, self.call.name)
        yield Static(f"● {label}({_key_argument(self.call)})", classes="tool-title")
        yield Static("执行中…", classes="tool-summary")

    def finish(self, result: ToolResult) -> None:
        self.remove_class("running")
        self.add_class("success" if result.success else "failed")
        summary = result.output if result.success else result.error
        summary = summary.replace("\r", " ").replace("\n", " ")
        if len(summary) > 240:
            summary = f"{summary[:240]}…"
        if result.truncated:
            summary = f"{summary} [已截断]"
        self.query_one(".tool-summary", Static).update(summary or "（无输出）")


def _key_argument(call: ToolCall) -> str:
    for key in ("path", "command", "pattern"):
        value = call.arguments.get(key)
        if isinstance(value, str):
            return value if len(value) <= 80 else f"{value[:80]}…"
    return ""
