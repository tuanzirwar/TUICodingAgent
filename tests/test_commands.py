from __future__ import annotations

from pathlib import Path

import pytest

from tuicodingagent.commands import (
    BUILTIN_REGISTRY,
    Command,
    CommandKind,
    CommandRegistry,
)
from tuicodingagent.models import PermissionMode


async def _noop(_ui) -> None:
    return None


class FakeUI:
    def __init__(self) -> None:
        self.idle = True
        self.messages: list[str] = []
        self.prompts: list[tuple[str, str]] = []
        self.mode = PermissionMode.DEFAULT
        self.actions: list[str] = []

    def is_idle(self) -> bool:
        return self.idle

    async def show_message(self, text: str) -> None:
        self.messages.append(text)

    async def send_user_message(self, text: str, display_text: str) -> None:
        self.prompts.append((text, display_text))

    async def set_mode(self, mode: PermissionMode) -> None:
        self.mode = mode

    async def trigger_compact(self) -> None:
        self.actions.append("compact")

    async def open_resume(self) -> None:
        self.actions.append("resume")

    async def clear_and_new_session(self) -> None:
        self.actions.append("clear")

    def request_exit(self) -> None:
        self.actions.append("exit")

    def permission_mode_name(self) -> str:
        return self.mode.value

    def usage(self) -> tuple[int, int]:
        return 12, 34

    def tool_count(self) -> int:
        return 6

    def memory_files(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        return ("project.md",), ("user.md",)

    def model_name(self) -> str:
        return "model-x"

    def workspace_path(self) -> Path:
        return Path("/workspace")

    def session_identity(self) -> tuple[str, Path]:
        return "session-x", Path("/workspace/conversation.jsonl")

    def hooks_summary(self) -> str:
        return "No hooks loaded."


def test_registry_detects_name_and_alias_conflicts() -> None:
    first = Command("/one", ("/o",), "one", "/one", CommandKind.LOCAL, _noop)
    registry = CommandRegistry((first,))
    with pytest.raises(ValueError, match="/one"):
        registry.register(Command("/ONE", (), "dup", "/one", CommandKind.LOCAL, _noop))
    with pytest.raises(ValueError, match="/o"):
        registry.register(Command("/two", ("/O",), "dup", "/two", CommandKind.LOCAL, _noop))


def test_builtin_help_completion_and_hidden_are_registry_driven() -> None:
    assert len(BUILTIN_REGISTRY) == 14
    names = [command.name for command in BUILTIN_REGISTRY.visible()]
    assert names == sorted(names)
    assert len(BUILTIN_REGISTRY.help_text().splitlines()) == 14
    assert [command.name for command in BUILTIN_REGISTRY.complete("/s")] == [
        "/session",
        "/status",
    ]
    hidden = Command("/secret", (), "hidden", "/secret", CommandKind.LOCAL, _noop, hidden=True)
    registry = CommandRegistry((hidden,))
    assert "/secret" in registry and registry.visible() == () and registry.complete("/") == ()


@pytest.mark.asyncio
async def test_dispatch_case_unknown_arguments_and_idle_gate() -> None:
    ui = FakeUI()
    assert not await BUILTIN_REGISTRY.dispatch("普通消息", ui)
    assert await BUILTIN_REGISTRY.dispatch("/Help", ui)
    assert len(ui.messages[-1].splitlines()) == 14
    await BUILTIN_REGISTRY.dispatch("/foobar", ui)
    assert "/help" in ui.messages[-1]
    await BUILTIN_REGISTRY.dispatch("/status extra", ui)
    assert "未知命令" in ui.messages[-1]

    ui.idle = False
    await BUILTIN_REGISTRY.dispatch("/review", ui)
    assert ui.messages[-1] == "请等待当前任务完成" and not ui.prompts
    await BUILTIN_REGISTRY.dispatch("/permission", ui)
    assert ui.messages[-1] == "default"


@pytest.mark.asyncio
async def test_local_outputs_and_prompt_commands() -> None:
    ui = FakeUI()
    for command in ("/status", "/memory", "/permission", "/session"):
        await BUILTIN_REGISTRY.dispatch(command, ui)
    status = ui.messages[0].splitlines()
    assert len(status) == 6
    assert [line.split(":", 1)[0].strip() for line in status] == [
        "权限模式",
        "累计 Token",
        "可用工具",
        "记忆条目",
        "当前模型",
        "当前工作目录",
    ]
    assert "project.md" in ui.messages[1] and "user.md" in ui.messages[1]
    assert "session-x" in ui.messages[3]
    await BUILTIN_REGISTRY.dispatch("/hooks", ui)
    assert ui.messages[-1] == "No hooks loaded."

    await BUILTIN_REGISTRY.dispatch("/do", ui)
    await BUILTIN_REGISTRY.dispatch("/review", ui)
    assert ui.mode == PermissionMode.DEFAULT
    assert "计划" in ui.prompts[0][0] and "审查" in ui.prompts[1][0]
