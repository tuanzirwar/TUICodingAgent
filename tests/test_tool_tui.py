from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest
from textual.widgets import Markdown, Static

from tuicodingagent.models import (
    AppConfig,
    ChatMessage,
    ProviderConfig,
    ProviderEvent,
    ProviderEventType,
    SessionState,
    ToolCall,
    ToolDefinition,
)
from tuicodingagent.runtime import DO_PROMPT
from tuicodingagent.tui.app import TUICodingAgentApp
from tuicodingagent.tui.widgets import PromptInput, ToolEntry


class ToolTUIProvider:
    def __init__(self, config: ProviderConfig, path: str) -> None:
        self.config = config
        self.path = path
        self.round = 0

    async def stream(
        self,
        system_prompt: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        self.round += 1
        if self.round == 1:
            yield ProviderEvent(
                ProviderEventType.TOOL_CALL,
                tool_call=ToolCall("read-1", "read_file", {"path": self.path}),
            )
        else:
            yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="**已处理**")

    async def close(self) -> None:
        return None


def _config() -> AppConfig:
    return AppConfig((ProviderConfig("Fake", "openai", "model", "secret"),))


@pytest.mark.asyncio
@pytest.mark.parametrize("exists", [True, False])
async def test_tool_line_shows_success_or_failure_and_input_recovers(
    tmp_path: Path, exists: bool
) -> None:
    path = "very-long.txt"
    if exists:
        (tmp_path / path).write_text("内容" * 200, encoding="utf-8")
    app = TUICodingAgentApp(
        _config(),
        workspace=tmp_path,
        provider_factory=lambda config: ToolTUIProvider(config, path),
    )
    async with app.run_test(size=(80, 24)) as pilot:
        prompt = app.query_one(PromptInput)
        prompt.focus()
        await pilot.press("r", "e", "a", "d", "enter")
        await pilot.pause(0.3)

        entry = app.query_one(ToolEntry)
        title = str(entry.query_one(".tool-title", Static).render())
        summary = str(entry.query_one(".tool-summary", Static).render())
        assert "● Read(very-long.txt)" in title
        assert entry.has_class("success" if exists else "failed")
        if exists:
            assert len(summary) <= 241 and "…" in summary
        else:
            assert "读取文件失败" in summary
        assert len(app.query(Markdown)) == 1
        assert app.session_state == SessionState.IDLE and not prompt.disabled


class ModeProvider:
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self.requests: list[tuple[tuple[str, ...], str]] = []

    async def stream(
        self,
        system_prompt: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        self.requests.append((tuple(item.name for item in tools), messages[-1].content))
        yield ProviderEvent(ProviderEventType.USAGE, input_tokens=5, output_tokens=2)
        yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="完成")

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_plan_do_and_usage_status(tmp_path: Path) -> None:
    provider = ModeProvider(_config().providers[0])
    app = TUICodingAgentApp(
        _config(), workspace=tmp_path, provider_factory=lambda _config: provider
    )
    async with app.run_test(size=(90, 24)) as pilot:
        prompt = app.query_one(PromptInput)
        prompt.focus()
        await pilot.press("/", "p", "l", "a", "n", "enter")
        await pilot.pause()
        assert "plan" in str(app.query_one("#status-bar", Static).render())
        await pilot.press("p", "l", "a", "n", "enter")
        await pilot.pause(0.2)
        assert provider.requests[0][0] == (
            "read_file",
            "glob",
            "grep",
            "load_skill",
            "Agent",
            "TaskList",
            "TaskGet",
        )

        await pilot.press("/", "d", "o", "enter")
        await pilot.pause(0.2)
        assert provider.requests[1][1].startswith(f"{DO_PROMPT}\n\n<system-reminder>")
        assert len(provider.requests[1][0]) == 15
        status = str(app.query_one("#status-bar", Static).render())
        assert "default" in status and "↑10" in status and "↓4" in status


class CancelProvider(ModeProvider):
    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self.calls = 0

    async def stream(
        self,
        system_prompt: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        self.calls += 1
        if self.calls == 1:
            await asyncio.Event().wait()
        yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="取消后恢复")


@pytest.mark.asyncio
async def test_escape_cancels_loop_without_exiting_and_next_turn_works(tmp_path: Path) -> None:
    provider = CancelProvider(_config().providers[0])
    app = TUICodingAgentApp(
        _config(), workspace=tmp_path, provider_factory=lambda _config: provider
    )
    async with app.run_test(size=(80, 24)) as pilot:
        prompt = app.query_one(PromptInput)
        prompt.focus()
        await pilot.press("x", "enter")
        await pilot.pause(0.1)
        assert app.session_state == SessionState.STREAMING
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert app.session_state == SessionState.IDLE and app.is_running
        assert app.runtime and app.runtime.history[-1].role == "assistant"
        await pilot.press("y", "enter")
        await pilot.pause(0.2)
        assert app.session_state == SessionState.IDLE
        assert provider.calls == 2 and len(app.query(Markdown)) >= 1
