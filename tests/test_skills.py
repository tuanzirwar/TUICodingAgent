from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import httpx
import pytest

from tuicodingagent.commands import build_skill_registry
from tuicodingagent.models import (
    ChatMessage,
    ProviderConfig,
    ProviderEvent,
    ProviderEventType,
    ToolCall,
    ToolDefinition,
)
from tuicodingagent.runtime import ConversationRuntime
from tuicodingagent.skills import SkillDef, SkillExecutor, SkillLoader, parse_skill_file
from tuicodingagent.skills.installer import InstallSkillTool, SkillInstaller
from tuicodingagent.tools import LoadSkillTool, create_default_registry

ALL_TOOLS = {
    "read_file",
    "write_file",
    "edit_file",
    "bash",
    "glob",
    "grep",
    "load_skill",
    "install_skill",
}


def _skill(
    name: str,
    body: str,
    *,
    description: str = "测试 Skill",
    tools: str = "[read_file]",
    mode: str = "inline",
    context: str = "full",
) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"tools: {tools}\n"
        f"mode: {mode}\n"
        f"context: {context}\n"
        "---\n"
        f"{body}\n"
    )


def test_parse_single_and_directory_skill_and_arguments(tmp_path: Path) -> None:
    single = tmp_path / "one.md"
    single.write_text(_skill("one", "处理 $ARGUMENTS"), encoding="utf-8")
    directory = tmp_path / "two"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        _skill("two", "目录正文", mode="fork", context="recent"), encoding="utf-8"
    )
    (directory / "references.py").write_text("VALUE = 1", encoding="utf-8")

    first = parse_skill_file(single)
    second = parse_skill_file(directory)
    assert first.name == "one" and not first.is_directory and "$ARGUMENTS" in first.prompt_body
    assert second.is_directory and second.mode == "fork" and second.context == "recent"


