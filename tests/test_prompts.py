from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tuicodingagent.models import AgentMode, ChatMessage, ProviderConfig
from tuicodingagent.prompts import (
    FIXED_MODULES,
    OPTIONAL_MODULES,
    PromptModule,
    assemble_stable_prompt,
    build_system_prompt,
    inject_system_reminder,
    reminder_for,
)
from tuicodingagent.tools import create_default_registry


def test_prompt_modules_are_ordered_deterministically_and_empty_slots_are_skipped() -> None:
    first = assemble_stable_prompt()
    second = assemble_stable_prompt()
    assert first.encode() == second.encode()
    positions = [first.index(module.content) for module in FIXED_MODULES]
    assert positions == sorted(positions)
    assert all(module.name not in first for module in OPTIONAL_MODULES)
    assert "\n\n\n" not in first

    inserted = PromptModule("插入模块", 350, "插入内容")
    extended = assemble_stable_prompt((inserted,))
    assert extended.index("改动应聚焦") < extended.index("插入内容") < extended.index("需要读取")


@pytest.mark.asyncio
async def test_environment_is_separate_complete_and_never_contains_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").mkdir()

    def unavailable(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired("git", 0.5)

    monkeypatch.setattr(subprocess, "run", unavailable)
    config = ProviderConfig("测试服务", "openai", "demo-model", "never-print-me")
    prompt = await build_system_prompt(tmp_path, config)

    assert prompt.stable == assemble_stable_prompt()
    for label in ("工作目录", "平台", "当前日期", "Git 状态", "应用版本", "当前模型"):
        assert label in prompt.environment
    assert "never-print-me" not in str(prompt)
    assert "不可用" in prompt.environment


@pytest.mark.asyncio
async def test_instructions_and_memory_fill_optional_prompt_slots(tmp_path: Path) -> None:
    config = ProviderConfig("测试服务", "openai", "demo-model", "secret")
    prompt = await build_system_prompt(tmp_path, config, "项目指令", "长期偏好")
    assert "项目指令" in prompt.stable and "长期偏好" in prompt.stable
    assert prompt.stable.index("长期偏好") < prompt.stable.index("项目指令")


def test_system_reminder_is_injected_into_copy_and_cadence_is_predictable() -> None:
    original = [ChatMessage("user", "问题")]
    full = reminder_for(AgentMode.PLAN, 1)
    short = reminder_for(AgentMode.PLAN, 2)
    repeated = reminder_for(AgentMode.PLAN, 5)
    injected = inject_system_reminder(original, full)

    assert full.startswith("<system-reminder>") and full.endswith("</system-reminder>")
    assert "等待用户输入 /do" in full
    assert "等待用户输入 /do" not in short
    assert repeated == full
    assert original == [ChatMessage("user", "问题")]
    assert injected[0].content.startswith("问题\n\n<system-reminder>")


def test_critical_rules_are_reinforced_in_prompt_and_tool_descriptions(tmp_path: Path) -> None:
    prompt = assemble_stable_prompt()
    definitions = {
        item.name: item.description for item in create_default_registry(tmp_path).definitions()
    }
    assert "优先使用对应专用工具" in prompt
    assert "编辑任何已有文件前必须先读取" in prompt
    assert "必须优先使用专用工具" in definitions["bash"]
    assert "编辑前必须先用 read_file" in definitions["edit_file"]
