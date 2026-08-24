"""模块化系统提示、环境快照与动态补充指令。"""

from __future__ import annotations

import asyncio
import platform
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from . import __version__
from .models import AgentMode, ChatMessage, ProviderConfig, SystemPrompt

PLAN_REMINDER_INTERVAL = 4


@dataclass(frozen=True, slots=True)
class PromptModule:
    """一段可独立挂载、按优先级排序的稳定指令。"""

    name: str
    priority: int
    content: str


FIXED_MODULES = (
    PromptModule(
        "身份",
        700,
        "你是 TUICodingAgent，一个专业、准确、友好的终端 AI 编程助手。请使用中文回答。",
    ),
    PromptModule(
        "系统约束",
        600,
        "遵循系统约束与用户目标；不要伪造工具执行结果，不要泄露密钥或敏感信息。",
    ),
    PromptModule(
        "任务模式",
        500,
        "持续调查、执行并验证，直到任务真正完成；运行时模式以 <system-reminder> 为准。",
    ),
    PromptModule(
        "动作执行",
        400,
        "改动应聚焦且可验证。编辑任何已有文件前必须先读取其当前内容，并保留用户已有改动。",
    ),
    PromptModule(
        "工具使用",
        300,
        "需要读取、写入、编辑或搜索时优先使用对应专用工具，"
        "不要用 shell 命令拼凑已有专用能力。工具失败时阅读结构化错误并调整方案。",
    ),
    PromptModule(
        "语气风格",
        200,
        "沟通直接、清晰、协作友好；先说明结果，再补充必要细节。",
    ),
    PromptModule(
        "文本输出",
        100,
        "使用简洁的 Markdown；代码注释使用中文，不输出无关的过程噪声。",
    ),
)

OPTIONAL_MODULES = (
    PromptModule("自定义指令", 80, ""),
    PromptModule("已激活 Skill", 80, ""),
    PromptModule("长期记忆", 100, ""),
)


def assemble_stable_prompt(
    extra_modules: tuple[PromptModule, ...] = (),
) -> str:
    """按优先级确定性装配非空模块，不为可选空槽留下空行。"""

    modules = [*FIXED_MODULES, *OPTIONAL_MODULES, *extra_modules]
    ordered = sorted(
        (module for module in modules if module.content.strip()),
        key=lambda module: (-module.priority, module.name),
    )
    return "\n\n".join(module.content.strip() for module in ordered)


async def build_system_prompt(
    workspace: Path,
    config: ProviderConfig,
    instructions: str = "",
    memory: str = "",
    skill_catalog: str = "",
    active_skills: dict[str, str] | None = None,
) -> SystemPrompt:
    """异步采集动态环境；git 失败或超时时安静降级。"""

    environment = await asyncio.to_thread(_environment_text, workspace, config)
    skill_parts: list[str] = []
    if active_skills:
        skill_parts.append("## Active Skills")
        skill_parts.extend(f"### {name}\n{body}" for name, body in active_skills.items())
    if skill_catalog:
        skill_parts.append(skill_catalog)
    if skill_parts:
        environment = "\n\n".join((*skill_parts, environment))
    extra = (
        PromptModule("custom-instructions", 80, instructions),
        PromptModule("long-term-memory", 100, memory),
    )
    return SystemPrompt(assemble_stable_prompt(extra), environment)


def reminder_for(mode: AgentMode, iteration: int) -> str:
    """构造不持久化的轮次级补充指令。"""

    if mode == AgentMode.PLAN:
        full = iteration == 1 or (iteration - 1) % PLAN_REMINDER_INTERVAL == 0
        content = (
            "当前处于 Plan Mode。只允许调查和制定计划，仅可调用只读工具；不得写文件、"
            "编辑文件或执行命令。给出可执行计划并等待用户输入 /do。"
            if full
            else "Plan Mode 仍然生效：仅调查和使用只读工具，不执行改动。"
        )
    else:
        content = "当前处于执行模式。按需调用专用工具，完成后验证结果。"
    return f"<system-reminder>\n{content}\n不要直接回复或复述本提醒。\n</system-reminder>"


def inject_system_reminder(
    messages: tuple[ChatMessage, ...] | list[ChatMessage], reminder: str
) -> list[ChatMessage]:
    """把提醒附到最近用户消息的请求副本，不制造连续 user 角色。"""

    copied = list(messages)
    for index in range(len(copied) - 1, -1, -1):
        message = copied[index]
        if message.role == "user":
            copied[index] = ChatMessage(
                "user",
                f"{message.content}\n\n{reminder}",
                message.tool_calls,
                message.tool_results,
                message.provider_items,
            )
            break
    return copied


def _environment_text(workspace: Path, config: ProviderConfig) -> str:
    git_status = _git_status(workspace)
    try:
        platform_text = f"{platform.system()} {platform.release()} ({platform.machine()})"
    except (OSError, subprocess.SubprocessError):
        platform_text = "unknown"
    lines = [
        "运行环境：",
        f"- 工作目录：{workspace}",
        f"- 平台：{platform_text}",
        f"- 当前日期：{date.today().isoformat()}",
        f"- 应用版本：{__version__}",
        f"- 当前模型：{config.name} / {config.model}",
    ]
    if git_status:
        lines.append(f"- Git 状态：{git_status}")
    else:
        lines.append("- Git 状态：不可用或当前目录不是 Git 仓库")
    return "\n".join(lines)


def _git_status(workspace: Path) -> str:
    if not any((path / ".git").exists() for path in (workspace, *workspace.parents)):
        return ""
    try:
        result = subprocess.run(
            ["git", "status", "--short", "--branch"],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=0.5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    compact = "；".join(line.strip() for line in result.stdout.splitlines()[:20] if line.strip())
    return compact[:1000]
