"""模型任务生命周期与 SQLite 消息租约的桥接。"""

from __future__ import annotations

import asyncio

from ..errors import ProviderError
from ..models import PermissionMode
from .mailbox import Mailbox, Message


class MailboxDelivery:
    def __init__(
        self,
        runtime,
        mailbox: Mailbox,
        agent_id: str,
        *,
        permission_mode="default",
        ack_timeout=30.0,
        apply_approval=True,
    ) -> None:
        self.runtime = runtime
        self.mailbox = mailbox
        self.agent_id = agent_id
        self.permission_mode = permission_mode
        self.ack_timeout = ack_timeout
        self.apply_approval = apply_approval
        self.claimed: list[Message] = []
        self._heartbeat: asyncio.Task | None = None
        self._lease_lost = False
        self._stop = asyncio.Event()
        self._lease_lock = asyncio.Lock()
        self._loss_event = asyncio.Event()

    def bind(self) -> None:
        self.runtime.set_reminder_provider(self.provide)
        self.runtime.set_delivery_callbacks(
            self.commit, self.release, persist=True, validate=self.validate, watch=self.wait_lost
        )

    async def provide(self) -> str:
        await self.validate()
        messages = await self.mailbox.claim(self.agent_id, limit=100, ack_timeout=self.ack_timeout)
        if not messages:
            return ""
        self.claimed.extend(messages)
        if self._heartbeat is None:
            self._stop.clear()
            self._heartbeat = asyncio.create_task(self._renew())
        for message in messages:
            if self.apply_approval and message.type == "plan_approval_response":
                self.runtime.set_permission_mode(
                    PermissionMode(self.permission_mode) if message.approve else PermissionMode.PLAN
                )
        rows = [f"收到 {len(messages)} 条新消息:"]
        rows.extend(
            f"[{index}] 来自 {message.sender}(type={message.type},id={message.message_id}):\n"
            f"    {message.text}"
            for index, message in enumerate(messages, 1)
        )
        return "<incoming-messages>\n" + "\n".join(rows) + "\n</incoming-messages>"

    async def _renew(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.ack_timeout / 3)
                return
            except TimeoutError:
                pass
            try:
                await self.validate()
            except Exception:
                self._lease_lost = True
                self._loss_event.set()
                return

    async def wait_lost(self) -> None:
        await self._loss_event.wait()
        raise ProviderError("团队消息租约失效，停止运行中的工具")

    async def validate(self) -> None:
        async with self._lease_lock:
            if self._lease_lost:
                raise ProviderError("团队消息租约已失效，禁止继续执行旧任务")
            if not self.claimed:
                return
            try:
                accepted = await self.mailbox.settle_claims(
                    self.agent_id, list(self.claimed), "renew", ack_timeout=self.ack_timeout
                )
            except Exception as exc:
                self._lease_lost = True
                self._loss_event.set()
                raise ProviderError("无法验证团队消息租约，暂停执行") from exc
            if len(accepted) != len(self.claimed):
                self._lease_lost = True
                self._loss_event.set()
                raise ProviderError("团队消息租约已失效，禁止继续执行旧任务")

    async def _stop_heartbeat(self) -> None:
        if self._heartbeat is not None:
            # 不取消 to_thread 中的续租事务，避免旧线程在确认之后改写租约。
            self._stop.set()
            await asyncio.gather(self._heartbeat, return_exceptions=True)
            self._heartbeat = None

    async def commit(self) -> None:
        await self._stop_heartbeat()
        if self._lease_lost:
            raise ProviderError("团队消息租约已失效，需要重新领取")
        accepted = await self.mailbox.settle_claims(self.agent_id, self.claimed, "ack")
        if self._lease_lost or len(accepted) != len(self.claimed):
            raise ProviderError("团队消息租约已失效，不能确认其他消费者领取的消息")
        self.claimed.clear()
        self._lease_lost = False
        self._loss_event.clear()

    async def release(self) -> None:
        await self._stop_heartbeat()
        await self.mailbox.settle_claims(self.agent_id, self.claimed, "release")
        self.claimed.clear()
        self._lease_lost = False
        self._loss_event.clear()


class LeadMailboxDelivery:
    """Lead 动态汇集多个 Team，输入存档和确认仍由 Runtime 生命周期驱动。"""

    def __init__(self, runtime, manager) -> None:
        self.runtime = runtime
        self.manager = manager
        self.deliveries: dict[str, MailboxDelivery] = {}

    def bind(self) -> None:
        self.runtime.set_reminder_provider(self.provide)
        self.runtime.set_delivery_callbacks(
            self.commit, self.release, persist=True, validate=self.validate, watch=self.wait_lost
        )

    async def provide(self) -> str:
        sections = []
        for team in tuple(self.manager.teams.values()):
            key = str(team.config_dir)
            if key not in self.deliveries:
                self.deliveries[key] = MailboxDelivery(
                    self.runtime,
                    Mailbox(team.config_dir / "mailbox"),
                    team.lead_agent_id,
                    apply_approval=False,
                )
            content = await self.deliveries[key].provide()
            if content:
                sections.append(f"[Team {team.name}]\n{content}")
        return "<team-update>\n" + "\n".join(sections) + "\n</team-update>" if sections else ""

    async def commit(self) -> None:
        for delivery in self.deliveries.values():
            if delivery.claimed:
                await delivery.commit()

    async def validate(self) -> None:
        for delivery in self.deliveries.values():
            if delivery.claimed:
                await delivery.validate()

    async def wait_lost(self) -> None:
        tasks = [
            asyncio.create_task(delivery.wait_lost())
            for delivery in self.deliveries.values()
            if delivery.claimed
        ]
        try:
            if not tasks:
                await asyncio.Event().wait()
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            await next(iter(done))
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def release(self) -> None:
        for delivery in self.deliveries.values():
            if delivery.claimed:
                await delivery.release()
