"""斜杠命令注册、冲突检测、UI 抽象与内置处理函数。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from .models import PermissionMode
from .runtime import DO_PROMPT

REVIEW_PROMPT = "请对当前项目的相关代码进行审查，重点检查正确性、安全性、可维护性和测试缺口。"


class CommandKind(StrEnum):
    LOCAL = "local"
    UI = "ui"
    PROMPT = "prompt"


class CommandUI(Protocol):
    def is_idle(self) -> bool: ...
    async def show_message(self, text: str) -> None: ...
    async def send_user_message(self, text: str, display_text: str) -> None: ...
    async def set_mode(self, mode: PermissionMode) -> None: ...
    async def trigger_compact(self) -> None: ...
    async def open_resume(self) -> None: ...
    async def clear_and_new_session(self) -> None: ...
    def request_exit(self) -> None: ...
    def permission_mode_name(self) -> str: ...
    def usage(self) -> tuple[int, int]: ...
    def tool_count(self) -> int: ...
    def memory_files(self) -> tuple[tuple[str, ...], tuple[str, ...]]: ...
    def model_name(self) -> str: ...
    def workspace_path(self) -> Path: ...
    def session_identity(self) -> tuple[str, Path]: ...
    def command_help(self) -> str: ...
    async def run_skill(self, name: str, args: str) -> None: ...
    async def manage_skills(self, args: str) -> None: ...
    def hooks_summary(self) -> str: ...
    async def manage_worktrees(self, args: str) -> None: ...


Handler = Callable[[CommandUI], Awaitable[None]]
ArgumentHandler = Callable[[CommandUI, str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    aliases: tuple[str, ...]
    description: str
    usage: str
    kind: CommandKind
    handler: Handler
    argument_hint: str = ""
    hidden: bool = False
    argument_handler: ArgumentHandler | None = None


class CommandRegistry:
    def __init__(self, commands: tuple[Command, ...] = ()) -> None:
        self._commands: dict[str, Command] = {}
        self._lookup: dict[str, Command] = {}
        for command in commands:
            self.register(command)

    def register(self, command: Command, *, replace: bool = False) -> None:
        canonical = _normalize_name(command.name)
        aliases = tuple(_normalize_name(alias) for alias in command.aliases)
        keys = (canonical, *aliases)
        if replace and canonical in self._lookup:
            old = self._lookup[canonical]
            self._commands.pop(old.name, None)
            for key, value in tuple(self._lookup.items()):
                if value == old:
                    self._lookup.pop(key, None)
        for key in keys:
            if key in self._lookup:
                raise ValueError(f"命令名称或别名冲突：{key}")
        normalized = Command(
            canonical,
            aliases,
            command.description,
            command.usage,
            command.kind,
            command.handler,
            command.argument_hint,
            command.hidden,
            command.argument_handler,
        )
        self._commands[canonical] = normalized
        for key in keys:
            self._lookup[key] = normalized

    async def dispatch(self, raw: str, ui: CommandUI) -> bool:
        text = raw.strip()
        if not text or not text.startswith("/"):
            return False
        parts = text.split(maxsplit=1)
        name = parts[0]
        args = parts[1].strip() if len(parts) == 2 else ""
        command = self._lookup.get(name.casefold())
        if command is None:
            await ui.show_message(f"未知命令：{text}。输入 /help 查看可用命令。")
            return True
        if command.kind != CommandKind.LOCAL and not ui.is_idle():
            await ui.show_message("请等待当前任务完成")
            return True
        if args:
            if command.argument_handler is None:
                await ui.show_message(f"未知命令：{text}。输入 /help 查看可用命令。")
                return True
            await command.argument_handler(ui, args)
        elif command.argument_handler is not None:
            await command.argument_handler(ui, "")
        else:
            await command.handler(ui)
        return True

    def visible(self) -> tuple[Command, ...]:
        return tuple(
            command
            for command in sorted(self._commands.values(), key=lambda item: item.name)
            if not command.hidden
        )

    def complete(self, prefix: str) -> tuple[Command, ...]:
        if "\n" in prefix or "\r" in prefix or not prefix.startswith("/"):
            return ()
        lowered = prefix.casefold()
        return tuple(command for command in self.visible() if command.name.startswith(lowered))

    def help_text(self) -> str:
        commands = self.visible()
        width = max((len(command.name) for command in commands), default=0)
        return "\n".join(
            f"{command.name.ljust(width)}  {command.description}" for command in commands
        )

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name.casefold() in self._lookup

    def __iter__(self) -> Iterator[str]:
        return (command.name for command in self.visible())

    def __len__(self) -> int:
        return len(self._commands)


async def _help(ui: CommandUI) -> None:
    helper = getattr(ui, "command_help", None)
    await ui.show_message(helper() if helper is not None else BUILTIN_REGISTRY.help_text())


async def _exit(ui: CommandUI) -> None:
    ui.request_exit()


async def _plan(ui: CommandUI) -> None:
    await ui.set_mode(PermissionMode.PLAN)


async def _do(ui: CommandUI) -> None:
    await ui.set_mode(PermissionMode.DEFAULT)
    await ui.send_user_message(DO_PROMPT, "/do · 执行上述计划")


async def _compact(ui: CommandUI) -> None:
    await ui.trigger_compact()


async def _resume(ui: CommandUI) -> None:
    await ui.open_resume()


async def _clear(ui: CommandUI) -> None:
    await ui.clear_and_new_session()


async def _status(ui: CommandUI) -> None:
    input_tokens, output_tokens = ui.usage()
    rows = (
        ("权限模式", ui.permission_mode_name()),
        ("累计 Token", f"↑{input_tokens} ↓{output_tokens}"),
        ("可用工具", str(ui.tool_count())),
        ("记忆条目", str(sum(len(group) for group in ui.memory_files()))),
        ("当前模型", ui.model_name()),
        ("当前工作目录", str(ui.workspace_path())),
    )
    width = max(len(key) for key, _ in rows)
    await ui.show_message("\n".join(f"{key.ljust(width)} : {value}" for key, value in rows))


async def _memory(ui: CommandUI) -> None:
    project, user = ui.memory_files()
    project_text = "、".join(project) if project else "（无）"
    user_text = "、".join(user) if user else "（无）"
    await ui.show_message(f"项目记忆：{project_text}\n用户记忆：{user_text}")


async def _permission(ui: CommandUI) -> None:
    await ui.show_message(ui.permission_mode_name())


async def _session(ui: CommandUI) -> None:
    identifier, path = ui.session_identity()
    await ui.show_message(f"session ID：{identifier}\n会话存档：{path}")


async def _review(ui: CommandUI) -> None:
    await ui.send_user_message(REVIEW_PROMPT, "/review · 代码审查")


async def _hooks(ui: CommandUI) -> None:
    await ui.show_message(ui.hooks_summary())


async def _worktree(ui: CommandUI, args: str) -> None:
    await ui.manage_worktrees(args)


def build_builtin_registry() -> CommandRegistry:
    entries = (
        Command("/exit", (), "退出 TUICodingAgent", "/exit", CommandKind.UI, _exit),
        Command("/plan", (), "进入只读规划模式", "/plan", CommandKind.UI, _plan),
        Command("/do", (), "执行已经形成的计划", "/do", CommandKind.PROMPT, _do),
        Command("/compact", (), "立即压缩当前会话上下文", "/compact", CommandKind.UI, _compact),
        Command("/resume", (), "恢复历史会话", "/resume", CommandKind.UI, _resume),
        Command("/clear", (), "结束当前会话并新建会话", "/clear", CommandKind.UI, _clear),
        Command("/help", (), "显示可用命令", "/help", CommandKind.LOCAL, _help),
        Command("/status", (), "显示运行状态", "/status", CommandKind.LOCAL, _status),
        Command("/memory", (), "列出已加载记忆文件", "/memory", CommandKind.LOCAL, _memory),
        Command(
            "/permission", (), "显示当前权限模式", "/permission", CommandKind.LOCAL, _permission
        ),
        Command("/session", (), "显示当前会话标识", "/session", CommandKind.LOCAL, _session),
        Command("/review", (), "让 AI 审查当前代码", "/review", CommandKind.PROMPT, _review),
        Command("/hooks", (), "列出已加载生命周期 Hook", "/hooks", CommandKind.LOCAL, _hooks),
        Command(
            "/worktree",
            (),
            "管理 Git Worktree 隔离目录",
            "/worktree create|list|enter|exit|remove",
            CommandKind.UI,
            _help,
            "create <slug> | list | enter <slug> | exit [--remove] | remove <slug>",
            argument_handler=_worktree,
        ),
    )
    return CommandRegistry(entries)


def build_skill_registry(skills) -> CommandRegistry:
    """在全新内置注册中心上挂载管理命令与动态 Skill 命令。"""

    registry = build_builtin_registry()

    async def noop(_ui: CommandUI) -> None:
        return None

    async def manage(ui: CommandUI, args: str) -> None:
        await ui.manage_skills(args)

    registry.register(
        Command(
            "/skill",
            (),
            "管理已加载 Skill",
            "/skill list | info <name> | reload",
            CommandKind.LOCAL,
            noop,
            "list | info <name> | reload",
            argument_handler=manage,
        )
    )
    for skill in skills:

        async def run(ui: CommandUI, args: str, name: str = skill.name) -> None:
            await ui.run_skill(name, args)

        registry.register(
            Command(
                f"/{skill.name}",
                (),
                f"{skill.description} [skill]",
                f"/{skill.name} [参数]",
                CommandKind.PROMPT,
                noop,
                "参数",
                argument_handler=run,
            ),
            replace=True,
        )
    return registry


def _normalize_name(name: str) -> str:
    normalized = name.strip().casefold()
    if not normalized.startswith("/") or any(character.isspace() for character in normalized):
        raise ValueError(f"无效命令名称：{name}")
    return normalized


BUILTIN_REGISTRY = build_builtin_registry()
BUILTIN_COMMANDS = BUILTIN_REGISTRY

__all__ = [
    "BUILTIN_COMMANDS",
    "BUILTIN_REGISTRY",
    "Command",
    "CommandKind",
    "CommandRegistry",
    "CommandUI",
    "REVIEW_PROMPT",
    "build_builtin_registry",
    "build_skill_registry",
]
