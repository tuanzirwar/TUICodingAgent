from __future__ import annotations

from pathlib import Path

from test_context_tui import CompactProvider
from textual.widgets import OptionList, Static

from tuicodingagent.memory import MemoryManager
from tuicodingagent.models import AppConfig, ProviderConfig
from tuicodingagent.session import scan_sessions
from tuicodingagent.tui.app import TUICodingAgentApp
from tuicodingagent.tui.widgets import PromptInput


async def test_completion_filter_escape_and_enter_local_command(tmp_path: Path) -> None:
    config = AppConfig((ProviderConfig("fake", "openai", "model", "secret"),))
    provider = CompactProvider(config.providers[0])
    app = TUICodingAgentApp(config, workspace=tmp_path, provider_factory=lambda _: provider)
    async with app.run_test(size=(100, 32)) as pilot:
        prompt = app.query_one(PromptInput)
        menu = app.query_one("#command-menu", OptionList)
        prompt.focus()
        await pilot.press("/")
        await pilot.pause()
        assert menu.option_count == 17 and prompt.completion_active
        await pilot.press("s")
        await pilot.pause()
        assert menu.option_count == 3
        await pilot.press("escape")
        assert not prompt.completion_active and prompt.text == "/s"

        prompt.clear()
        await pilot.press("/", "s", "t", "a", "t", "u", "s", "enter")
        await pilot.pause(0.1)
        rendered = " ".join(str(widget.render()) for widget in app.query(Static))
        assert "权限模式" in rendered and "当前工作目录" in rendered
        assert provider.main_calls == 0


async def test_review_persists_and_clear_starts_new_session(tmp_path: Path) -> None:
    config = AppConfig((ProviderConfig("fake", "openai", "model", "secret"),))
    provider = CompactProvider(config.providers[0])
    memory = MemoryManager(tmp_path, tmp_path / "home")
    app = TUICodingAgentApp(
        config,
        workspace=tmp_path,
        provider_factory=lambda _: provider,
        memory_manager=memory,
    )
    async with app.run_test(size=(100, 32)) as pilot:
        prompt = app.query_one(PromptInput)
        prompt.focus()
        original_id = app.session_identity()[0]
        await pilot.press("/", "r", "e", "v", "i", "e", "w", "enter")
        await pilot.pause(0.3)
        assert provider.main_calls == 1
        assert app.runtime is not None and "Skill `review` 结果" in app.runtime.history[0].content
        assert "Skill `review`" in scan_sessions(tmp_path)[0].context.conversation_path.read_text(
            encoding="utf-8"
        )

        await pilot.press("/", "c", "l", "e", "a", "r", "enter")
        await pilot.pause(0.2)
        assert app.session_identity()[0] != original_id
        assert app.runtime.history == () and app.usage() == (0, 0)
        assert any(item.context.session_id == original_id for item in scan_sessions(tmp_path))
