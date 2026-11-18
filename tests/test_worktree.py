from __future__ import annotations

import os
import subprocess
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tuicodingagent.models import (
    ChatMessage,
    PermissionMode,
    ProviderConfig,
    ProviderEvent,
    ProviderEventType,
    ToolCall,
)
from tuicodingagent.runtime import ConversationRuntime
from tuicodingagent.subagent import Catalog, Definition
from tuicodingagent.subagent.manager import Manager as TaskManager
from tuicodingagent.subagent.service import Service
from tuicodingagent.tools import create_default_registry, with_cwd
from tuicodingagent.worktree import (
    ExitAction,
    ExitOptions,
    Manager,
    WorktreeHasChangesError,
    validate_slug,
)

CONFIG = ProviderConfig("fake", "openai", "model", "secret")


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(root), *args),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout.strip()


def make_repo(root: Path) -> Path:
    root.mkdir()
    git(root, "init")
    git(root, "config", "user.email", "e2e@example.com")
    git(root, "config", "user.name", "TUICodingAgent Test")
    (root / ".gitignore").write_text(
        "config.yaml\n.env\nnode_modules/\n.tuicodingagent/settings.local.yaml\n"
        ".tuicodingagent/worktrees/\n.tuicodingagent/worktree_session.json\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text("MAIN\n", encoding="utf-8")
    (root / "server.py").write_text("VALUE = 'main'\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")
    return root


@pytest.mark.parametrize("name", ["../etc", "..", "a//b", "a/b ", "/a", "a/"])
def test_validate_slug_rejects_unsafe_names(name: str) -> None:
    with pytest.raises(ValueError):
        validate_slug(name)


def test_validate_slug_accepts_nested_name() -> None:
    assert validate_slug("feature/a") == "feature/a"


@pytest.mark.asyncio
async def test_create_setup_fast_recovery_and_remove_guard(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    local = repo / ".tuicodingagent" / "settings.local.yaml"
    local.parent.mkdir()
    local.write_text("permissions: {}\n", encoding="utf-8")
    (repo / "config.yaml").write_text("providers: []\n", encoding="utf-8")
    (repo / ".env").write_text("TOKEN=test\n", encoding="utf-8")
    (repo / ".husky").mkdir()
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "marker.txt").write_text("shared\n", encoding="utf-8")
    (repo / ".worktreeinclude").write_text("*.env\n", encoding="utf-8")
    git(repo, "add", ".worktreeinclude")
    git(repo, "commit", "-m", "include rules")

    manager = Manager(repo)
    item = await manager.create("team/alice", manual=True)
    assert item.path == repo / ".tuicodingagent" / "worktrees" / "team+alice"
    assert item.branch == "worktree-team+alice"
    assert (item.path / ".tuicodingagent" / "settings.local.yaml").is_file()
    assert (item.path / "config.yaml").read_text(encoding="utf-8") == "providers: []\n"
    assert (item.path / ".env").read_text(encoding="utf-8") == "TOKEN=test\n"
    linked_modules = item.path / "node_modules"
    assert linked_modules.samefile(repo / "node_modules")
    assert (item.path / "node_modules" / "marker.txt").read_text(encoding="utf-8") == "shared\n"
    assert git(item.path, "config", "--get", "core.hooksPath") == str((repo / ".husky").resolve())

    # 清空内存映射后，已有目录必须走纯文件系统恢复路径。
    manager.active.clear()
    recovered = await manager.create("team/alice", manual=True)
    assert recovered.head_commit == item.head_commit

    cwd = Path.cwd()
    session = await manager.enter("team/alice")
    assert Path.cwd() == cwd
    assert Path(session.worktree_path) == item.path
    (item.path / "README.md").write_text("CHANGED\n", encoding="utf-8")
    with pytest.raises(WorktreeHasChangesError):
        await manager.exit("team/alice", ExitAction.REMOVE)
    assert item.path.is_dir()
    await manager.exit("team/alice", ExitAction.REMOVE, ExitOptions(discard_changes=True))
    assert not item.path.exists()
    assert git(repo, "branch", "--list", item.branch) == ""


@pytest.mark.asyncio
async def test_session_restore_gone_and_auto_cleanup(tmp_path: Path, capsys) -> None:
    repo = make_repo(tmp_path / "repo")
    manager = Manager(repo)
    temporary = await manager.create("agent-a1234567", manual=False)
    await manager.enter(temporary.name)
    restored = Manager(repo)
    assert restored.current_session is not None
    git(repo, "worktree", "remove", "--force", str(temporary.path))
    git(repo, "branch", "-D", temporary.branch)
    restored = Manager(repo)
    assert restored.current_session is None
    assert "session worktree gone" in capsys.readouterr().err

    clean = await restored.create("agent-a7654321", manual=False)
    report = await restored.auto_cleanup(clean.name)
    assert not report.kept and not clean.path.exists()

    manual = await restored.create("manual", manual=True)
    report = await restored.auto_cleanup(manual.name)
    assert report.kept and manual.path.exists()
    await restored.remove(manual.name, ExitOptions(discard_changes=True))


@pytest.mark.asyncio
async def test_sweep_stale_only_removes_safe_temporary_worktrees(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-u", "origin", "HEAD")
    manager = Manager(repo)

    removable = await manager.create("agent-a1111111", manual=False)
    changed = await manager.create("agent-a2222222", manual=False)
    committed = await manager.create("agent-a3333333", manual=False)
    current = await manager.create("agent-a4444444", manual=False)
    ordinary = await manager.create("ordinary", manual=False)
    (changed.path / "changed.txt").write_text("changed\n", encoding="utf-8")
    (committed.path / "commit.txt").write_text("commit\n", encoding="utf-8")
    git(committed.path, "add", "commit.txt")
    git(committed.path, "commit", "-m", "unpushed")
    await manager.enter(current.name)

    old = (datetime.now() - timedelta(days=2)).timestamp()
    for item in (removable, changed, committed, current, ordinary):
        os.utime(item.path, (old, old))
    removed = await manager.sweep_stale(datetime.now() - timedelta(days=1))
    assert removed == ["agent-a1111111"]
    assert not removable.path.exists()
    assert all(item.path.exists() for item in (changed, committed, current, ordinary))

    await manager.exit(current.name)
    for item in (changed, committed, current, ordinary):
        await manager.remove(item.name, ExitOptions(discard_changes=True))


@pytest.mark.asyncio
async def test_all_core_tools_use_explicit_cwd(tmp_path: Path) -> None:
    root = tmp_path / "root"
    child = tmp_path / "child"
    root.mkdir()
    child.mkdir()
    registry = create_default_registry(root)
    with with_cwd(child):
        written = await registry.execute(
            ToolCall("w", "write_file", {"path": "sample.txt", "content": "alpha\n"})
        )
        read = await registry.execute(ToolCall("r", "read_file", {"path": "sample.txt"}))
        edited = await registry.execute(
            ToolCall(
                "e",
                "edit_file",
                {"path": "sample.txt", "old_text": "alpha", "new_text": "beta"},
            )
        )
        globbed = await registry.execute(ToolCall("g", "glob", {"pattern": "*.txt", "path": "."}))
        grepped = await registry.execute(ToolCall("p", "grep", {"pattern": "beta", "path": "."}))
        command = await registry.execute(
            ToolCall("b", "bash", {"command": 'python -c "import os;print(os.getcwd())"'})
        )
    assert all(item.success for item in (written, read, edited, globbed, grepped, command))
    assert (child / "sample.txt").read_text(encoding="utf-8") == "beta\n"
    assert str(child.resolve()) in command.output
    assert not (root / "sample.txt").exists()


class IsolationProvider:
    config = CONFIG

    def __init__(self) -> None:
        self.round = 0
        self.requests: list[tuple[object, tuple[ChatMessage, ...]]] = []

    async def stream(self, prompt, messages, tools) -> AsyncIterator[ProviderEvent]:
        self.requests.append((prompt, tuple(messages)))
        self.round += 1
        if self.round == 1:
            yield ProviderEvent(
                ProviderEventType.TOOL_CALL,
                tool_call=ToolCall(
                    "write",
                    "write_file",
                    {"path": "server.py", "content": "VALUE = 'child'\n"},
                ),
            )
        else:
            yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="ISOLATED_OK")

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_subagent_worktree_isolation_keeps_changed_copy(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    registry = create_default_registry(repo)
    parent = ConversationRuntime(IsolationProvider(), workspace=repo)
    task_manager = TaskManager()
    worktrees = Manager(repo)
    catalog = Catalog(repo, user_root=tmp_path / "missing")
    catalog.load_all()
    child_provider = IsolationProvider()
    service = Service(
        workspace=repo,
        registry=registry,
        catalog=catalog,
        manager=task_manager,
        parent_runtime=parent,
        provider_factory=lambda _: child_provider,
        providers=(CONFIG,),
        worktree_manager=worktrees,
    )
    definition = Definition(
        "isolated",
        "isolated",
        "执行隔离修改",
        permission_mode=PermissionMode.DONT_ASK,
        isolation="worktree",
    )
    catalog._definitions["isolated"] = definition
    launched = await service.launch(
        prompt="修改 server.py",
        description="隔离修改",
        subagent_type="isolated",
    )
    assert isinstance(launched, str)
    assert "ISOLATED_OK" in launched and "Worktree 保留在" in launched
    assert (repo / "server.py").read_text(encoding="utf-8") == "VALUE = 'main'\n"
    kept = next(item for item in worktrees.list() if not item.manual)
    assert (kept.path / "server.py").read_text(encoding="utf-8") == "VALUE = 'child'\n"
    await worktrees.remove(kept.name, ExitOptions(discard_changes=True))
