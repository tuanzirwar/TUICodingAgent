from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from textual.widgets import OptionList, Static

from tuicodingagent.models import (
    AppConfig,
    ChatMessage,
    ProviderConfig,
    ProviderEvent,
    ProviderEventType,
    SystemPrompt,
    ToolDefinition,
)
from tuicodingagent.session import SessionContext, Writer
from tuicodingagent.tui.app import TUICodingAgentApp
from tuicodingagent.tui.widgets import PromptInput


class CompactProvider:
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self.main_calls = 0
        self.summary_calls = 0

    async def stream(
        self,
        system_prompt: object,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        if isinstance(system_prompt, SystemPrompt) and "九个编号部分" in str(system_prompt):
            self.summary_calls += 1
            yield ProviderEvent(
                ProviderEventType.TEXT_DELTA,
                text=(
                    "<analysis>草稿</analysis><summary>1. 请求 2. 概念 3. 文件 4. 错误 "
                    "5. 过程 6. 原话 7. 待办 8. 当前 9. 下一步</summary>"
                ),
            )
        else:
            self.main_calls += 1
            yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="普通回复")

    async def close(self) -> None:
        return None


async def test_compact_and_unknown_commands_do_not_enter_normal_chat(tmp_path: Path) -> None:
    config = AppConfig((ProviderConfig("fake", "openai", "model", "secret"),))
    provider = CompactProvider(config.providers[0])
    app = TUICodingAgentApp(config, workspace=tmp_path, provider_factory=lambda _: provider)
    async with app.run_test(size=(100, 30)) as pilot:
        prompt = app.query_one(PromptInput)
        prompt.focus()
        await pilot.press("h", "i", "enter")
        await pilot.pause(0.2)
        assert provider.main_calls == 1

        await pilot.press("/", "c", "o", "m", "p", "a", "c", "t", "enter")
        await pilot.pause(0.2)
        assert provider.summary_calls == 1 and provider.main_calls == 1
        rendered = " ".join(str(widget.render()) for widget in app.query(Static))
        assert "已压缩，token 从" in rendered

        await pilot.press("/", "u", "n", "k", "n", "o", "w", "n", "enter")
        await pilot.pause(0.1)
        assert provider.summary_calls == 1 and provider.main_calls == 1
        rendered = " ".join(str(widget.render()) for widget in app.query(Static))
        assert "未知命令" in rendered and "/help" in rendered


async def test_resume_filters_restores_and_appends_original_jsonl(tmp_path: Path) -> None:
    old_context = SessionContext.create(tmp_path, "20260818-120000-a1b2")
    with Writer(old_context, "old-model") as writer:
        writer.append(ChatMessage("user", "old 会话标题"))
        writer.append(ChatMessage("assistant", "旧回复"))
    before = len(old_context.conversation_path.read_text(encoding="utf-8").splitlines())
    config = AppConfig((ProviderConfig("fake", "openai", "model", "secret"),))
    provider = CompactProvider(config.providers[0])
    app = TUICodingAgentApp(config, workspace=tmp_path, provider_factory=lambda _: provider)

    async with app.run_test(size=(100, 30)) as pilot:
        prompt = app.query_one(PromptInput)
        prompt.focus()
        await pilot.press("/", "r", "e", "s", "u", "m", "e", "enter")
        await pilot.pause(0.2)
        assert len(app.screen.query(OptionList)) == 1
        await pilot.press("o", "l", "d", "enter")
        await pilot.pause(0.3)
        assert app.runtime is not None
        assert app.runtime.history[:2] == (
            ChatMessage("user", "old 会话标题"),
            ChatMessage("assistant", "旧回复"),
        )
        rendered = " ".join(str(widget.render()) for widget in app.query(Static))
        assert "已恢复会话 20260818-120000-a1b2" in rendered

        prompt.focus()
        await pilot.press("n", "e", "w", "enter")
        await pilot.pause(0.3)

    after = len(old_context.conversation_path.read_text(encoding="utf-8").splitlines())
    assert after == before + 2
