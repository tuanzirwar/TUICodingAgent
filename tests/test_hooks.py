from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import httpx
import pytest
from test_context_tui import CompactProvider
from textual.widgets import Static

from tuicodingagent.hooks import HookEngine, HookEvent, load_hooks
from tuicodingagent.hooks.models import (
    HookAction,
    HookCondition,
    HookPredicate,
    HookRule,
)
from tuicodingagent.matching import parse_match, parse_permission_match
from tuicodingagent.models import (
    AppConfig,
    ChatMessage,
    PermissionDecision,
    PermissionMode,
    ProviderConfig,
    ProviderEvent,
    ProviderEventType,
    RuntimeEventType,
    ToolCall,
    ToolDefinition,
)
from tuicodingagent.permissions import PermissionManager, decide_rules, parse_rule
from tuicodingagent.runtime import ConversationRuntime
from tuicodingagent.tools import create_default_registry
from tuicodingagent.tui.app import TUICodingAgentApp
from tuicodingagent.tui.widgets import PromptInput


def _rule(
    name: str,
    event: HookEvent,
    action: HookAction,
    *,
    predicate: HookPredicate | None = None,
    only_once: bool = False,
    async_run: bool = False,
    timeout: float = 2,
) -> HookRule:
    return HookRule(
        name,
        event,
        predicate,
        action,
        only_once,
        async_run,
        timeout,
        Path("hooks.yaml"),
    )


@pytest.mark.parametrize(
    ("matcher", "yes", "no"),
    [
        ({"type": "exact", "value": "a*b"}, "a*b", "axxb"),
        ({"type": "glob", "value": "src/**/*.py"}, "src/a/x.py", "src/x.md"),
        ({"type": "regex", "value": r"^npm (install|test)$"}, "npm test", "npm run"),
        (
            {"type": "not", "inner": {"type": "regex", "value": "^rm"}},
            "ls -lh",
            "rm -rf .",
        ),
    ],
)
def test_shared_matcher_types(matcher: dict[str, object], yes: str, no: str) -> None:
    parsed = parse_match(matcher)
    assert parsed.matches(yes) and not parsed.matches(no)


def test_permission_prefixes_nested_not_and_invalid_regex() -> None:
    assert parse_permission_match("=git status").matches("git status")
    assert not parse_permission_match("=git status").matches("git status -s")
    assert parse_permission_match("~^npm (install|test)$").matches("npm install")
    inverse = parse_permission_match("!~^rm")
    assert inverse.matches("ls") and not inverse.matches("rm -rf .")
    nested = parse_permission_match("!!=value")
    assert nested.matches("value") and not nested.matches("")
    with pytest.raises(ValueError, match="正则表达式无效"):
        parse_permission_match("~[")

    exact = parse_rule("Bash(=git status)", PermissionDecision.ALLOW)
    call = ToolCall("id", "bash", {"command": "git status"})
    assert decide_rules(call, "git status", (exact,)) == PermissionDecision.ALLOW


def test_loader_merges_layers_and_skips_bad_rules(tmp_path: Path, capsys) -> None:
    project = tmp_path / "project.yaml"
    user = tmp_path / "user.yaml"
    project.write_text(
        """hooks:
  - name: project-start
    event: SessionStart
    action: {type: prompt, text: zh-CN}
    only_once: true
  - name: bad-async
    event: PreToolUse
    async: true
    action: {type: shell, command: echo bad}
  - name: bad-event
    event: UnknownEvent
    action: {type: prompt, text: bad}
""",
        encoding="utf-8",
    )
    user.write_text(
        """hooks:
  - name: user-stop
    event: Stop
    action: {type: http, url: 'https://example.test/done'}
  - name: project-start
    event: SessionEnd
    action: {type: prompt, text: duplicate}
  - name: bad-if
    event: PreToolUse
    if: {all_of: [], any_of: []}
    action: {type: prompt, text: bad}
""",
        encoding="utf-8",
    )
    rules, sources = load_hooks(tmp_path, (project, user))
    error = capsys.readouterr().err

    assert [rule.name for rule in rules] == ["project-start", "user-stop"]
    assert sources == (project.resolve(), user.resolve())
    assert "async not allowed for blocking events" in error
    assert 'unknown event "UnknownEvent"' in error
    assert "duplicate name" in error and "exactly one" in error


