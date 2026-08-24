from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest
from textual.widgets import Markdown, OptionList, Static

from tuicodingagent.errors import AuthenticationError
from tuicodingagent.models import (
    AppConfig,
    ChatMessage,
    ProviderConfig,
    ProviderEvent,
    ProviderEventType,
    SessionState,
    ToolDefinition,
)
from tuicodingagent.providers.base import ChatProvider
from tuicodingagent.tui.app import TUICodingAgentApp
from tuicodingagent.tui.widgets import ChatEntry, PromptInput


class TUIProvider:
    def __init__(self, config: ProviderConfig, *, delay: float = 0.0) -> None:
        self.config = config
        self.delay = delay
        self.closed = False

    async def stream(
        self,
        system_prompt: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        if self.delay:
            await asyncio.sleep(self.delay)
        yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="**完成**")
        yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="\n\n- 项目")

    async def close(self) -> None:
        self.closed = True


class ErrorTUIProvider(TUIProvider):
    async def stream(
        self,
        system_prompt: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        raise AuthenticationError("认证失败，请检查配置")
        yield ""  # pragma: no cover


def _config(count: int = 1) -> AppConfig:
    providers = tuple(
        ProviderConfig(f"Provider {index}", "openai", f"model-{index}", "secret")
        for index in range(1, count + 1)
    )
    return AppConfig(providers)


@pytest.mark.asyncio
async def test_single_provider_layout_and_markdown_reply(tmp_path: Path) -> None:
    made: list[TUIProvider] = []

    def factory(config: ProviderConfig) -> ChatProvider:
        provider = TUIProvider(config)
        made.append(provider)
        return provider

    app = TUICodingAgentApp(_config(), workspace=tmp_path, provider_factory=factory)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert "TUICodingAgent" in str(app.query_one("#banner", Static).render())
        assert str(tmp_path) in str(app.query_one("#banner", Static).render())
        status = str(app.query_one("#status-bar", Static).render())
        assert "default" in status and "Provider 1" not in status
        prompt = app.query_one(PromptInput)
        prompt.focus()
        await pilot.press("h", "i", "enter")
        await pilot.pause(0.2)
        assert app.session_state == SessionState.IDLE
        assert prompt.text == ""
        assert len(app.query(ChatEntry)) == 2
        assert len(app.query(Markdown)) == 1
    assert made[0].closed


@pytest.mark.asyncio
async def test_multi_provider_direction_selection(tmp_path: Path) -> None:
    app = TUICodingAgentApp(
        _config(2), workspace=tmp_path, provider_factory=lambda config: TUIProvider(config)
    )
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert len(app.screen.query(OptionList)) == 1
        await pilot.press("down", "enter")
        await pilot.pause()
        assert app.active_provider == app.config.providers[1]
        status = str(app.query_one("#status-bar", Static).render())
        assert "default" in status and "Provider 2" not in status


@pytest.mark.asyncio
async def test_alt_enter_inserts_newline(tmp_path: Path) -> None:
    app = TUICodingAgentApp(
        _config(), workspace=tmp_path, provider_factory=lambda config: TUIProvider(config)
    )
    async with app.run_test(size=(50, 18)) as pilot:
        prompt = app.query_one(PromptInput)
        prompt.focus()
        await pilot.press("a", "alt+enter", "b")
        assert prompt.text == "a\nb"


@pytest.mark.asyncio
async def test_waiting_timer_and_input_lock(tmp_path: Path) -> None:
    app = TUICodingAgentApp(
        _config(),
        workspace=tmp_path,
        provider_factory=lambda config: TUIProvider(config, delay=1.1),
    )
    async with app.run_test(size=(42, 16)) as pilot:
        prompt = app.query_one(PromptInput)
        prompt.focus()
        await pilot.press("x", "enter")
        await pilot.pause(0.3)
        assert app.session_state == SessionState.STREAMING
        assert prompt.disabled
        body = app.query(ChatEntry).last().query_one(".message-body", Static)
        assert "Imagining" in str(body.render())
        await pilot.pause(1.1)
        assert app.session_state == SessionState.IDLE
        assert not prompt.disabled


@pytest.mark.asyncio
async def test_error_is_visible_and_next_submission_is_available(tmp_path: Path) -> None:
    app = TUICodingAgentApp(
        _config(), workspace=tmp_path, provider_factory=lambda config: ErrorTUIProvider(config)
    )
    async with app.run_test(size=(80, 24)) as pilot:
        prompt = app.query_one(PromptInput)
        prompt.focus()
        await pilot.press("x", "enter")
        await pilot.pause(0.2)
        assert app.session_state == SessionState.IDLE
        assert not prompt.disabled
        assert "认证失败" in " ".join(
            str(item.render()) for item in app.query(".error-entry .message-body")
        )


@pytest.mark.asyncio
async def test_exit_command_closes_app(tmp_path: Path) -> None:
    provider = TUIProvider(_config().providers[0])
    app = TUICodingAgentApp(
        _config(), workspace=tmp_path, provider_factory=lambda _config: provider
    )
    async with app.run_test(size=(80, 24)) as pilot:
        prompt = app.query_one(PromptInput)
        prompt.focus()
        await pilot.press("/", "e", "x", "i", "t", "enter")
        await pilot.pause()
    assert provider.closed
