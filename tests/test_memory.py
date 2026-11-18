from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from tuicodingagent.memory import (
    INDEX_BYTE_LIMIT,
    MemoryManager,
    explicit_memory_content,
    should_update_memory,
)
from tuicodingagent.models import (
    ChatMessage,
    ProviderConfig,
    ProviderEvent,
    ProviderEventType,
    ToolDefinition,
)


class MemoryProvider:
    config = ProviderConfig("fake", "openai", "model", "secret")

    def __init__(self, response: str) -> None:
        self.response = response
        self.tools: Sequence[ToolDefinition] | None = None

    async def stream(
        self,
        system_prompt: object,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        self.tools = tools
        yield ProviderEvent(ProviderEventType.TEXT_DELTA, text=self.response)

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_memory_create_update_delete_and_no_tools(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    home = tmp_path / "home"
    manager = MemoryManager(workspace, home)
    create = MemoryProvider(
        '[{"action":"create","level":"user","type":"user_preference",'
        '"title":"简洁回复","slug":"terse_replies","content":"用户偏好简洁回复。"}]'
    )
    assert await manager.update(
        create, (ChatMessage("user", "记住回复简洁"), ChatMessage("assistant", "好的"))
    )
    note = home / ".tuicodingagent" / "memory" / "user_preference_terse_replies.md"
    assert note.is_file() and "created:" in note.read_text(encoding="utf-8")
    assert "简洁回复" in manager.index_text
    assert create.tools == ()

    update = MemoryProvider(
        '[{"action":"update","level":"user",'
        '"filename":"user_preference_terse_replies.md",'
        '"title":"非常简洁","content":"用户要求回复非常简洁。"}]'
    )
    assert await manager.update(update, (ChatMessage("user", "更简洁"),))
    assert "非常简洁" in manager.index_text

    delete = MemoryProvider(
        '[{"action":"delete","level":"user","filename":"user_preference_terse_replies.md"}]'
    )
    assert await manager.update(delete, (ChatMessage("user", "忘掉"),))
    assert not note.exists() and "非常简洁" not in manager.index_text


@pytest.mark.asyncio
async def test_memory_failure_is_isolated(tmp_path: Path) -> None:
    manager = MemoryManager(tmp_path / "project", tmp_path / "home")
    assert not await manager.update(MemoryProvider("not-json"), (ChatMessage("user", "x"),))


@pytest.mark.asyncio
async def test_explicit_memory_falls_back_when_model_returns_no_operation(tmp_path: Path) -> None:
    home = tmp_path / "home"
    manager = MemoryManager(tmp_path / "project", home)

    assert await manager.update(
        MemoryProvider("[]"),
        (ChatMessage("user", "记住  我喜欢吃辣椒"), ChatMessage("assistant", "好的")),
        "记住  我喜欢吃辣椒",
    )

    notes = tuple((home / ".tuicodingagent" / "memory").glob("user_preference_*.md"))
    assert len(notes) == 1
    assert "我喜欢吃辣椒" in notes[0].read_text(encoding="utf-8")
    assert "我喜欢吃辣椒" in manager.index_text


def test_explicit_memory_detection_ignores_general_memory_discussion() -> None:
    assert explicit_memory_content("请记住使用中文回复") == "使用中文回复"
    assert (
        explicit_memory_content("remember that I prefer short replies")
        == "I prefer short replies"
    )
    assert explicit_memory_content("人的记忆是如何形成的？") == ""
    assert not should_update_memory(1, "如果已有可靠项目记忆，不必重复读取")
    assert not should_update_memory(1, "查询记忆中的服务超时设置")


def test_index_injection_limit_and_trigger(tmp_path: Path) -> None:
    project = tmp_path / "project"
    memory_dir = project / ".tuicodingagent" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "MEMORY.md").write_text("记忆" * 20_000, encoding="utf-8")
    manager = MemoryManager(project, tmp_path / "home")
    assert len(manager.index_text.encode("utf-8")) <= INDEX_BYTE_LIMIT
    assert manager.index_text.endswith("(index truncated)")
    assert should_update_memory(5, "普通消息")
    assert should_update_memory(1, "remember this")
    assert not should_update_memory(1, "普通消息")
