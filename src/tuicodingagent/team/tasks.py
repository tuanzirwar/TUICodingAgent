"""Team 共享任务图及跨进程安全持久化。"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from uuid import uuid4

from .mailbox import Mailbox


@dataclass(slots=True)
class SharedTask:
    id: str
    title: str
    description: str = ""
    status: str = "pending"
    assignee: str = ""
    blocked_by: list[str] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    revision: int = 0

    @property
    def subject(self) -> str:
        return self.title

    @property
    def owner(self) -> str:
        return self.assignee


class TaskStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = path.with_name("tasks.lock")
        self.lock = asyncio.Lock()

    async def create(
        self,
        title: str,
        description: str = "",
        assignee: str = "",
        blocked_by: list[str] | None = None,
    ) -> SharedTask:
        if not title.strip():
            raise ValueError("共享任务标题不能为空")
        async with self.lock:
            Mailbox._acquire(self.lock_path)
            try:
                tasks = self._load()
                blockers = list(dict.fromkeys(blocked_by or []))
                for blocker in blockers:
                    if blocker not in tasks:
                        raise LookupError(f"未知依赖任务：{blocker}")
                task = SharedTask(
                    f"task_{uuid4().hex[:6]}",
                    title.strip(),
                    description,
                    assignee=assignee,
                    blocked_by=blockers,
                )
                tasks[task.id] = task
                for blocker in blockers:
                    if task.id not in tasks[blocker].blocks:
                        tasks[blocker].blocks.append(task.id)
                        tasks[blocker].updated_at = time.time()
                self._save(tasks)
                return task
            finally:
                self.lock_path.unlink(missing_ok=True)

    async def update(
        self,
        task_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        assignee: str | None = None,
        owner: str | None = None,
        add_blocks: list[str] | None = None,
        add_blocked_by: list[str] | None = None,
        remove_blocks: list[str] | None = None,
        remove_blocked_by: list[str] | None = None,
        actor: str | None = None,
        actor_aliases: tuple[str, ...] = (),
        expected_revision: int | None = None,
    ) -> SharedTask:
        async with self.lock:
            Mailbox._acquire(self.lock_path)
            try:
                tasks = self._load()
                if task_id not in tasks:
                    raise LookupError(f"未知共享任务：{task_id}")
                task = tasks[task_id]
                if expected_revision is not None and (
                    isinstance(expected_revision, bool)
                    or not isinstance(expected_revision, int)
                    or expected_revision != task.revision
                ):
                    raise ValueError("任务 revision 已变化，请重新读取后更新")
                aliases = {actor, *actor_aliases}
                selected_assignee = assignee if assignee is not None else owner
                if actor is not None:
                    if task.assignee and task.assignee not in aliases:
                        raise ValueError("任务已分配给其他队员，不能修改或领取")
                    if selected_assignee is not None and selected_assignee not in aliases:
                        raise ValueError("队员不能将任务重新分配给其他队员")
                    if task.status == "completed" and status in {
                        "pending",
                        "in_progress",
                        "blocked",
                    }:
                        raise ValueError("完成任务需要由 Lead 明确重新开启")
                    if status == "in_progress" and not task.assignee:
                        selected_assignee = actor
                if status is not None:
                    if status not in {
                        "pending",
                        "in_progress",
                        "completed",
                        "blocked",
                    }:
                        raise ValueError("任务状态无效")
                    task.status = status
                if title is not None:
                    if not title.strip():
                        raise ValueError("共享任务标题不能为空")
                    task.title = title.strip()
                if description is not None:
                    task.description = description
                if selected_assignee is not None:
                    task.assignee = selected_assignee
                self._change_edges(
                    tasks,
                    task,
                    add_blocks or [],
                    add_blocked_by or [],
                    remove_blocks or [],
                    remove_blocked_by or [],
                )
                self._validate_graph(tasks)
                if task.status in {"in_progress", "completed"} and any(
                    tasks[key].status != "completed" for key in task.blocked_by
                ):
                    raise ValueError("依赖任务尚未完成，当前任务不能启动或完成")
                task.updated_at = time.time()
                task.revision += 1
                self._save(tasks)
                return task
            finally:
                self.lock_path.unlink(missing_ok=True)

    def get(self, task_id: str) -> SharedTask | None:
        return self._load().get(task_id)

    def list(self, status: str | None = None) -> list[dict[str, object]]:
        if status is not None and status not in {
            "pending",
            "in_progress",
            "completed",
            "blocked",
        }:
            raise ValueError("任务状态无效")
        tasks = self._load()
        result: list[dict[str, object]] = []
        for task in tasks.values():
            if status is not None and task.status != status:
                continue
            item = asdict(task)
            item["is_ready"] = all(
                tasks.get(key) is not None and tasks[key].status == "completed"
                for key in task.blocked_by
            )
            result.append(item)
        return result

    def _change_edges(
        self,
        tasks: dict[str, SharedTask],
        task: SharedTask,
        add_blocks: list[str],
        add_blocked_by: list[str],
        remove_blocks: list[str],
        remove_blocked_by: list[str],
    ) -> None:
        changes = (*add_blocks, *add_blocked_by, *remove_blocks, *remove_blocked_by)
        for other_id in changes:
            if other_id not in tasks:
                raise LookupError(f"未知依赖任务：{other_id}")
            if other_id == task.id:
                raise ValueError("任务不能依赖自身")
        for blocked_id in add_blocks:
            self._link(task, tasks[blocked_id])
        for blocker_id in add_blocked_by:
            self._link(tasks[blocker_id], task)
        for blocked_id in remove_blocks:
            self._unlink(task, tasks[blocked_id])
        for blocker_id in remove_blocked_by:
            self._unlink(tasks[blocker_id], task)

    @staticmethod
    def _validate_graph(tasks: dict[str, SharedTask]) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(identifier: str) -> None:
            if identifier in visiting:
                raise ValueError("共享任务依赖不能形成环")
            if identifier in visited:
                return
            visiting.add(identifier)
            for dependency in tasks[identifier].blocked_by:
                visit(dependency)
            visiting.remove(identifier)
            visited.add(identifier)

        for identifier in tasks:
            visit(identifier)
        for task in tasks.values():
            if task.status in {"in_progress", "completed"} and any(
                tasks[key].status != "completed" for key in task.blocked_by
            ):
                raise ValueError("已有执行中或完成任务依赖当前任务，不能使其依赖失效")

    @staticmethod
    def _link(blocker: SharedTask, blocked: SharedTask) -> None:
        if blocked.id not in blocker.blocks:
            blocker.blocks.append(blocked.id)
        if blocker.id not in blocked.blocked_by:
            blocked.blocked_by.append(blocker.id)

    @staticmethod
    def _unlink(blocker: SharedTask, blocked: SharedTask) -> None:
        blocker.blocks = [item for item in blocker.blocks if item != blocked.id]
        blocked.blocked_by = [item for item in blocked.blocked_by if item != blocker.id]

    def _load(self) -> dict[str, SharedTask]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        entries = raw.get("tasks", []) if isinstance(raw, dict) else raw
        return {item["id"]: SharedTask(**item) for item in entries}

    def _save(self, tasks: dict[str, SharedTask]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        Mailbox._atomic_write(self.path, {"tasks": [asdict(value) for value in tasks.values()]})


__all__ = ["SharedTask", "TaskStore"]
