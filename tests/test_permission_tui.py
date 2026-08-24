from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest
from textual.widgets import OptionList, Static

from tuicodingagent.models import (
    AppConfig,
    ChatMessage,
    PermissionMode,
    ProviderConfig,
    ProviderEvent,
    ProviderEventType,
    SessionState,
    ToolCall,
    ToolDefinition,
)
from tuicodingagent.permissions import PermissionManager
from tuicodingagent.tui.app import TUICodingAgentApp
from tuicodingagent.tui.screens import PermissionScreen
from tuicodingagent.tui.widgets import PromptInput


class ApprovalProvider:
    def __init__(self, config: ProviderConfig, replies: list[str] | None = None) -> None:
        self.config = config
        self.calls = 0
        self.replies = replies or []

    async def stream(
        self,
        system_prompt: object,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        self.calls += 1
        if self.calls == 1:
            yield ProviderEvent(
                ProviderEventType.TOOL_CALL,
                tool_call=ToolCall(
                    "write-approval",
                    "write_file",
                    {"path": "approved.txt", "content": "已批准"},
                ),
            )
        else:
            yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="任务继续完成")

    async def close(self) -> None:
        return None


class TextProvider(ApprovalProvider):
    async def stream(
        self,
        system_prompt: object,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="完成")


def _config() -> AppConfig:
    return AppConfig((ProviderConfig("Fake", "openai", "model", "secret"),))


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["1", "2"])
async def test_permission_dialog_allows_once_or_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    local = tmp_path / ".tuicodingagent" / "settings.local.yaml"
    manager = PermissionManager(tmp_path, local_path=local)
    monkeypatch.setattr(PermissionManager, "load", classmethod(lambda cls, root: manager))
    provider = ApprovalProvider(_config().providers[0])
    app = TUICodingAgentApp(_config(), workspace=tmp_path, provider_factory=lambda config: provider)
    async with app.run_test(size=(90, 28)) as pilot:
        prompt = app.query_one(PromptInput)
        prompt.focus()
        await pilot.press("w", "r", "i", "t", "e", "enter")
        await pilot.pause(0.2)
        assert isinstance(app.screen, PermissionScreen)
        assert len(app.screen.query(OptionList)) == 1
        assert "write_file" in str(app.screen.query_one("#permission-title", Static).render())
        await pilot.press(key)
        await pilot.pause(0.3)
        assert (tmp_path / "approved.txt").read_text(encoding="utf-8") == "已批准"
        assert app.session_state == SessionState.IDLE
    assert local.exists() is (key == "2")


@pytest.mark.asyncio
async def test_permission_dialog_rejects_then_model_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = PermissionManager(tmp_path, local_path=tmp_path / "local.yaml")
    monkeypatch.setattr(PermissionManager, "load", classmethod(lambda cls, root: manager))
    app = TUICodingAgentApp(
        _config(),
        workspace=tmp_path,
        provider_factory=lambda config: ApprovalProvider(config),
    )
    async with app.run_test(size=(90, 28)) as pilot:
        prompt = app.query_one(PromptInput)
        prompt.focus()
        await pilot.press("x", "enter")
        await pilot.pause(0.2)
        await pilot.press("3")
        await pilot.pause(0.3)
        assert not (tmp_path / "approved.txt").exists()
        assert app.session_state == SessionState.IDLE
        rendered = " ".join(str(widget.render()) for widget in app.query(Static))
        assert "任务继续完成" in rendered


@pytest.mark.asyncio
async def test_shift_tab_cycles_modes_and_plan_do_are_fixed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = PermissionManager(tmp_path)
    monkeypatch.setattr(PermissionManager, "load", classmethod(lambda cls, root: manager))
    app = TUICodingAgentApp(
        _config(),
        workspace=tmp_path,
        provider_factory=lambda config: TextProvider(config),
    )
    async with app.run_test(size=(90, 28)) as pilot:
        for expected in (
            PermissionMode.ACCEPT_EDITS,
            PermissionMode.PLAN,
            PermissionMode.BYPASS,
            PermissionMode.DEFAULT,
        ):
            await pilot.press("shift+tab")
            await pilot.pause()
            assert app.runtime and app.runtime.permission_mode == expected
            assert expected.value in str(app.query_one("#status-bar", Static).render())

        await pilot.press("/", "p", "l", "a", "n", "enter")
        await pilot.pause()
        assert app.runtime and app.runtime.permission_mode == PermissionMode.PLAN
        await pilot.press("/", "d", "o", "enter")
        await pilot.pause(0.2)
        assert app.runtime and app.runtime.permission_mode == PermissionMode.DEFAULT


@pytest.mark.asyncio
async def test_permission_wait_can_cancel_and_next_turn_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = PermissionManager(tmp_path)
    monkeypatch.setattr(PermissionManager, "load", classmethod(lambda cls, root: manager))
    provider = ApprovalProvider(_config().providers[0])
    app = TUICodingAgentApp(_config(), workspace=tmp_path, provider_factory=lambda config: provider)
    async with app.run_test(size=(90, 28)) as pilot:
        prompt = app.query_one(PromptInput)
        prompt.focus()
        await pilot.press("x", "enter")
        await pilot.pause(0.2)
        assert isinstance(app.screen, PermissionScreen)
        await pilot.press("escape")
        await pilot.pause(0.3)
        assert app.session_state == SessionState.IDLE and not (tmp_path / "approved.txt").exists()
        assert app.runtime and [message.role for message in app.runtime.history] == [
            "user",
            "assistant",
            "tool",
            "assistant",
        ]
        await pilot.press("y", "enter")
        await pilot.pause(0.3)
        assert app.session_state == SessionState.IDLE and provider.calls == 2
