"""内存型后台 SubAgent 任务管理。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ..models import PermissionChoice, RuntimeEventType, ToolCall

ApprovalHandler = Callable[[str, ToolCall, str], Awaitable[PermissionChoice]]
NotificationSink = Callable[[str], None]


@dataclass(slots=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(slots=True)
class BackgroundTask:
    id: str
    name: str
    runtime: object
    task: str
    status: str = "running"
    result: str = ""
    err: BaseException | None = None
    start_time: float = field(default_factory=time.time)
    end_time: float | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    tool_count: int = 0
    last_activity: str = ""
    handle: asyncio.Task[None] | None = None
    notify: bool = True
    launch_status: str = "async_launched"


class Manager:
    def __init__(self, notification_sink: NotificationSink | None = None) -> None:
        self.tasks: dict[str, BackgroundTask] = {}
        self.done: asyncio.Queue[str] = asyncio.Queue()
        self.notification_sink = notification_sink
        self.approval_handler: ApprovalHandler | None = None
        self._counter = 0

    def launch(self, runtime, task: str, *, name: str = "", notify: bool = True) -> BackgroundTask:
        self._counter += 1
        identifier = f"task-{self._counter:04d}"
        item = BackgroundTask(identifier, name, runtime, task, notify=notify)
        self.tasks[identifier] = item
        item.handle = asyncio.create_task(self._drive(item))
        item.handle.add_done_callback(lambda handle: self._finalize_early_cancel(item, handle))
        return item

    def _finalize_early_cancel(self, item: BackgroundTask, handle: asyncio.Task[None]) -> None:
        """处理协程尚未获得首个时间片就被取消的边界。"""

        if not handle.cancelled() or item.status != "running":
            return
        item.status = "cancelled"
        item.err = asyncio.CancelledError()
        item.end_time = time.time()
        self.done.put_nowait(item.id)
        if item.notify and self.notification_sink is not None:
            self.notification_sink(self.notification(item))

    async def run_inline(self, runtime, task: str, *, name: str = "") -> str:
        item = BackgroundTask("inline", name, runtime, task)
        await self._consume(item)
        if item.err is not None:
            raise RuntimeError(str(item.err)) from item.err
        return item.result

    async def _drive(self, item: BackgroundTask) -> None:
        try:
            await self._consume(item)
            item.status = "completed"
        except asyncio.CancelledError as exc:
            item.status = "cancelled"
            item.err = exc
        except BaseException as exc:  # 后台边界必须保护主应用
            item.status = "failed"
            item.err = exc
        finally:
            item.end_time = time.time()
            await self.done.put(item.id)
            if item.notify and self.notification_sink is not None:
                self.notification_sink(self.notification(item))

    async def _consume(self, item: BackgroundTask) -> None:
        chunks: list[str] = []
        async for event in item.runtime.chat(item.task):
            if event.type == RuntimeEventType.TEXT_DELTA:
                chunks.append(event.text)
            elif event.type == RuntimeEventType.TOOL_STARTED and event.tool_call:
                item.tool_count += 1
                item.last_activity = event.tool_call.name
            elif event.type == RuntimeEventType.USAGE:
                item.usage.input_tokens = event.total_input_tokens
                item.usage.output_tokens = event.total_output_tokens
            elif event.type == RuntimeEventType.PERMISSION_REQUESTED and event.tool_call:
                if self.approval_handler is None:
                    raise RuntimeError(f"子 Agent 等待权限审批：{event.text}")
                choice = await self.approval_handler(
                    item.name or item.id, event.tool_call, event.text
                )
                item.runtime.resolve_permission(event.tool_call.id, choice)
            elif event.type == RuntimeEventType.COMPLETED:
                item.result = event.text or "".join(chunks)
            elif event.type == RuntimeEventType.FAILED:
                raise RuntimeError(event.text)
            elif event.type == RuntimeEventType.CANCELLED:
                raise asyncio.CancelledError
        if not item.result:
            item.result = "".join(chunks).strip() or "SubAgent 已完成，但没有返回文本。"

    def list(self) -> tuple[BackgroundTask, ...]:
        return tuple(self.tasks.values())

    def get(self, task_id: str) -> BackgroundTask | None:
        return self.tasks.get(task_id)

    def stop(self, task_id: str) -> bool:
        item = self.get(task_id)
        if item is None or item.handle is None or item.handle.done():
            return False
        item.handle.cancel()
        return True

    def find_name(self, name: str) -> BackgroundTask | None:
        matches = [item for item in self.tasks.values() if item.name == name]
        return matches[-1] if matches else None

    async def send_message(self, name: str, message: str) -> BackgroundTask:
        previous = self.find_name(name)
        if previous is None or previous.status != "completed":
            raise ValueError(f"没有可续派的后台 Agent：{name}")
        return self.launch(previous.runtime, message, name=name)

    def notification(self, item: BackgroundTask) -> str:
        result = item.result if item.status == "completed" else str(item.err or "未知错误")
        return (
            "<task-notification>\n"
            f'Task {item.id} (name="{item.name}"): {item.status}\n'
            f"Result: {result}\n"
            "</task-notification>"
        )

    async def close(self) -> None:
        handles = [
            item.handle for item in self.tasks.values() if item.handle and not item.handle.done()
        ]
        for handle in handles:
            handle.cancel()
        if handles:
            await asyncio.gather(*handles, return_exceptions=True)
        providers = {
            id(item.runtime.provider): item.runtime.provider for item in self.tasks.values()
        }
        if providers:
            await asyncio.gather(
                *(provider.close() for provider in providers.values()),
                return_exceptions=True,
            )


__all__ = ["BackgroundTask", "Manager", "TokenUsage"]
