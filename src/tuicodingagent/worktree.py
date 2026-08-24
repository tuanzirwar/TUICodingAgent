"""安全、可恢复的 Git Worktree 生命周期管理。"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

SLUG_SEGMENT = re.compile(r"^[a-zA-Z0-9._-]+$")
TEMP_NAME = re.compile(r"^agent-a[0-9a-f]{7}$")
LARGE_DIRECTORIES = ("node_modules", ".venv", "vendor")


class WorktreeError(RuntimeError):
    pass


class WorktreeHasChangesError(WorktreeError):
    pass


class ExitAction(StrEnum):
    KEEP = "keep"
    REMOVE = "remove"


@dataclass(frozen=True, slots=True)
class ExitOptions:
    discard_changes: bool = False


@dataclass(frozen=True, slots=True)
class Worktree:
    name: str
    path: Path
    branch: str
    based_on: str
    head_commit: str
    created: datetime
    manual: bool


@dataclass(frozen=True, slots=True)
class WorktreeSession:
    original_cwd: str
    worktree_path: str
    worktree_name: str
    original_branch: str
    original_head_commit: str
    session_id: str
    hook_based: bool = False


@dataclass(frozen=True, slots=True)
class ExitReport:
    removed: bool
    path: str
    branch: str


@dataclass(frozen=True, slots=True)
class AutoCleanupReport:
    kept: bool
    path: str = ""
    branch: str = ""


def validate_slug(name: str) -> str:
    if not name:
        raise ValueError("Worktree 名称不能为空")
    if len(name) > 64:
        raise ValueError("Worktree 名称不能超过 64 个字符")
    if name.startswith("/") or name.endswith("/") or "//" in name:
        raise ValueError("Worktree 名称不能以 / 开头或结尾，也不能包含连续 //")
    for segment in name.split("/"):
        if segment in {".", ".."}:
            raise ValueError("Worktree 名称不能包含 . 或 .. 段")
        if not SLUG_SEGMENT.fullmatch(segment):
            raise ValueError(f"Worktree 名称段包含非法字符：{segment!r}")
    return name


class Manager:
    def __init__(self, repo_root: str | Path) -> None:
        requested = Path(repo_root).resolve()
        result = _git_sync(requested, "rev-parse", "--show-toplevel")
        try:
            actual = Path(result.stdout.strip()).resolve()
        except (OSError, ValueError) as exc:
            raise WorktreeError("当前目录不是 Git 仓库") from exc
        if result.returncode != 0 or actual != requested:
            raise WorktreeError("WorktreeManager 必须使用 Git 仓库根目录")
        self.repo_root = requested
        self.worktree_dir = requested / ".tuicodingagent" / "worktrees"
        self.session_file = requested / ".tuicodingagent" / "worktree_session.json"
        try:
            self.worktree_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorktreeError(f"无法创建 Worktree 目录：{exc}") from exc
        self.lock = asyncio.Lock()
        self.active: dict[str, Worktree] = {}
        self.current_session = self._load_session()
        self._creating: set[str] = set()
        self._scan_existing()
        self._warn_gitignore()

    async def create(self, name: str, base_ref: str = "HEAD", *, manual: bool = False) -> Worktree:
        validate_slug(name)
        flat = name.replace("/", "+")
        path = (self.worktree_dir / flat).resolve()
        branch = f"worktree-{flat}"
        async with self.lock:
            if name in self.active or name in self._creating:
                raise WorktreeError(f"Worktree 已存在：{name}")
            if path.exists():
                recovered = self._recover(name, path, branch, base_ref, manual)
                self.active[name] = recovered
                return recovered
            self._creating.add(name)
        try:
            result = await _git(
                self.repo_root,
                "worktree",
                "add",
                "-B",
                branch,
                str(path),
                base_ref,
            )
            if result.returncode != 0:
                if path.exists():
                    shutil.rmtree(path, ignore_errors=True)
                raise WorktreeError(result.stderr.strip() or "git worktree add 失败")
            await self._post_creation(path)
            head = await self._git_value(path, "rev-parse", "HEAD")
            item = Worktree(name, path, branch, base_ref, head, datetime.now(), manual)
            async with self.lock:
                self.active[name] = item
            return item
        finally:
            async with self.lock:
                self._creating.discard(name)

    async def enter(self, name: str) -> WorktreeSession:
        async with self.lock:
            item = self._require(name)
        branch = await self._git_value(self.repo_root, "branch", "--show-current")
        head = await self._git_value(self.repo_root, "rev-parse", "HEAD")
        async with self.lock:
            if self.active.get(name) != item:
                raise WorktreeError("Worktree 状态已变化，请重试")
            session = WorktreeSession(
                original_cwd=str(Path.cwd().resolve()),
                worktree_path=str(item.path),
                worktree_name=name,
                original_branch=branch,
                original_head_commit=head,
                session_id=str(uuid4()),
            )
            self.current_session = session
            self._persist_session(session)
            return session

    async def exit(
        self,
        name: str,
        action: ExitAction = ExitAction.KEEP,
        options: ExitOptions | None = None,
    ) -> ExitReport:
        options = options or ExitOptions()
        async with self.lock:
            item = self._require(name)
            session = self.current_session
            if session is None or session.worktree_name != name:
                raise WorktreeError("只能退出当前进入的 Worktree")
        if (
            action == ExitAction.REMOVE
            and not options.discard_changes
            and await self.has_changes(item)
        ):
            raise WorktreeHasChangesError("Worktree 含未提交修改或新增提交，拒绝删除")
        original = Path(session.original_cwd)
        if await asyncio.to_thread(original.is_dir):
            os.chdir(original)
        async with self.lock:
            self.current_session = None
            self._persist_session(None)
        if action == ExitAction.REMOVE:
            await self._remove_item(item)
        return ExitReport(action == ExitAction.REMOVE, str(item.path), item.branch)

    async def remove(self, name: str, options: ExitOptions | None = None) -> ExitReport:
        options = options or ExitOptions()
        async with self.lock:
            item = self._require(name)
            if self.current_session and self.current_session.worktree_name == name:
                raise WorktreeError("请先退出当前 Worktree")
        if not options.discard_changes and await self.has_changes(item):
            raise WorktreeHasChangesError("Worktree 含未提交修改或新增提交，拒绝删除")
        await self._remove_item(item)
        return ExitReport(True, str(item.path), item.branch)

    async def auto_cleanup(self, name: str) -> AutoCleanupReport:
        async with self.lock:
            item = self._require(name)
        if item.manual or await self.has_changes(item):
            return AutoCleanupReport(True, str(item.path), item.branch)
        await self.remove(name, ExitOptions(discard_changes=True))
        return AutoCleanupReport(False)

    def list(self) -> tuple[Worktree, ...]:
        return tuple(sorted(self.active.values(), key=lambda item: item.name))

    async def has_changes(self, item: Worktree) -> bool:
        status = await _git(item.path, "status", "--porcelain")
        if status.returncode != 0 or status.stdout.strip():
            return True
        commits = await _git(item.path, "rev-list", "--count", f"{item.head_commit}..HEAD")
        if commits.returncode != 0:
            return True
        try:
            return int(commits.stdout.strip() or "0") > 0
        except ValueError:
            return True

    async def sweep_stale(self, cutoff: datetime) -> list[str]:
        removed: list[str] = []
        for path in tuple(self.worktree_dir.iterdir()):
            if not path.is_dir() or not TEMP_NAME.fullmatch(path.name):
                continue
            if datetime.fromtimestamp(path.stat().st_mtime) > cutoff:
                continue
            if self.current_session and Path(self.current_session.worktree_path) == path.resolve():
                continue
            item = self.active.get(path.name)
            if item is None or await self.has_changes(item):
                continue
            unpushed = await _git(path, "rev-list", "--max-count=1", "HEAD", "--not", "--remotes")
            if unpushed.returncode != 0 or unpushed.stdout.strip():
                continue
            await self.remove(item.name, ExitOptions(discard_changes=True))
            removed.append(item.name)
        return removed

    async def _remove_item(self, item: Worktree) -> None:
        result = await _git(self.repo_root, "worktree", "remove", "--force", str(item.path))
        if result.returncode != 0:
            raise WorktreeError(result.stderr.strip() or "git worktree remove 失败")
        await asyncio.sleep(0.1)
        branch = await _git(self.repo_root, "branch", "-D", item.branch)
        if branch.returncode != 0:
            print(f"删除 Worktree 分支失败：{branch.stderr.strip()}", file=sys.stderr)
        async with self.lock:
            self.active.pop(item.name, None)

    def _require(self, name: str) -> Worktree:
        try:
            return self.active[name]
        except KeyError as exc:
            raise WorktreeError(f"未知 Worktree：{name}") from exc

    async def _post_creation(self, path: Path) -> None:
        steps = (
            self._copy_local_config,
            self._configure_hooks,
            self._link_large_directories,
            self._copy_included_files,
        )
        for step in steps:
            try:
                await step(path)
            except Exception as exc:
                print(f"Worktree 初始化警告（{step.__name__}）：{exc}", file=sys.stderr)

    async def _copy_local_config(self, path: Path) -> None:
        for relative in (
            Path("config.yaml"),
            Path(".tuicodingagent/config.yaml"),
            Path(".tuicodingagent/settings.local.yaml"),
        ):
            source, target = self.repo_root / relative, path / relative
            if source.is_file() and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(shutil.copy2, source, target)

    async def _configure_hooks(self, path: Path) -> None:
        result = await _git(self.repo_root, "config", "--get", "core.hooksPath")
        hooks = result.stdout.strip() if result.returncode == 0 else ""
        if not hooks and (self.repo_root / ".husky").is_dir():
            hooks = str((self.repo_root / ".husky").resolve())
        elif hooks and not Path(hooks).is_absolute():
            hooks = str((self.repo_root / hooks).resolve())
        if hooks:
            await _git(path, "config", "core.hooksPath", hooks)

    async def _link_large_directories(self, path: Path) -> None:
        for name in LARGE_DIRECTORIES:
            source, target = self.repo_root / name, path / name
            if source.is_dir() and not target.exists():
                await asyncio.to_thread(_link_directory, source, target)

    async def _copy_included_files(self, path: Path) -> None:
        include = self.repo_root / ".worktreeinclude"
        if not include.is_file():
            return
        patterns = [
            line.strip()
            for line in include.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        listed = await _git(
            self.repo_root,
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
        )
        if listed.returncode != 0:
            raise WorktreeError(listed.stderr.strip())
        for relative_text in listed.stdout.splitlines():
            relative = Path(relative_text)
            if not any(fnmatch.fnmatch(relative.as_posix(), pattern) for pattern in patterns):
                continue
            source, target = self.repo_root / relative, path / relative
            if source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(shutil.copy2, source, target)

    def _recover(
        self,
        name: str,
        path: Path,
        branch: str,
        based_on: str,
        manual: bool,
    ) -> Worktree:
        head, recovered_branch = _read_worktree_head(path)
        return Worktree(
            name,
            path,
            recovered_branch or branch,
            based_on,
            head,
            datetime.fromtimestamp(path.stat().st_mtime),
            manual,
        )

    def _scan_existing(self) -> None:
        for path in self.worktree_dir.iterdir():
            if not path.is_dir() or not (path / ".git").is_file():
                continue
            name = path.name.replace("+", "/")
            try:
                self.active[name] = self._recover(
                    name, path.resolve(), f"worktree-{path.name}", "HEAD", False
                )
            except (OSError, ValueError):
                continue

    def _load_session(self) -> WorktreeSession | None:
        try:
            raw = json.loads(self.session_file.read_text(encoding="utf-8") or "null")
            if raw is None:
                return None
            session = WorktreeSession(**raw)
            if not Path(session.worktree_path).is_dir():
                print("session worktree gone, cleared", file=sys.stderr)
                with suppress(OSError):
                    self._persist_session(None)
                return None
            return session
        except FileNotFoundError:
            return None
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            print(f"Worktree session 无效，已清空：{exc}", file=sys.stderr)
            with suppress(OSError):
                self._persist_session(None)
            return None

    def _persist_session(self, session: WorktreeSession | None) -> None:
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.session_file.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(asdict(session) if session else None, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, self.session_file)

    def _warn_gitignore(self) -> None:
        try:
            text = (self.repo_root / ".gitignore").read_text(encoding="utf-8")
        except OSError:
            text = ""
        missing = [
            item
            for item in (
                ".tuicodingagent/worktrees/",
                ".tuicodingagent/worktree_session.json",
            )
            if item not in text
        ]
        if missing:
            print(".gitignore 未忽略 Worktree 运行文件：" + "、".join(missing), file=sys.stderr)

    async def _git_value(self, cwd: Path, *args: str) -> str:
        result = await _git(cwd, *args)
        if result.returncode != 0:
            raise WorktreeError(result.stderr.strip() or f"git {' '.join(args)} 失败")
        return result.stdout.strip()


def build_worktree_notice(parent_cwd: str, worktree_path: str) -> str:
    return f"""<worktree-context>
