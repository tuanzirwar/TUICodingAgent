"""协议无关、可取消的 ReAct Agent Loop。"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from pathlib import Path

from .context import (
    MANUAL_SAFETY_MARGIN,
    CompactResult,
    ContextController,
    is_prompt_too_long,
)
from .errors import ProviderError
from .hooks import HookDispatch, HookEngine, HookEvent
from .memory import MemoryManager, explicit_memory_content, should_update_memory
from .models import (
    AgentMode,
    ChatMessage,
    PermissionChoice,
    PermissionDecision,
    PermissionMode,
    ProviderEventType,
    RuntimeEvent,
    RuntimeEventType,
    ToolCall,
    ToolResult,
)
from .permissions import PermissionManager
from .prompts import build_system_prompt, inject_system_reminder, reminder_for
from .providers.base import ChatProvider
from .session import RestoredSession, SessionContext, Writer
from .tools.registry import ToolRegistry
from .trace import TraceEvent, TraceRecorder, TracingProvider, token_estimate, utc_now
from .verification import VerificationRunner

MAX_AGENT_ITERATIONS = 12
MAX_UNKNOWN_TOOL_ROUNDS = 3
SYSTEM_SKILL_TOOLS = frozenset({"load_skill", "install_skill"})

DO_PROMPT = "请根据上文已经制定的计划立即开始执行，使用必要工具完成任务并验证结果。"


class _AgentCancelled(Exception):
    """内部协作式取消信号。"""


class ConversationRuntime:
    def __init__(
        self,
        provider: ChatProvider,
        system_prompt: str | None = None,
        tool_registry: ToolRegistry | None = None,
        workspace: Path | None = None,
        permission_manager: PermissionManager | None = None,
        context_controller: ContextController | None = None,
        instructions: str = "",
        memory_manager: MemoryManager | None = None,
        session_writer: Writer | None = None,
        hook_engine: HookEngine | None = None,
        max_iterations: int | None = None,
        allowed_tools: frozenset[str] | None = None,
        blocked_tool_errors: dict[str, str] | None = None,
        permission_source: str = "",
        verification_runner: VerificationRunner | None = None,
    ) -> None:
        self.trace_recorder = TraceRecorder.for_workspace((workspace or Path.cwd()).resolve())
        self.trace_session_id = uuid.uuid4().hex
        self.agent_id = os.getenv("TUICODINGAGENT_AGENT_ID", "lead")
        self.provider = (
            provider
            if isinstance(provider, TracingProvider)
            else TracingProvider(provider, self.trace_recorder)
        )
        self.system_prompt_override = system_prompt
        self.tool_registry = tool_registry
        self.workspace = (workspace or Path.cwd()).resolve()
        self.permission_manager = permission_manager
        self.context_controller = context_controller
        self.instructions = instructions
        self.memory_manager = memory_manager
        self.session_writer = session_writer
        self.hook_engine = hook_engine
        self.max_iterations = max_iterations or MAX_AGENT_ITERATIONS
        self.allowed_tools = allowed_tools
        self.blocked_tool_errors = blocked_tool_errors or {}
        self.permission_source = permission_source
        self.verification_runner = verification_runner
        if self.verification_runner is None and workspace is not None:
            self.verification_runner = VerificationRunner(self.workspace)
        self.mode = AgentMode.EXECUTE
        self._history: list[ChatMessage] = []
        self._busy = False
        self._cancel_requested = False
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cache_creation_input_tokens = 0
        self.total_cache_read_input_tokens = 0
        self._permission_future: asyncio.Future[PermissionChoice] | None = None
        self._permission_call_id = ""
        self._session_lock = asyncio.Lock()
        self._reminder_provider: Callable[[], Awaitable[str]] | None = None
        self.turn_count = 0
        self._memory_tasks: set[asyncio.Task[bool]] = set()
        self.skill_catalog = ""
        self.active_skills: dict[str, str] = {}
        self._skill_tool_allowlists: dict[str, frozenset[str]] = {}
        self._external_reminders: list[str] = []

    @property
    def history(self) -> tuple[ChatMessage, ...]:
        return tuple(self._history)

    def set_mode(self, mode: AgentMode) -> None:
        self.mode = mode

    def set_skill_catalog(self, catalog: str) -> None:
        self.skill_catalog = catalog

    def activate_skill(self, name: str, body: str, tools: Sequence[str] = ()) -> None:
        """激活 Skill，并记录其工具可见范围。"""

        self.active_skills[name] = body
        self._skill_tool_allowlists[name] = frozenset(tools)

    def clear_active_skills(self) -> None:
        self.active_skills.clear()
        self._skill_tool_allowlists.clear()

    def seed_history(self, messages: Sequence[ChatMessage]) -> None:
        """仅供隔离的 fork 运行时注入上下文快照。"""

        if self._busy:
            raise RuntimeError("运行中不能替换历史")
        self._history = list(messages)

    def append_external_result(self, text: str) -> None:
        """把独立 Skill 的最终摘要回流主历史并持久化。"""

        message = ChatMessage("assistant", text)
        self._history.append(message)
        if self.session_writer is not None:
            self.session_writer.append(message)

    def queue_reminder(self, text: str) -> None:
        """把后台通知放入下一次模型请求，不污染持久历史。"""

        self._external_reminders.append(text)

    def set_reminder_provider(self, provider: Callable[[], Awaitable[str]] | None) -> None:
        """注册每轮模型调用前的异步 reminder 来源。"""

        self._reminder_provider = provider

    async def run_to_completion(self, task: str) -> str:
        """非交互跑完整个 ReAct 循环并返回最终文本。"""

        trace = TraceEvent(
            "agent_task",
            session_id=self.trace_session_id,
            agent_id=self.agent_id,
            model=self.provider.config.model,
            request_bytes=len(task.encode("utf-8")),
        )
        chunks: list[str] = []
        try:
            async for event in self.chat(task):
                if event.type == RuntimeEventType.TEXT_DELTA:
                    chunks.append(event.text)
                elif event.type == RuntimeEventType.COMPLETED:
                    result = event.text or "".join(chunks)
                    trace.response_bytes = len(result.encode("utf-8"))
                    self.trace_recorder.record(trace.finish())
                    return result
                elif event.type == RuntimeEventType.FAILED:
                    raise RuntimeError(event.text)
                elif event.type == RuntimeEventType.CANCELLED:
                    raise asyncio.CancelledError
                elif event.type == RuntimeEventType.PERMISSION_REQUESTED:
                    raise RuntimeError(f"子 Agent 等待权限审批：{event.text}")
        except BaseException as exc:
            self.trace_recorder.record(trace.finish(success=False, error=exc))
            raise
        result = "".join(chunks)
        trace.response_bytes = len(result.encode("utf-8"))
        self.trace_recorder.record(trace.finish())
        return result

    async def run_hook(self, event: HookEvent, **extra: object) -> HookDispatch:
        if self.hook_engine is None:
            return HookDispatch()
        payload: dict[str, object] = {
            "session_id": self._session_id(),
            "cwd": str(self.workspace),
            "mode": self.permission_mode.value,
            **extra,
        }
        return await self.hook_engine.emit(event, payload)

    async def session_start(self) -> None:
        await self.run_hook(HookEvent.SESSION_START)

    async def session_end(self) -> None:
        await self.run_hook(HookEvent.SESSION_END)

    async def session_resume(self) -> None:
        if self.hook_engine is not None:
            self.hook_engine.reset_session()
        await self.run_hook(HookEvent.SESSION_RESUME)

    @property
    def permission_mode(self) -> PermissionMode:
        if self.permission_manager is None:
            return PermissionMode.BYPASS
        return self.permission_manager.mode

    def set_permission_mode(self, mode: PermissionMode) -> None:
        if self.permission_manager is not None:
            self.permission_manager.set_mode(mode)
        self.mode = AgentMode.PLAN if mode == PermissionMode.PLAN else AgentMode.EXECUTE

    def resolve_permission(self, call_id: str, choice: PermissionChoice) -> None:
        future = self._permission_future
        if future is not None and not future.done() and call_id == self._permission_call_id:
            future.set_result(choice)

    def request_cancel(self) -> None:
        self._cancel_requested = True
        future = self._permission_future
        if future is not None and not future.done():
            future.set_result(PermissionChoice.CANCEL)

    async def chat(self, user_text: str) -> AsyncIterator[RuntimeEvent]:
        text = user_text.strip()
        if not text:
            raise ValueError("消息不能为空")
        if self._busy:
            raise RuntimeError("上一轮回复仍在生成中")
        await self._session_lock.acquire()
        self._busy = True
        self._cancel_requested = False
        if isinstance(self.provider, TracingProvider):
            session_id = self._session_id() or self.trace_session_id
            self.trace_session_id = session_id
            self.provider.set_context(session_id, self.agent_id)
        if self.tool_registry is not None:
            self.tool_registry.set_trace_context(
                self.trace_session_id, self.agent_id, self.provider.config.model
            )
        started = time.monotonic()
        turn_trace = TraceEvent(
            "turn",
            session_id=self.trace_session_id,
            agent_id=self.agent_id,
            model=self.provider.config.model,
            request_bytes=len(text.encode("utf-8")),
        )
        turn_error = ""
        turn_messages = [ChatMessage("user", text)]
        persisted_count = 0
        current_calls: list[ToolCall] = []
        assistant_staged = False
        unknown_rounds = 0
        modified_paths: set[Path] = set()
        verification_attempts = 0
        try:
            yield RuntimeEvent(RuntimeEventType.STARTED, elapsed=0.0)
            persisted_count = self._persist_new(turn_messages, persisted_count)
            for iteration in range(1, self.max_iterations + 1):
                self._raise_if_cancelled()
                yield RuntimeEvent(
                    RuntimeEventType.ITERATION_STARTED,
                    elapsed=time.monotonic() - started,
                    iteration=iteration,
                )
                round_text: list[str] = []
                current_calls = []
                current_provider_items: list[dict[str, object]] = []
                assistant_staged = False
                prompt = await build_system_prompt(
                    self.workspace,
                    self.provider.config,
                    self.instructions,
                    self.memory_manager.index_text if self.memory_manager else "",
                    self.skill_catalog,
                    self.active_skills,
                )
                if self.memory_manager is not None and self.memory_manager.index_text:
                    memory = self.memory_manager.index_text
                    self.trace_recorder.record(
                        TraceEvent(
                            "retrieval",
                            session_id=self.trace_session_id,
                            agent_id=self.agent_id,
                            response_bytes=len(memory.encode("utf-8")),
                            output_tokens=token_estimate(memory),
                            metadata={"source": "long_term_memory_index"},
                        ).finish()
                    )
                if self.system_prompt_override is not None:
                    prompt = type(prompt)(self.system_prompt_override, prompt.environment)
                definitions = self._definitions()
                if self.context_controller is not None:
                    managed_history, _ = await self.context_controller.offload_and_snip(
                        self._history
                    )
                    managed_turn, _ = await self.context_controller.offload_and_snip(turn_messages)
                    self._history = list(managed_history)
                    turn_messages = list(managed_turn)
                    combined = tuple((*self._history, *turn_messages))
                    if await self.context_controller.should_auto_compact(combined):
                        yield RuntimeEvent(
                            RuntimeEventType.CONTEXT_STATUS,
                            text="正在压缩上下文...",
                            iteration=iteration,
                        )
                        compacted = await self._compact(combined, definitions, "auto")
                        if compacted.success:
                            self._history = []
                            turn_messages = list(compacted.messages)
                            if self.session_writer is not None:
                                self.session_writer.replace(turn_messages)
                            persisted_count = len(turn_messages)
                            status = (
                                f"已压缩，token 从 {compacted.before_tokens} "
                                f"降至 {compacted.after_tokens}"
                            )
                        else:
                            status = f"上下文自动压缩失败：{compacted.error}"
                        yield RuntimeEvent(
                            RuntimeEventType.CONTEXT_STATUS,
                            text=status,
                            iteration=iteration,
                        )
                await self.run_hook(HookEvent.PRE_USER_MESSAGE, prompt=text)
                reminder = reminder_for(self.mode, iteration)
                if self._reminder_provider is not None:
                    supplied = await self._reminder_provider()
                    if supplied:
                        reminder += "\n\n" + supplied
                if self._external_reminders:
                    reminder += "\n\n" + "\n\n".join(self._external_reminders)
                    self._external_reminders.clear()
                if self.hook_engine is not None:
                    injected = self.hook_engine.drain_prompts()
                    if injected:
                        reminder += "\n\n" + "\n\n".join(
                            f"[Hook reminder]\n{item}" for item in injected
                        )
                request_messages = inject_system_reminder(
                    [*self._history, *turn_messages], reminder
                )
                emergency_retried = False
                while True:
                    try:
                        async for event in self.provider.stream(
                            prompt,
                            request_messages,
                            definitions,
                        ):
                            self._raise_if_cancelled()
                            if event.type == ProviderEventType.TEXT_DELTA:
                                round_text.append(event.text)
                                yield _text_event(event.text, started, iteration)
                            elif event.type == ProviderEventType.TOOL_CALL and event.tool_call:
                                current_calls.append(event.tool_call)
                            elif (
                                event.type == ProviderEventType.PROVIDER_ITEM
                                and event.provider_item is not None
                            ):
                                current_provider_items.append(event.provider_item)
                            elif event.type == ProviderEventType.USAGE:
                                self.total_input_tokens += event.input_tokens
                                self.total_output_tokens += event.output_tokens
                                self.total_cache_creation_input_tokens += (
                                    event.cache_creation_input_tokens
                                )
                                self.total_cache_read_input_tokens += event.cache_read_input_tokens
                                if self.context_controller is not None:
                                    self.context_controller.update_usage(
                                        request_messages,
                                        input_tokens=event.input_tokens,
                                        output_tokens=event.output_tokens,
                                        cache_creation_input_tokens=(
                                            event.cache_creation_input_tokens
                                        ),
                                        cache_read_input_tokens=event.cache_read_input_tokens,
                                    )
                                yield RuntimeEvent(
                                    RuntimeEventType.USAGE,
                                    elapsed=time.monotonic() - started,
                                    iteration=iteration,
                                    input_tokens=event.input_tokens,
                                    output_tokens=event.output_tokens,
                                    total_input_tokens=self.total_input_tokens,
                                    total_output_tokens=self.total_output_tokens,
                                    cache_creation_input_tokens=(event.cache_creation_input_tokens),
                                    cache_read_input_tokens=event.cache_read_input_tokens,
                                    total_cache_creation_input_tokens=(
                                        self.total_cache_creation_input_tokens
                                    ),
                                    total_cache_read_input_tokens=(
                                        self.total_cache_read_input_tokens
                                    ),
                                )
                        break
                    except ProviderError as exc:
                        if (
                            self.context_controller is None
                            or emergency_retried
                            or not is_prompt_too_long(exc)
                        ):
                            raise
                        emergency_retried = True
                        yield RuntimeEvent(
                            RuntimeEventType.CONTEXT_STATUS,
                            text="上下文撞墙，自动压缩中...",
                            iteration=iteration,
                        )
                        combined = tuple((*self._history, *turn_messages))
                        compacted = await self._compact(
                            combined, definitions, "emergency", force_offload=True
                        )
                        retry_limit = self.context_controller.context_window - MANUAL_SAFETY_MARGIN
                        if not compacted.success or compacted.after_tokens >= retry_limit:
                            detail = compacted.error or "压缩后仍超出安全阈值"
                            raise ProviderError(f"紧急上下文压缩失败：{detail}") from exc
                        self._history = []
                        turn_messages = list(compacted.messages)
                        if self.session_writer is not None:
                            self.session_writer.replace(turn_messages)
                        persisted_count = len(turn_messages)
                        request_messages = inject_system_reminder(turn_messages, reminder)
                        round_text.clear()
                        current_calls.clear()
                        yield RuntimeEvent(
                            RuntimeEventType.CONTEXT_STATUS,
                            text=(
                                f"上下文撞墙后已压缩，token 从 {compacted.before_tokens} "
                                f"降至 {compacted.after_tokens}"
                            ),
                            iteration=iteration,
                        )

                complete_text = "".join(round_text)
                if not current_calls or self.tool_registry is None:
                    if modified_paths and self.verification_runner is not None:
                        verification = await self.verification_runner.run(modified_paths)
                        self.trace_recorder.record(
                            TraceEvent(
                                "verification",
                                session_id=self.trace_session_id,
                                agent_id=self.agent_id,
                                model=self.provider.config.model,
                                response_bytes=len(verification.output.encode("utf-8")),
                                metadata={"steps": verification.steps},
                            ).finish(success=verification.success)
                        )
                        if not verification.success and verification_attempts < 2:
                            verification_attempts += 1
                            turn_messages.append(ChatMessage("assistant", complete_text))
                            turn_messages.append(
                                ChatMessage(
                                    "user",
                                    "<verification-failed>\n"
                                    + verification.output
                                    + "\n请修复后重新验证。",
                                )
                            )
                            persisted_count = self._persist_new(
                                turn_messages, persisted_count
                            )
                            continue
                        if not verification.success:
                            complete_text += "\n\n自动验证未通过：\n" + verification.output
                    turn_messages.append(
                        ChatMessage(
                            "assistant",
                            complete_text,
                            provider_items=tuple(current_provider_items),
                        )
                    )
                    persisted_count = self._persist_new(turn_messages, persisted_count)
                    self._history.extend(turn_messages)
                    await self._schedule_memory_update(text, tuple(turn_messages))
                    await self.run_hook(HookEvent.STOP, iter=iteration)
                    yield _completed_event(complete_text, started, iteration)
                    return

                turn_messages.append(
                    ChatMessage(
                        "assistant",
                        complete_text,
                        tool_calls=tuple(current_calls),
                        provider_items=tuple(current_provider_items),
                    )
                )
                persisted_count = self._persist_new(turn_messages, persisted_count)
                assistant_staged = True
                results: list[ToolResult] = []
                for batch in _execution_batches(current_calls, self.tool_registry):
                    executable: list[ToolCall] = []
                    ready_results: dict[str, ToolResult] = {}
                    for call in batch:
                        yield RuntimeEvent(
                            RuntimeEventType.TOOL_STARTED,
                            elapsed=time.monotonic() - started,
                            iteration=iteration,
                            tool_call=call,
                        )
                        if call.name in self.blocked_tool_errors:
                            ready_results[call.id] = ToolResult(
                                call.id,
                                call.name,
                                False,
                                error=self.blocked_tool_errors[call.name],
                            )
                            continue
                        if not self._tool_allowed(call.name):
                            reason = self.blocked_tool_errors.get(
                                call.name, "当前子 Agent 的工具约束不允许此调用"
                            )
                            ready_results[call.id] = ToolResult(
                                call.id,
                                call.name,
                                False,
                                error=reason,
                            )
                            continue
                        hook = await self.run_hook(
                            HookEvent.PRE_TOOL_USE,
                            tool_name=call.name,
                            tool_input=call.arguments,
                        )
                        if hook.blocked:
                            reason = f"[hook {hook.hook_name}] {hook.reason}"
                            ready_results[call.id] = ToolResult(
                                call.id,
                                call.name,
                                False,
                                error=reason,
                                metadata={"hook_blocked": True},
                            )
                            continue
                        if call.name == "load_skill":
                            executable.append(call)
                            continue
                        if self.permission_manager is None:
                            executable.append(call)
                            continue
                        evaluation = self.permission_manager.evaluate(call)
                        if evaluation.decision == PermissionDecision.DENY:
                            ready_results[call.id] = _permission_denied(call, evaluation.reason)
                            continue
                        if evaluation.decision == PermissionDecision.ASK:
                            await self.run_hook(
                                HookEvent.NOTIFICATION,
                                kind="approval",
                                detail=call.name,
                            )
                            future: asyncio.Future[PermissionChoice] = (
                                asyncio.get_running_loop().create_future()
                            )
                            self._permission_future = future
                            self._permission_call_id = call.id
                            yield RuntimeEvent(
                                RuntimeEventType.PERMISSION_REQUESTED,
                                text=(
                                    f"[来自 SubAgent {self.permission_source}] {evaluation.reason}"
                                    if self.permission_source
                                    else evaluation.reason
                                ),
                                elapsed=time.monotonic() - started,
                                iteration=iteration,
                                tool_call=call,
                            )
                            try:
                                choice = await future
                            finally:
                                self._permission_future = None
                                self._permission_call_id = ""
                            if choice == PermissionChoice.CANCEL:
                                raise _AgentCancelled
                            if choice == PermissionChoice.DENY_ONCE:
                                ready_results[call.id] = _permission_denied(
                                    call, "用户拒绝了本次工具调用"
                                )
                                continue
                            if choice == PermissionChoice.ALLOW_ALWAYS:
                                self.permission_manager.allow_always(evaluation.exact_rule)
                        executable.append(call)
                    tasks = [
                        asyncio.create_task(self.tool_registry.execute(call)) for call in executable
                    ]
                    try:
                        executed = await asyncio.gather(*tasks)
                    except BaseException:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        raise
                    ready_results.update(
                        (call.id, result) for call, result in zip(executable, executed, strict=True)
                    )
                    if self.context_controller is not None:
                        for call in batch:
                            await self.context_controller.record_read(call, ready_results[call.id])
                    for call in batch:
                        result = ready_results[call.id]
                        results.append(result)
                        if result.success and call.name in {"write_file", "edit_file"}:
                            raw_path = result.metadata.get("path")
                            if isinstance(raw_path, str):
                                modified_paths.add(Path(raw_path))
                        await self.run_hook(
                            HookEvent.POST_TOOL_USE,
                            tool_name=call.name,
                            tool_input=call.arguments,
                            tool_result=result.output if result.success else result.error,
                            is_error=not result.success,
                        )
                        yield RuntimeEvent(
                            RuntimeEventType.TOOL_FINISHED,
                            elapsed=time.monotonic() - started,
                            iteration=iteration,
                            tool_call=call,
                            tool_result=result,
                        )
                    self._raise_if_cancelled()

                turn_messages.append(ChatMessage("tool", tool_results=tuple(results)))
                persisted_count = self._persist_new(turn_messages, persisted_count)
                assistant_staged = False
                only_unknown = all(
                    self.tool_registry.get(call.name) is None for call in current_calls
                )
                unknown_rounds = unknown_rounds + 1 if only_unknown else 0
                if unknown_rounds >= MAX_UNKNOWN_TOOL_ROUNDS:
                    warning = "模型连续请求未知工具，Agent Loop 已停止。"
                    turn_messages.append(ChatMessage("assistant", warning))
                    persisted_count = self._persist_new(turn_messages, persisted_count)
                    self._history.extend(turn_messages)
                    yield _limit_event(warning, started, iteration)
                    yield _completed_event(warning, started, iteration)
                    return
                if iteration == self.max_iterations:
                    warning = f"已达到 Agent Loop 最大迭代次数 {self.max_iterations}，任务已停止。"
                    turn_messages.append(ChatMessage("assistant", warning))
                    persisted_count = self._persist_new(turn_messages, persisted_count)
                    self._history.extend(turn_messages)
                    yield _limit_event(warning, started, iteration)
                    yield _completed_event(warning, started, iteration)
                    return
        except _AgentCancelled:
            turn_error = "AgentCancelled"
            self._finish_cancelled_history(turn_messages, current_calls, assistant_staged)
            self._persist_new(turn_messages, persisted_count)
            yield RuntimeEvent(
                RuntimeEventType.CANCELLED,
                text="已取消当前 Agent Loop。",
                elapsed=time.monotonic() - started,
            )
        except asyncio.CancelledError:
            turn_error = "CancelledError"
            self._finish_cancelled_history(turn_messages, current_calls, assistant_staged)
            self._persist_new(turn_messages, persisted_count)
            raise
        except ProviderError as exc:
            turn_error = type(exc).__name__
            await self.run_hook(
                HookEvent.NOTIFICATION,
                kind="stream_error",
                detail=str(exc),
            )
            if len(turn_messages) > 1:
                turn_messages.append(ChatMessage("assistant", f"模型流出错：{exc}"))
                self._persist_new(turn_messages, persisted_count)
                self._history.extend(turn_messages)
            yield RuntimeEvent(
                RuntimeEventType.FAILED,
                text=str(exc),
                elapsed=time.monotonic() - started,
            )
        except BaseException as exc:
            turn_error = type(exc).__name__
            raise
        finally:
            turn_trace.tool_latency_ms = round((time.monotonic() - started) * 1000, 3)
            turn_trace.success = not turn_error
            turn_trace.error_type = turn_error
            turn_trace.end_time = utc_now()
            self.trace_recorder.record(turn_trace)
            self._busy = False
            self._cancel_requested = False
            self._session_lock.release()

    async def force_compact(self) -> CompactResult:
        if self.context_controller is None:
            return CompactResult(False, self.history, 0, 0, error="上下文管理未启用")
        async with self._session_lock:
            definitions = self._definitions()
            result = await self._compact(self.history, definitions, "manual")
            if result.success:
                self._history = list(result.messages)
                if self.session_writer is not None:
                    self.session_writer.replace(result.messages)
            return result

    async def restore(self, restored: RestoredSession, writer: Writer) -> CompactResult | None:
        """在空闲互斥区切换到历史会话，超限时先压缩一次。"""

        async with self._session_lock:
            await self.session_end()
            old_writer = self.session_writer
            self._history = list(restored.messages)
            self.session_writer = writer
            if self.context_controller is not None:
                self.context_controller = ContextController(
                    self.workspace,
                    self.context_controller.context_window,
                    restored.info.context,
                )
                # 存档可能保留原始工具输出；恢复时先脱载并重写快照。
                managed, offloaded = await self.context_controller.offload_and_snip(
                    self._history
                )
                if offloaded:
                    self._history = list(managed)
                    writer.replace(managed)
            if old_writer is not None:
                old_writer.close()
            await self.session_resume()
            if (
                self.context_controller is None
                or not await self.context_controller.should_auto_compact(self.history)
            ):
                return None
            result = await self._compact(self.history, self._definitions(), "manual")
            if result.success:
                self._history = list(result.messages)
                writer.replace(result.messages)
            return result

    async def clear_and_new_session(self) -> SessionContext:
        """结束当前存档并建立全新的空会话。"""

        async with self._session_lock:
            await self.session_end()
            context = SessionContext.create(self.workspace)
            old_writer = self.session_writer
            self.session_writer = Writer(context, self.provider.config.model)
            if old_writer is not None:
                old_writer.close()
            window = (
                self.context_controller.context_window
                if self.context_controller is not None
                else self.provider.config.context_window
            )
            if window is not None:
                self.context_controller = ContextController(self.workspace, window, context)
            self._history = []
            self.total_input_tokens = 0
            self.total_output_tokens = 0
            self.total_cache_creation_input_tokens = 0
            self.total_cache_read_input_tokens = 0
            self.turn_count = 0
            self.clear_active_skills()
            if self.hook_engine is not None:
                self.hook_engine.reset_session()
            await self.session_start()
            return context

    def cancel_background_tasks(self) -> None:
        for task in tuple(self._memory_tasks):
            task.cancel()

    def _persist_new(self, messages: list[ChatMessage], start: int) -> int:
        if self.session_writer is not None:
            for message in messages[start:]:
                self.session_writer.append(message)
        return len(messages)

    async def _schedule_memory_update(
        self, user_text: str, turn_messages: tuple[ChatMessage, ...]
    ) -> None:
        self.turn_count += 1
        if self.memory_manager is None or not should_update_memory(self.turn_count, user_text):
            return
        explicit_text = user_text if explicit_memory_content(user_text) else None
        task = asyncio.create_task(
            self.memory_manager.update(self.provider, turn_messages, explicit_text)
        )
        self._memory_tasks.add(task)
        task.add_done_callback(self._memory_tasks.discard)
        if explicit_text:
            await task

    def _definitions(self):
        if self.tool_registry is None:
            return ()
        return self.tool_registry.filtered_definitions(
            self._effective_tool_allowlist(),
            read_only=self.mode == AgentMode.PLAN,
            system_tools=SYSTEM_SKILL_TOOLS,
        )

    def _active_tool_allowlist(self) -> frozenset[str] | None:
        constrained = [tools for tools in self._skill_tool_allowlists.values() if tools]
        if not constrained:
            return None
        allowed = set(constrained[0])
        for tools in constrained[1:]:
            allowed.intersection_update(tools)
        return frozenset(allowed)

    def _effective_tool_allowlist(self) -> frozenset[str] | None:
        skill_allowed = self._active_tool_allowlist()
        if self.allowed_tools is None:
            return skill_allowed
        if skill_allowed is None:
            return self.allowed_tools
        return self.allowed_tools.intersection(skill_allowed)

    def _tool_allowed(self, name: str) -> bool:
        allowed = self._effective_tool_allowlist()
        return name in SYSTEM_SKILL_TOOLS or allowed is None or name in allowed

    def _session_id(self) -> str:
        if self.session_writer is not None:
            return self.session_writer.context.session_id
        if self.context_controller is not None:
            return self.context_controller.session_id
        return ""

    async def _compact(
        self,
        messages: Sequence[ChatMessage],
        definitions,
        trigger: str,
        *,
        force_offload: bool = False,
    ) -> CompactResult:
        assert self.context_controller is not None
        await self.run_hook(HookEvent.PRE_COMPACT, trigger=trigger)
        trace = TraceEvent(
            "compression",
            session_id=self.trace_session_id,
            agent_id=self.agent_id,
            model=self.provider.config.model,
            retry_count=int(trigger == "emergency"),
            metadata={"trigger": trigger},
        )
        started = time.perf_counter()
        try:
            result = await self.context_controller.compact(
                self.provider,
                messages,
                definitions,
                trigger,
                force_offload=force_offload,
            )
        except BaseException as exc:
            trace.tool_latency_ms = round((time.perf_counter() - started) * 1000, 3)
            self.trace_recorder.record(trace.finish(success=False, error=exc))
            raise
        trace.input_tokens = result.before_tokens
        trace.output_tokens = result.after_tokens
        if result.success and result.messages:
            summary_content = result.messages[0].content
            trace.response_bytes = len(summary_content.encode("utf-8"))
            trace.metadata["summary_tokens"] = token_estimate(summary_content)
            trace.metadata["summary_bytes"] = trace.response_bytes
        trace.tool_latency_ms = round((time.perf_counter() - started) * 1000, 3)
        trace.metadata.update(
            {
                "offloaded": result.offloaded,
                "ratio": result.after_tokens / max(1, result.before_tokens),
            }
        )
        self.trace_recorder.record(trace.finish(success=result.success))
        await self.run_hook(
            HookEvent.POST_COMPACT,
            trigger=trigger,
            before_tokens=result.before_tokens,
            after_tokens=result.after_tokens,
        )
        return result

    def _raise_if_cancelled(self) -> None:
        if self._cancel_requested:
            raise _AgentCancelled

    def _finish_cancelled_history(
        self,
        turn_messages: list[ChatMessage],
        current_calls: Sequence[ToolCall],
        assistant_staged: bool,
    ) -> None:
        if assistant_staged:
            cancelled = tuple(
                ToolResult(call.id, call.name, False, error="工具调用已取消")
                for call in current_calls
            )
            turn_messages.append(ChatMessage("tool", tool_results=cancelled))
        turn_messages.append(ChatMessage("assistant", "当前任务已由用户取消。"))
        self._history.extend(turn_messages)

    async def close(self) -> None:
        await self.session_end()
        if self._memory_tasks:
            await asyncio.gather(*tuple(self._memory_tasks), return_exceptions=True)
        if self.session_writer is not None:
            self.session_writer.close()
        if self.hook_engine is not None:
            await self.hook_engine.close()
        await self.provider.close()


def _execution_batches(calls: Sequence[ToolCall], registry: ToolRegistry) -> list[list[ToolCall]]:
    batches: list[list[ToolCall]] = []
    read_batch: list[ToolCall] = []
    for call in calls:
        if registry.is_read_only(call.name):
            read_batch.append(call)
            continue
        if read_batch:
            batches.append(read_batch)
            read_batch = []
        batches.append([call])
    if read_batch:
        batches.append(read_batch)
    return batches


def _text_event(text: str, started: float, iteration: int) -> RuntimeEvent:
    return RuntimeEvent(
        RuntimeEventType.TEXT_DELTA,
        text=text,
        elapsed=time.monotonic() - started,
        iteration=iteration,
    )


def _completed_event(text: str, started: float, iteration: int) -> RuntimeEvent:
    return RuntimeEvent(
        RuntimeEventType.COMPLETED,
        text=text,
        elapsed=time.monotonic() - started,
        iteration=iteration,
    )


def _limit_event(text: str, started: float, iteration: int) -> RuntimeEvent:
    return RuntimeEvent(
        RuntimeEventType.LIMIT_REACHED,
        text=text,
        elapsed=time.monotonic() - started,
        iteration=iteration,
    )


def _permission_denied(call: ToolCall, reason: str) -> ToolResult:
    return ToolResult(
        call.id,
        call.name,
        False,
        error=f"权限拒绝：{reason}",
        metadata={"permission_denied": True},
    )
