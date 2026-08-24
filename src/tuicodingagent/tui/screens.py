"""启动阶段使用的 TUI Screen。"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from ..models import PermissionChoice, ProviderConfig, ToolCall
from ..session import SessionInfo


class ProviderSelectScreen(ModalScreen[ProviderConfig]):
    """用方向键选择本次会话的活动 provider。"""

    def __init__(self, providers: tuple[ProviderConfig, ...]) -> None:
        super().__init__()
        self.providers = providers

    def compose(self) -> ComposeResult:
        options = [
            Option(f"{item.name}  ·  {item.model}", id=str(index))
            for index, item in enumerate(self.providers)
        ]
        with Center(), Vertical(id="provider-dialog"):
            yield Label("选择 Provider", id="provider-title")
            yield Static("↑↓ 移动，Enter 确认", id="provider-help")
            yield OptionList(*options, id="provider-options")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        index = int(event.option.id or "0")
        self.dismiss(self.providers[index])


class PermissionScreen(ModalScreen[PermissionChoice]):
    """暂停 Agent Loop 的人在回路审批弹窗。"""

    BINDINGS = [
        Binding("1", "allow_once", "允许本次", show=False, priority=True),
        Binding("2", "allow_always", "永久允许", show=False, priority=True),
        Binding("3", "deny_once", "拒绝本次", show=False, priority=True),
        Binding("escape", "cancel", "取消任务", show=False, priority=True),
    ]

    def __init__(self, call: ToolCall, reason: str) -> None:
        super().__init__()
        self.call = call
        self.reason = reason

    def compose(self) -> ComposeResult:
        preview = "\n".join(
            f"{key}: {_preview(value)}" for key, value in self.call.arguments.items()
        )
        with Center(), Vertical(id="permission-dialog"):
            yield Label(f"批准工具调用 · {self.call.name}", id="permission-title")
            yield Static(preview or "（无参数）", id="permission-arguments")
            yield Static(self.reason, id="permission-reason")
            yield Static("↑↓ + Enter，或按 1 / 2 / 3", id="permission-help")
            yield OptionList(
                Option("1  允许本次", id=PermissionChoice.ALLOW_ONCE.value),
                Option("2  永久允许", id=PermissionChoice.ALLOW_ALWAYS.value),
                Option("3  拒绝本次", id=PermissionChoice.DENY_ONCE.value),
                id="permission-options",
            )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(PermissionChoice(event.option.id or PermissionChoice.DENY_ONCE))

    def action_allow_once(self) -> None:
        self.dismiss(PermissionChoice.ALLOW_ONCE)

    def action_allow_always(self) -> None:
        self.dismiss(PermissionChoice.ALLOW_ALWAYS)

    def action_deny_once(self) -> None:
        self.dismiss(PermissionChoice.DENY_ONCE)

    def action_cancel(self) -> None:
        self.dismiss(PermissionChoice.CANCEL)


class ResumeScreen(ModalScreen[SessionInfo | None]):
    """支持搜索过滤的历史会话选择框。"""

    BINDINGS = [
        Binding("up", "previous", "上一个", show=False, priority=True),
        Binding("down", "next", "下一个", show=False, priority=True),
        Binding("enter", "select", "恢复", show=False, priority=True),
        Binding("escape", "cancel", "取消", show=False, priority=True),
    ]

    def __init__(self, sessions: list[SessionInfo]) -> None:
        super().__init__()
        self.sessions = sessions
        self._visible = sessions

    def compose(self) -> ComposeResult:
        with Center(), Vertical(id="resume-dialog"):
            yield Label("恢复历史会话", id="resume-title")
            yield Static("输入标题关键词过滤；↑↓ 选择，Enter 恢复，Esc 取消", id="resume-help")
            yield Input(placeholder="搜索会话...", id="resume-search")
            yield OptionList(*self._options(self.sessions), id="resume-options")

    def on_mount(self) -> None:
        self.query_one("#resume-search", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        keyword = event.value.casefold().strip()
        self._visible = [item for item in self.sessions if keyword in item.title.casefold()]
        options = self.query_one("#resume-options", OptionList)
        options.clear_options()
        options.add_options(self._options(self._visible))
        if self._visible:
            options.highlighted = 0

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        identifier = str(event.option.id or "")
        selected = next(
            (item for item in self._visible if item.context.session_id == identifier), None
        )
        if selected is not None:
            self.dismiss(selected)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_previous(self) -> None:
        self.query_one("#resume-options", OptionList).action_cursor_up()

    def action_next(self) -> None:
        self.query_one("#resume-options", OptionList).action_cursor_down()

    def action_select(self) -> None:
        options = self.query_one("#resume-options", OptionList)
        if options.highlighted is None or not self._visible:
            return
        selected = self._visible[options.highlighted]
        self.dismiss(selected)

    @staticmethod
    def _options(sessions: list[SessionInfo]) -> list[Option]:
        return [
            Option(
                f"{item.title}  ·  {_relative_time(item.modified_at)}  ·  "
                f"{item.model}  ·  {_file_size(item.size)}",
                id=item.context.session_id,
            )
            for item in sessions
        ]


def _preview(value: object) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text if len(text) <= 240 else f"{text[:240]}…"


def _relative_time(timestamp: float) -> str:
    import time

    seconds = max(0, int(time.time() - timestamp))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} minutes ago"
    if seconds < 86400:
        return f"{seconds // 3600} hours ago"
    return f"{seconds // 86400} days ago"


def _file_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"