你当前在一个独立的 Git Worktree 副本中工作，与父 Agent 隔离。
- 父目录: {parent_cwd}
- 你的工作目录: {worktree_path}
- 父 Agent 提到的绝对路径基于父目录，你需要替换前缀为本地路径后再读写
- 编辑文件前，必须先在本地 Worktree 重新 read_file，避免使用过时内容
</worktree-context>"""


async def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    def run() -> subprocess.CompletedProcess[str]:
        environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}
        return subprocess.run(
            ("git", "-C", str(cwd), *args),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            check=False,
        )

    return await asyncio.to_thread(run)


def _git_sync(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", "-C", str(cwd), *args),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _read_worktree_head(path: Path) -> tuple[str, str]:
    pointer = (path / ".git").read_text(encoding="utf-8").strip()
    if not pointer.startswith("gitdir:"):
        raise ValueError("Worktree .git 指针无效")
    gitdir = Path(pointer.split(":", 1)[1].strip()).resolve()
    common_text = (gitdir / "commondir").read_text(encoding="utf-8").strip()
    common = (gitdir / common_text).resolve()
    head_text = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
    if not head_text.startswith("ref: "):
        return head_text, ""
    ref = head_text[5:]
    ref_path = common / ref
    if ref_path.is_file():
        head = ref_path.read_text(encoding="utf-8").strip()
    else:
        head = ""
        packed = common / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.endswith(f" {ref}"):
                    head = line.split(" ", 1)[0]
                    break
    if not head:
        raise ValueError("无法只读恢复 Worktree HEAD")
    return head, ref.removeprefix("refs/heads/")


def _link_directory(source: Path, target: Path) -> None:
    """优先创建符号链接；Windows 无权限时使用等价的目录 Junction。"""

    try:
        os.symlink(source, target, target_is_directory=True)
        return
    except OSError as original:
        if os.name != "nt":
            raise
        link_error = original
    result = subprocess.run(
        ("cmd", "/d", "/s", "/c", "mklink", "/J", str(target), str(source)),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0 or not target.is_dir():
        raise link_error


__all__ = [
    "AutoCleanupReport",
    "ExitAction",
    "ExitOptions",
    "ExitReport",
    "Manager",
    "Worktree",
    "WorktreeError",
    "WorktreeHasChangesError",
    "WorktreeSession",
    "build_worktree_notice",
    "validate_slug",
]