@pytest.mark.asyncio
async def test_predicate_nested_field_only_once_prompt_and_reset() -> None:
    predicate = HookPredicate(
        "all_of",
        (
            HookCondition("tool_name", parse_match({"type": "exact", "value": "write_file"})),
            HookCondition("tool_input.path", parse_match({"type": "glob", "value": "src/*.py"})),
        ),
    )
    engine = HookEngine(
        (
            _rule(
                "remember",
                HookEvent.POST_TOOL_USE,
                HookAction("prompt", {"text": "格式化完成"}),
                predicate=predicate,
                only_once=True,
            ),
        )
    )
    base = {"tool_name": "write_file", "tool_input": {"path": "src/a.py"}}
    await engine.emit(HookEvent.POST_TOOL_USE, base)
    await engine.emit(HookEvent.POST_TOOL_USE, base)
    assert engine.drain_prompts() == ("格式化完成",)
    summary = engine.summary()
    assert "PostToolUse:" in summary and "remember" in summary and "[once]" in summary
    engine.reset_session()
    await engine.emit(HookEvent.POST_TOOL_USE, base)
    assert engine.drain_prompts() == ("格式化完成",)


@pytest.mark.asyncio
async def test_async_shell_does_not_block_and_timeout_only_logs(tmp_path: Path, capsys) -> None:
    target = tmp_path / "async.txt"
    async_command = (
        f'"{sys.executable}" -c "import time; from pathlib import Path; time.sleep(.2); '
        f"Path(r'{target}').write_text('done')\""
    )
    slow_command = f'"{sys.executable}" -c "import time; time.sleep(1)"'
    engine = HookEngine(
        (
            _rule(
                "background",
                HookEvent.POST_TOOL_USE,
                HookAction("shell", {"command": async_command}),
                async_run=True,
            ),
            _rule(
                "timeout",
                HookEvent.SESSION_START,
                HookAction("shell", {"command": slow_command}),
                timeout=0.05,
            ),
        )
    )
    started = time.monotonic()
    await engine.emit(HookEvent.POST_TOOL_USE, {})
    assert time.monotonic() - started < 0.1
    await asyncio.sleep(0.5)
    assert target.read_text() == "done"
    await engine.emit(HookEvent.SESSION_START, {})
    assert "timeout after 0.05s" in capsys.readouterr().err
    await engine.close()


@pytest.mark.asyncio
async def test_shell_block_stable_payload_failure_and_subagent_placeholder(capsys) -> None:
    command = (
        f'"{sys.executable}" -c "import sys; data=sys.stdin.read(); '
        "print('blocked' if data.startswith('{') else 'bad', file=sys.stderr); sys.exit(2)\""
    )
    engine = HookEngine(
        (
            _rule("guard", HookEvent.PRE_TOOL_USE, HookAction("shell", {"command": command})),
            _rule(
                "future",
                HookEvent.SESSION_START,
                HookAction("subagent", {"agent_name": "foo", "prompt": "test"}),
            ),
        )
    )
    decision = await engine.emit(
        HookEvent.PRE_TOOL_USE,
        {"tool_input": {}, "tool_name": "bash", "cwd": ".", "session_id": "s"},
    )
    await engine.emit(HookEvent.SESSION_START, {})
    assert decision.blocked and decision.hook_name == "guard" and decision.reason == "blocked"
    assert "[hook subagent] not yet implemented, skipped: future" in capsys.readouterr().err


class FakeHTTPClient:
    responses: list[httpx.Response] = []
    requests: list[tuple[str, str, bytes]] = []

    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        self.requests.append((method, url, kwargs["content"]))
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_http_block_default_json_and_template_failure_isolated(monkeypatch, capsys) -> None:
    monkeypatch.setattr("tuicodingagent.hooks.engine.httpx.AsyncClient", FakeHTTPClient)
    FakeHTTPClient.responses = [
        httpx.Response(200, json={"decision": "block", "reason": "network policy"})
    ]
    FakeHTTPClient.requests = []
    block = _rule(
        "network",
        HookEvent.PRE_TOOL_USE,
        HookAction("http", {"url": "https://example.test/check", "method": "POST"}),
    )
    broken = _rule(
        "bad-template",
        HookEvent.STOP,
        HookAction("http", {"url": "https://example.test/done", "body": "{value.bad}"}),
    )
    engine = HookEngine((block, broken))
    decision = await engine.emit(HookEvent.PRE_TOOL_USE, {"tool_name": "bash"})
    await engine.emit(HookEvent.STOP, {"value": {}})

    sent = json.loads(FakeHTTPClient.requests[0][2])
    assert sent["event"] == "PreToolUse"
    assert decision.blocked and decision.reason == "network policy"
    assert "bad-template" in capsys.readouterr().err


