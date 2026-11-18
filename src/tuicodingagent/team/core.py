"""Team 持久化模型与生命周期管理。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .backend import BackendType, InProcessBackend, detect_backend, new_backend

INVALID_NAME = re.compile(r"[^a-z0-9._-]+")


def sanitize_name(value: str) -> str:
    result = INVALID_NAME.sub("-", value.strip().casefold()).strip("-.")
    if not result:
        raise ValueError("Team 名称清理后不能为空")
    return result[:64]


class TeamHasActiveMembersError(RuntimeError):
    pass


@dataclass(slots=True)
class TeammateInfo:
    name: str
    agent_id: str
    agent_type: str = "general-purpose"
    model: str = ""
    worktree_path: str = ""
    branch: str = ""
    backend_type: str = BackendType.IN_PROCESS.value
    pane_id: str = ""
    is_active: bool | None = True
    plan_mode_required: bool = False
    session_dir: str = ""


@dataclass(frozen=True, slots=True)
class LeadMessage:
    team_name: str
    sender: str
    text: str
    type: str
    timestamp: str


@dataclass(slots=True)
class Team:
    name: str
    sanitized_name: str
    config_dir: Path
    lead_agent_id: str = "lead"
    backend: BackendType = BackendType.IN_PROCESS
    description: str = ""
    lead_permission_mode: str = "default"
    created_at: float = field(default_factory=time.time)
    members: list[TeammateInfo] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def config_path(self) -> Path:
        return self.config_dir / "config.json"

    async def add_member(self, member: TeammateInfo) -> None:
        async with self._lock:
            self._reload_from_disk_locked()
            if any(
                item.name == member.name or item.agent_id == member.agent_id
                for item in self.members
            ):
                raise ValueError(f"Team 队员名称或 ID 已存在：{member.name}")
            self.members.append(member)
            self._save_locked()

    async def set_member_active(self, agent_id: str, active: bool) -> None:
        async with self._lock:
            self._reload_from_disk_locked()
            member = self.find_member(agent_id)
            if member is None:
                raise LookupError(f"未知 Team 队员：{agent_id}")
            member.is_active = active
            self._save_locked()

    async def set_member_pane(self, agent_id: str, pane_id: str) -> None:
        async with self._lock:
            self._reload_from_disk_locked()
            member = self.find_member(agent_id)
            if member is None:
                raise LookupError(f"未知 Team 队员：{agent_id}")
            member.pane_id = pane_id
            self._save_locked()

    async def remove_member(self, agent_id: str) -> None:
        async with self._lock:
            self._reload_from_disk_locked()
            self.members = [item for item in self.members if item.agent_id != agent_id]
            self._save_locked()

    def find_member(self, name_or_id: str) -> TeammateInfo | None:
        return next(
            (item for item in self.members if name_or_id in {item.name, item.agent_id}),
            None,
        )

    def save_initial(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        (self.config_dir / "mailbox").mkdir(exist_ok=True)
        self._save_locked()

    def _reload_from_disk_locked(self) -> None:
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        self.members = [TeammateInfo(**item) for item in raw.get("members", [])]

    def _save_locked(self) -> None:
        payload = {
            "name": self.name,
            "sanitized_name": self.sanitized_name,
            "lead_agent_id": self.lead_agent_id,
            "backend": self.backend.value,
            "description": self.description,
            "lead_permission_mode": self.lead_permission_mode,
            "created_at": self.created_at,
            "members": [asdict(item) for item in self.members],
        }
        temporary = self.config_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.config_path)


class AgentNameRegistry:
    def __init__(self) -> None:
        import threading

        self._lock = threading.Lock()
        self.by_name: dict[str, str] = {}
        self.by_id: dict[str, str] = {}

    def register(self, name: str, agent_id: str) -> None:
        with self._lock:
            old_id = self.by_name.get(name)
            if old_id:
                self.by_id.pop(old_id, None)
            old_name = self.by_id.get(agent_id)
            if old_name:
                self.by_name.pop(old_name, None)
            self.by_name[name] = agent_id
            self.by_id[agent_id] = name

    def unregister(self, name: str) -> None:
        with self._lock:
            agent_id = self.by_name.pop(name, None)
            if agent_id:
                self.by_id.pop(agent_id, None)

    def resolve(self, name_or_id: str) -> str | None:
        with self._lock:
            if name_or_id in self.by_id:
                return name_or_id
            return self.by_name.get(name_or_id)

    def name_of(self, agent_id: str) -> str | None:
        with self._lock:
            return self.by_id.get(agent_id)

    def claim(self, name: str) -> str:
        normalized = sanitize_name(name)
        with self._lock:
            candidate, suffix = normalized, 2
            while candidate in self.by_name:
                candidate = f"{normalized}-{suffix}"
                suffix += 1
            # spawn 成功后由 register 写入真实 ID；占位避免并发重名。
            self.by_name[candidate] = ""
            return candidate

    def release(self, name: str) -> None:
        self.unregister(name)


class Manager:
    def __init__(
        self,
        root: Path | None = None,
        *,
        in_process_backend: InProcessBackend | None = None,
        cleanup_member=None,
        registry: AgentNameRegistry | None = None,
    ) -> None:
        self.root = root or Path.home() / ".tuicodingagent" / "teams"
        self.root.mkdir(parents=True, exist_ok=True)
        self.teams: dict[str, Team] = {}
        self._lock = asyncio.Lock()
        self.in_process_backend = in_process_backend or InProcessBackend()
        self.registry = registry or AgentNameRegistry()
        self.cleanup_member = cleanup_member
        self.lead_permission_mode = "default"
        self._scan()

    async def create(self, name: str, description: str = "") -> Team:
        async with self._lock:
            base = sanitize_name(name)
            slug, suffix = base, 2
            while slug in self.teams or (self.root / slug).exists():
                slug, suffix = f"{base}-{suffix}", suffix + 1
            team = Team(
                name=name,
                sanitized_name=slug,
                config_dir=self.root / slug,
                description=description,
                backend=detect_backend(),
                lead_permission_mode=self.lead_permission_mode,
            )
            team.members.append(
                TeammateInfo(
                    "lead",
                    "lead",
                    agent_type="lead",
                    backend_type=team.backend.value,
                    is_active=None,
                )
            )
            team.save_initial()
            self.teams[slug] = team
            self.registry.register("lead", "lead")
            return team

    def get(self, name: str) -> Team | None:
        slug = sanitize_name(name)
        return self.teams.get(slug) or next(
            (team for team in self.teams.values() if team.name == name), None
        )

    async def delete(self, name: str, force: bool = False) -> None:
        team = self.get(name)
        if team is None:
            raise LookupError(f"未知 Team：{name}")
        async with team._lock:
            team._reload_from_disk_locked()
            if not force and any(item.is_active is not False for item in team.members):
                raise TeamHasActiveMembersError("Team 仍有活跃队员，使用 --force 强制删除")
            members = tuple(team.members)
        for member in members:
            backend = new_backend(BackendType(member.backend_type), self.in_process_backend)
            try:
                await backend.kill(member.pane_id, member.agent_id)
            except (OSError, RuntimeError) as exc:
                print(f"停止 Team 队员失败 {member.name}：{exc}", file=sys.stderr)
            try:
                if self.cleanup_member is not None:
                    await self.cleanup_member(member)
                elif member.session_dir:
                    shutil.rmtree(member.session_dir, ignore_errors=True)
            except (OSError, RuntimeError) as exc:
                print(f"清理 Team 队员资源失败 {member.name}：{exc}", file=sys.stderr)
            self.registry.unregister(member.name)
        shutil.rmtree(team.config_dir, ignore_errors=True)
        async with self._lock:
            self.teams.pop(team.sanitized_name, None)

    def find_member(self, name: str) -> tuple[Team, TeammateInfo] | None:
        for team in self.teams.values():
            member = team.find_member(name)
            if member is not None:
                return team, member
        return None

    def attach_lead_runtime(self, runtime):
        from .delivery import LeadMailboxDelivery

        delivery = LeadMailboxDelivery(runtime, self)
        delivery.bind()
        return delivery

    async def poll_lead_mailboxes(self, *, mark_read: bool = False) -> list[LeadMessage]:
        from .mailbox import Mailbox

        found: list[LeadMessage] = []
        for team in tuple(self.teams.values()):
            mailbox = Mailbox(team.config_dir / "mailbox")
            messages = await mailbox.read(team.lead_agent_id)
            if not messages:
                continue
            if mark_read:
                # 保留显式旧版本兼容；TUI 轮询默认只查看，不提前确认。
                await mailbox.mark_read(team.lead_agent_id, list(range(len(messages))))
            found.extend(
                LeadMessage(
                    team.name,
                    message.sender,
                    message.text,
                    message.type,
                    message.timestamp,
                )
                for message in messages
            )
        return found

    def _scan(self) -> None:
        for directory in self.root.iterdir():
            path = directory / "config.json"
            if not directory.is_dir() or not path.is_file():
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                backend = BackendType(raw.get("backend", BackendType.IN_PROCESS.value))
                members = [TeammateInfo(**item) for item in raw.get("members", [])]
                if backend == BackendType.IN_PROCESS:
                    for item in members:
                        item.is_active = False
                else:
                    for item in members:
                        if item.agent_id == "lead":
                            continue
                        item.is_active = _pane_is_alive(backend, item.pane_id)
                team = Team(
                    name=raw["name"],
                    sanitized_name=raw["sanitized_name"],
                    config_dir=directory,
                    lead_agent_id=raw.get("lead_agent_id", "lead"),
                    backend=backend,
                    description=raw.get("description", ""),
                    lead_permission_mode=raw.get("lead_permission_mode", "default"),
                    created_at=float(raw.get("created_at", time.time())),
                    members=members,
                )
                team._save_locked()
                self.teams[team.sanitized_name] = team
                for member in team.members:
                    self.registry.register(member.name, member.agent_id)
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                print(f"跳过无效 Team 目录 {directory}：{exc}", file=sys.stderr)


def _pane_is_alive(backend: BackendType, pane_id: str) -> bool:
    if not pane_id:
        return False
    command = (
        ("tmux", "list-panes", "-a", "-F", "#{pane_id}")
        if backend == BackendType.TMUX
        else ("it2", "list-panes")
    )
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and pane_id in result.stdout.splitlines()


__all__ = [
    "AgentNameRegistry",
    "LeadMessage",
    "Manager",
    "Team",
    "TeamHasActiveMembersError",
    "TeammateInfo",
    "sanitize_name",
]