def test_loader_priority_catalog_hot_reload_and_fallback(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    project = tmp_path / ".tuicodingagent" / "skills"
    user = tmp_path / "home-skills"
    builtin = tmp_path / "builtins"
    for root in (project, user, builtin):
        root.mkdir(parents=True)
    (project / "same.md").write_text(_skill("same", "项目正文"), encoding="utf-8")
    (user / "same.md").write_text(_skill("same", "用户正文"), encoding="utf-8")
    (builtin / "base.md").write_text(_skill("base", "不可提前披露的正文"), encoding="utf-8")
    (project / "bad.md").write_text("not frontmatter", encoding="utf-8")
    loader = SkillLoader(
        tmp_path,
        user_root=user,
        builtin_root=builtin,
        available_tools=ALL_TOOLS,
    )
    loaded = loader.load_all()

    assert [item.name for item in loaded] == ["base", "same"]
    assert loader.get("same").prompt_body == "项目正文"
    assert loader.get_source_label("same") == "project"
    assert "不可提前披露的正文" not in loader.catalog_text()
    assert "跳过无效 Skill" in caplog.text

    (project / "same.md").write_text(_skill("same", "热更新正文"), encoding="utf-8")
    assert loader.get("same").prompt_body == "热更新正文"
    (project / "same.md").write_text("broken", encoding="utf-8")
    assert loader.get("same").prompt_body == "热更新正文"


def test_missing_whitelisted_tool_fails_fast(tmp_path: Path) -> None:
    root = tmp_path / ".tuicodingagent" / "skills"
    root.mkdir(parents=True)
    (root / "bad.md").write_text(_skill("bad", "正文", tools="[ghost]"), encoding="utf-8")
    with pytest.raises(ValueError, match="不存在的工具.*ghost"):
        SkillLoader(tmp_path, available_tools=ALL_TOOLS).load_all()


class CaptureProvider:
    config = ProviderConfig("fake", "openai", "model", "secret")

    def __init__(self, answer: str = "完成") -> None:
        self.answer = answer
        self.requests: list[tuple[object, tuple[ChatMessage, ...], tuple[str, ...]]] = []
        self.closed = False

    async def stream(
        self,
        system_prompt,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        self.requests.append((system_prompt, tuple(messages), tuple(tool.name for tool in tools)))
        yield ProviderEvent(ProviderEventType.TEXT_DELTA, text=self.answer)

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_load_skill_pins_sop_and_whitelist_applies_each_round(tmp_path: Path) -> None:
    root = tmp_path / ".tuicodingagent" / "skills"
    root.mkdir(parents=True)
    (root / "focused.md").write_text(
        _skill("focused", "只读 SOP", tools="[read_file, grep]"), encoding="utf-8"
    )
    registry = create_default_registry(tmp_path)
    loader = SkillLoader(tmp_path, available_tools=ALL_TOOLS)
    loader.load_all()
    provider = CaptureProvider()
    runtime = ConversationRuntime(provider, tool_registry=registry, workspace=tmp_path)
    load_tool = LoadSkillTool(loader, runtime)
    registry.register(load_tool)
    runtime.set_skill_catalog(loader.catalog_text())
    result = await load_tool.execute(ToolCall("load", "load_skill", {"name": "focused"}))
    assert result.success and "只读 SOP" not in result.output

    _ = [event async for event in runtime.chat("执行")]
    prompt, _, tools = provider.requests[0]
    assert "只读 SOP" in str(prompt) and "## Active Skills" in str(prompt)
    assert tools == ("read_file", "grep", "load_skill")

    runtime.activate_skill("second", "第二份 SOP", ("read_file", "glob"))
    _ = [event async for event in runtime.chat("再次执行")]
    assert provider.requests[1][2] == ("read_file", "load_skill")
    await runtime.clear_and_new_session()
    _ = [event async for event in runtime.chat("清理后")]
    assert "## Active Skills" not in str(provider.requests[2][0])
    assert provider.requests[2][2] == (
        "read_file",
        "write_file",
        "edit_file",
        "bash",
        "glob",
        "grep",
        "load_skill",
    )


@pytest.mark.asyncio
async def test_fork_context_isolated_and_result_does_not_mutate_main(tmp_path: Path) -> None:
    main_provider = CaptureProvider("主回复")
    main = ConversationRuntime(main_provider, tool_registry=create_default_registry(tmp_path))
    main.seed_history([ChatMessage("user", str(index)) for index in range(8)])
    loader = SkillLoader(tmp_path, available_tools=ALL_TOOLS)
    forks: list[CaptureProvider] = []

    def factory(_config: ProviderConfig) -> CaptureProvider:
        provider = CaptureProvider("独立结论")
        forks.append(provider)
        return provider

    executor = SkillExecutor(
        loader,
        main,
        main.tool_registry,
        tmp_path,
        factory,
        (main_provider.config,),
    )
    skill = SkillDef(
        "forked",
        "隔离",
        "独立 SOP $ARGUMENTS",
        ("read_file",),
        "fork",
        None,
        "recent",
        tmp_path / "forked.md",
        False,
    )
    before = main.history
    result = await executor.execute_fork(skill, "参数")

    assert result == "独立结论" and main.history == before
    request = forks[0].requests[0]
    assert len(request[1]) == 2
    assert all(f"user: {index}" in request[1][0].content for index in range(3, 8))
    assert "独立 SOP 参数" in str(request[0]) and forks[0].closed


class SkillUI:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.messages: list[str] = []

    def is_idle(self) -> bool:
        return True

    async def run_skill(self, name: str, args: str) -> None:
        self.calls.append((name, args))

    async def show_message(self, text: str) -> None:
        self.messages.append(text)


@pytest.mark.asyncio
async def test_skill_command_overrides_builtin_and_accepts_arguments(tmp_path: Path) -> None:
    skill = SkillDef(
        "review", "自定义审查", "正文", (), "inline", None, "full", tmp_path / "x.md", False
    )
    registry = build_skill_registry((skill,))
    ui = SkillUI()
    assert len([item for item in registry.visible() if item.name == "/review"]) == 1
    await registry.dispatch("/review src", ui)
    assert ui.calls == [("review", "src")]
    review = next(item for item in registry.visible() if item.name == "/review")
    assert "[skill]" in review.description


@pytest.mark.asyncio
async def test_raw_url_install_is_atomic_and_triggers_reload(tmp_path: Path) -> None:
    content = _skill("remote", "远程正文").encode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "raw.githubusercontent.com"
        return httpx.Response(200, content=content)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    installer = SkillInstaller(tmp_path / "user-skills", client)
    reloaded: list[bool] = []

    def reload_callback() -> None:
        reloaded.append(True)

    tool = InstallSkillTool(installer, reload_callback)
    result = await tool.execute(
        ToolCall(
            "install",
            "install_skill",
            {"url": "https://raw.githubusercontent.com/acme/repo/main/SKILL.md"},
        )
    )
    await client.aclose()

    assert result.success and reloaded == [True]
    installed = tmp_path / "user-skills" / "remote" / "SKILL.md"
    assert installed.is_file() and parse_skill_file(installed.parent).name == "remote"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["github", "skills_sh"])
async def test_directory_install_sources_keep_supporting_files(tmp_path: Path, source: str) -> None:
    skill_text = _skill("packaged", "使用参考资料")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/references"):
            return httpx.Response(
                200,
                json=[
                    {
                        "name": "helper.py",
                        "type": "file",
                        "size": 9,
                        "download_url": "https://raw.githubusercontent.com/acme/repo/main/helper.py",
                    }
                ],
            )
        if request.url.host == "api.github.com":
            return httpx.Response(
                200,
                json=[
                    {
                        "name": "SKILL.md",
                        "type": "file",
                        "size": len(skill_text.encode()),
                        "download_url": "https://raw.githubusercontent.com/acme/repo/main/SKILL.md",
                    },
                    {
                        "name": "references",
                        "type": "dir",
                        "url": "https://api.github.com/repos/acme/repo/contents/skill/references",
                    },
                ],
            )
        if request.url.path.endswith("helper.py"):
            return httpx.Response(200, content=b"VALUE = 1")
        if request.url.host == "raw.githubusercontent.com":
            return httpx.Response(200, content=skill_text.encode())
        if request.url.host == "skills.sh":
            return httpx.Response(
                200,
                json={
                    "files": [
                        {"path": "SKILL.md", "contents": skill_text},
                        {"path": "references/helper.py", "contents": "VALUE = 1"},
                    ]
                },
            )
        raise AssertionError(request.url)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    installer = SkillInstaller(tmp_path / source, client)
    url = (
        "https://github.com/acme/repo/tree/main/skill"
        if source == "github"
        else "https://skills.sh/acme/repo/packaged"
    )
    name, destination = await installer.install(url)
    await client.aclose()

    assert name == "packaged"
    assert (destination / "SKILL.md").is_file()
    assert (destination / "references" / "helper.py").read_text(encoding="utf-8") == "VALUE = 1"