class ToolProvider:
    config = ProviderConfig("fake", "openai", "model", "secret")

    def __init__(self) -> None:
        self.calls = 0
        self.requests: list[tuple[object, tuple[ChatMessage, ...], tuple[str, ...]]] = []

    async def stream(
        self,
        system_prompt,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        self.requests.append((system_prompt, tuple(messages), tuple(item.name for item in tools)))
        self.calls += 1
        if self.calls == 1:
            yield ProviderEvent(
                ProviderEventType.TOOL_CALL,
                tool_call=ToolCall("write", "write_file", {"path": "blocked.txt", "content": "no"}),
            )
        else:
            yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="已根据拦截调整")

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_runtime_pretool_block_returns_tool_result_and_model_adjusts(tmp_path: Path) -> None:
    command = f'"{sys.executable}" -c "import sys; print(\'blocked\', file=sys.stderr);sys.exit(2)"'
    engine = HookEngine(
        (_rule("deny-write", HookEvent.PRE_TOOL_USE, HookAction("shell", {"command": command})),)
    )
    provider = ToolProvider()
    runtime = ConversationRuntime(
        provider,
        tool_registry=create_default_registry(tmp_path),
        workspace=tmp_path,
        permission_manager=PermissionManager(tmp_path, mode=PermissionMode.BYPASS),
        hook_engine=engine,
    )
    events = [event async for event in runtime.chat("写文件")]

    assert not (tmp_path / "blocked.txt").exists()
    finished = next(event for event in events if event.type == RuntimeEventType.TOOL_FINISHED)
    assert finished.tool_result and finished.tool_result.metadata["hook_blocked"] is True
    assert "[hook deny-write] blocked" in provider.requests[1][1][-1].tool_results[0].error
    assert events[-1].text == "已根据拦截调整"


class ReminderProvider:
    config = ProviderConfig("fake", "openai", "model", "secret")

    def __init__(self) -> None:
        self.messages: list[tuple[ChatMessage, ...]] = []

    async def stream(
        self, _prompt, messages: Sequence[ChatMessage], _tools: Sequence[ToolDefinition] = ()
    ) -> AsyncIterator[ProviderEvent]:
        self.messages.append(tuple(messages))
        yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="完成")

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_session_start_prompt_only_first_request_and_not_history(tmp_path: Path) -> None:
    engine = HookEngine(
        (
            _rule(
                "language",
                HookEvent.SESSION_START,
                HookAction("prompt", {"text": "用 zh-CN 回复"}),
                only_once=True,
            ),
        )
    )
    provider = ReminderProvider()
    runtime = ConversationRuntime(provider, hook_engine=engine, workspace=tmp_path)
    await runtime.session_start()
    _ = [event async for event in runtime.chat("一")]
    _ = [event async for event in runtime.chat("二")]

    assert "用 zh-CN 回复" in provider.messages[0][-1].content
    assert "用 zh-CN 回复" not in provider.messages[1][-1].content
    assert all("Hook reminder" not in message.content for message in runtime.history)
    await runtime.clear_and_new_session()
    _ = [event async for event in runtime.chat("三")]
    assert "用 zh-CN 回复" in provider.messages[2][-1].content
    await runtime.close()


@pytest.mark.asyncio
async def test_tui_user_prompt_block_does_not_persist(tmp_path: Path) -> None:
    command = (
        f'"{sys.executable}" -c "import sys; '
        "print('contains delete', file=sys.stderr);sys.exit(2)\""
    )
    predicate = HookPredicate(
        "all_of",
        (HookCondition("prompt", parse_match({"type": "regex", "value": "(?i)delete"})),),
    )
    engine = HookEngine(
        (
            _rule(
                "prompt-guard",
                HookEvent.USER_PROMPT_SUBMIT,
                HookAction("shell", {"command": command}),
                predicate=predicate,
            ),
        )
    )
    config = AppConfig((ProviderConfig("fake", "openai", "model", "secret"),))
    provider = CompactProvider(config.providers[0])
    app = TUICodingAgentApp(
        config,
        workspace=tmp_path,
        provider_factory=lambda _config: provider,
        hook_engine=engine,
    )
    async with app.run_test(size=(90, 24)) as pilot:
        prompt = app.query_one(PromptInput)
        prompt.focus()
        await pilot.press(
            *list("please"), "space", *list("delete"), "space", *list("file"), "enter"
        )
        await pilot.pause(0.3)
        rendered = " ".join(str(widget.render()) for widget in app.query(Static))
        assert "[hook prompt-guard] contains delete" in rendered
        assert app.runtime is not None and app.runtime.history == ()
        assert provider.main_calls == 0
