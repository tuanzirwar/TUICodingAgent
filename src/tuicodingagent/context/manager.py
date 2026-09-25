"""两层上下文压缩、Token 估算与恢复状态。"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Literal

from ..errors import ProviderError
from ..models import (
    ChatMessage,
    ProviderEventType,
    SystemPrompt,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from ..providers.base import ChatProvider
from ..session import SessionContext

SINGLE_RESULT_LIMIT = 50_000
MESSAGE_AGGREGATE_LIMIT = 200_000
SUMMARY_OUTPUT_RESERVE = 20_000
AUTO_SAFETY_MARGIN = 13_000
MANUAL_SAFETY_MARGIN = 3_000
RECENT_TOKEN_MINIMUM = 10_000
RECENT_MESSAGE_MINIMUM = 5
MAX_RECENT_FILES = 5
FILE_SNAPSHOT_TOKEN_LIMIT = 5_000
AUTO_FAILURE_LIMIT = 3
DIRECT_PTL_RETRIES = 3
PTL_DROP_RATIO = 0.2
ESTIMATE_CHARS_PER_TOKEN = 3.5
PREVIEW_BYTE_LIMIT = 2_048
PREVIEW_LINE_LIMIT = 20

DEFAULT_CONTEXT_WINDOWS = {"openai": 128_000, "anthropic": 200_000}
BOUNDARY_NOTICE = (
    "边界提示：需要文件原文、错误原文或用户原话时，请使用文件读取工具重新读取，"
    "不要依据摘要猜测代码或细节。"
)
SUMMARY_PROMPT = """你正在压缩一段编程 Agent 对话。严禁调用任何工具。
先在 <analysis> 标签内写分析草稿，再在 <summary> 标签内写正式摘要；草稿会被丢弃。
正式摘要必须严格包含以下九个编号部分：
1. 主要请求和意图
2. 关键技术概念
3. 文件和代码段
4. 错误和修复
5. 问题解决过程
6. 所有用户消息原文（优先逐条、原文保留）
7. 待办任务
8. 当前工作（最详细，说明正在做什么以及停在哪一步）
9. 可能的下一步
只在 <summary> 中写可供后续 Agent 继续工作的事实。"""

logger = logging.getLogger(__name__)
CompactReason = Literal["auto", "manual", "emergency"]


@dataclass(frozen=True, slots=True)
class CompactResult:
    success: bool
    messages: tuple[ChatMessage, ...]
    before_tokens: int
    after_tokens: int
    offloaded: int = 0
    error: str = ""


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    path: str
    read_at: str
    content: str
    sequence: int


class ContextController:
    def __init__(
        self,
        workspace: Path,
        context_window: int,
        session_context: SessionContext | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.context_window = context_window
        context = session_context or SessionContext.create(self.workspace)
        self.session_id = context.session_id
        self.session_dir = context.session_dir
        self.results_dir = context.spill_dir
        try:
            self.session_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # 只读工作区不能让 Agent 启动失败；实际落盘会继续按 F1 安全降级。
            logger.warning("无法创建上下文会话目录，工具结果将保留原文：%s", type(exc).__name__)
        self.seen_ids: set[str] = set()
        self.replacements: dict[str, str] = {}
        self._ledger_lock = asyncio.Lock()
        self._files: dict[str, FileSnapshot] = {}
        self._file_lock = asyncio.Lock()
        self._file_sequence = 0
        self._auto_failures = 0
        self._breaker_lock = asyncio.Lock()
        self.usage_anchor = 0
        self._anchor_chars = 0

    @classmethod
    def for_provider(
        cls,
        workspace: Path,
        provider: ChatProvider,
        session_context: SessionContext | None = None,
    ) -> ContextController:
        configured = provider.config.context_window
        window = configured or DEFAULT_CONTEXT_WINDOWS[provider.config.protocol]
        return cls(workspace, window, session_context)

    async def offload_and_snip(
        self, messages: list[ChatMessage] | tuple[ChatMessage, ...]
    ) -> tuple[tuple[ChatMessage, ...], int]:
        """确定性改写工具结果；同一 id 的决策在会话内冻结。"""

        changed: list[ChatMessage] = []
        offloaded = 0
        async with self._ledger_lock:
            for message in messages:
                if message.role != "tool" or not message.tool_results:
                    changed.append(message)
                    continue
                results = list(message.tool_results)
                candidates: list[tuple[int, int, ToolResult, str]] = []
                for index, result in enumerate(results):
                    if result.tool_call_id in self.seen_ids:
                        replacement = self.replacements.get(result.tool_call_id)
                        if replacement is not None:
                            results[index] = _replace_result_content(result, replacement)
                        continue
                    content = _result_content(result)
                    candidates.append((len(content.encode("utf-8")), index, result, content))

                total = sum(item[0] for item in candidates)
                ordered = sorted(candidates, key=lambda item: item[0], reverse=True)
                attempted: set[str] = set()
                selected: list[tuple[int, int, ToolResult, str]] = []
                selected.extend(item for item in ordered if item[0] > SINGLE_RESULT_LIMIT)
                remaining_candidates = [item for item in ordered if item[0] <= SINGLE_RESULT_LIMIT]
                projected = total - sum(item[0] for item in selected)
                while projected > MESSAGE_AGGREGATE_LIMIT and remaining_candidates:
                    item = remaining_candidates.pop(0)
                    selected.append(item)
                    projected -= item[0]

                successful_bytes = 0
                for byte_count, index, result, content in selected:
                    attempted.add(result.tool_call_id)
                    try:
                        path = self._safe_result_path(result.tool_call_id)
                        await asyncio.to_thread(_write_once, path, content.encode("utf-8"))
                    except (OSError, UnicodeError, ValueError):
                        continue
                    preview = _preview_body(content, byte_count, path)
                    results[index] = _replace_result_content(result, preview)
                    self.replacements[result.tool_call_id] = preview
                    self.seen_ids.add(result.tool_call_id)
                    successful_bytes += byte_count
                    offloaded += 1

                # 如果必选项落盘失败，继续尝试大项直到实际剩余量达标。
                actual_remaining = total - successful_bytes
                for byte_count, index, result, content in remaining_candidates:
                    if actual_remaining <= MESSAGE_AGGREGATE_LIMIT:
                        break
                    attempted.add(result.tool_call_id)
                    try:
                        path = self._safe_result_path(result.tool_call_id)
                        await asyncio.to_thread(_write_once, path, content.encode("utf-8"))
                    except (OSError, UnicodeError, ValueError):
                        continue
                    preview = _preview_body(content, byte_count, path)
                    results[index] = _replace_result_content(result, preview)
                    self.replacements[result.tool_call_id] = preview
                    self.seen_ids.add(result.tool_call_id)
                    actual_remaining -= byte_count
                    offloaded += 1

                for _, _, result, _ in candidates:
                    if result.tool_call_id not in attempted:
                        self.seen_ids.add(result.tool_call_id)
                changed.append(replace(message, tool_results=tuple(results)))
        return tuple(changed), offloaded

    def _safe_result_path(self, tool_use_id: str) -> Path:
        path = (self.results_dir / tool_use_id).resolve(strict=False)
        if path.parent != self.results_dir.resolve(strict=False):
            raise ValueError("不安全的工具调用 id")
        return path

    def estimate_tokens(self, messages: list[ChatMessage] | tuple[ChatMessage, ...]) -> int:
        chars = _messages_chars(messages)
        incremental = max(0, chars - self._anchor_chars)
        return self.usage_anchor + math.ceil(incremental / ESTIMATE_CHARS_PER_TOKEN)

    def update_usage(
        self,
        messages: list[ChatMessage] | tuple[ChatMessage, ...],
        *,
        input_tokens: int,
        output_tokens: int,
        cache_creation_input_tokens: int,
        cache_read_input_tokens: int,
    ) -> None:
        self.usage_anchor = (
            input_tokens + output_tokens + cache_creation_input_tokens + cache_read_input_tokens
        )
        self._anchor_chars = _messages_chars(messages)

    async def auto_enabled(self) -> bool:
        async with self._breaker_lock:
            return self._auto_failures < AUTO_FAILURE_LIMIT

    async def should_auto_compact(
        self, messages: list[ChatMessage] | tuple[ChatMessage, ...]
    ) -> bool:
        return await self.auto_enabled() and self.estimate_tokens(messages) >= (
            self.context_window - SUMMARY_OUTPUT_RESERVE - AUTO_SAFETY_MARGIN
        )

    async def compact(
        self,
        provider: ChatProvider,
        messages: list[ChatMessage] | tuple[ChatMessage, ...],
        definitions: tuple[ToolDefinition, ...],
        reason: CompactReason,
        *,
        force_offload: bool = False,
    ) -> CompactResult:
        before = self.estimate_tokens(messages)
        source = tuple(messages)
        offloaded = 0
        try:
            if force_offload:
                source, offloaded = await self.offload_and_snip(source)
            summary = await self._summarize_with_retries(provider, source, reason)
            recovery = await self._recovery_message(definitions)
            recent = _recent_messages(source)
            compacted = (
                ChatMessage("assistant", f"<summary>\n{summary}\n</summary>"),
                recovery,
                *recent,
            )
            self.usage_anchor = 0
            self._anchor_chars = 0
            after = self.estimate_tokens(compacted)
            if reason == "auto":
                await self._record_auto_success()
            logger.info(
                "上下文压缩完成 reason=%s before=%s after=%s offloaded=%s",
                reason,
                before,
                after,
                offloaded,
            )
            return CompactResult(True, compacted, before, after, offloaded)
        except Exception as exc:
            if reason == "auto":
                await self._record_auto_failure()
            logger.info(
                "上下文压缩失败 reason=%s before=%s error=%s", reason, before, type(exc).__name__
            )
            return CompactResult(False, tuple(messages), before, before, offloaded, str(exc))

    async def _summarize_with_retries(
        self,
        provider: ChatProvider,
        messages: tuple[ChatMessage, ...],
        reason: CompactReason,
    ) -> str:
        groups = _conversation_groups(messages)
        if reason in {"manual", "emergency"}:
            limit = self.context_window - SUMMARY_OUTPUT_RESERVE - MANUAL_SAFETY_MARGIN
            while groups and _raw_token_estimate(_flatten(groups)) > limit:
                groups.pop(0)
        direct_retries = 0
        while groups:
            current = _flatten(groups)
            try:
                return await _request_summary(provider, current)
            except ProviderError as exc:
                if not is_prompt_too_long(exc):
                    raise
                if direct_retries < DIRECT_PTL_RETRIES:
                    drop_count = 1
                    direct_retries += 1
                else:
                    drop_count = max(1, math.ceil(len(groups) * PTL_DROP_RATIO))
                del groups[:drop_count]
        raise ProviderError("摘要请求在丢弃全部旧消息后仍超过上下文窗口")

    async def record_read(self, call: ToolCall, result: ToolResult) -> None:
        if call.name != "read_file" or not result.success:
            return
        raw_path = call.arguments.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            return
        path = Path(raw_path)
        absolute = (path if path.is_absolute() else self.workspace / path).resolve(strict=False)
        try:
            content = await asyncio.to_thread(absolute.read_text, encoding="utf-8")
        except (OSError, UnicodeError):
            return
        async with self._file_lock:
            self._file_sequence += 1
            key = str(absolute)
            self._files[key] = FileSnapshot(
                key,
                datetime.now().astimezone().isoformat(timespec="seconds"),
                content,
                self._file_sequence,
            )

    async def _recovery_message(self, definitions: tuple[ToolDefinition, ...]) -> ChatMessage:
        async with self._file_lock:
            snapshots = sorted(self._files.values(), key=lambda item: item.sequence, reverse=True)[
                :MAX_RECENT_FILES
            ]
        files: list[str] = []
        char_limit = int(FILE_SNAPSHOT_TOKEN_LIMIT * ESTIMATE_CHARS_PER_TOKEN)
        for snapshot in snapshots:
            content = snapshot.content
            if len(content) > char_limit:
                content = f"{content[:char_limit]}\n(content truncated)"
            files.append(f"文件：{snapshot.path}\n读取时间：{snapshot.read_at}\n{content}")
        tool_rows = [
            json.dumps(
                {"name": item.name, "input_schema": item.input_schema},
                ensure_ascii=False,
                sort_keys=True,
            )
            for item in definitions
        ]
        content = (
            "[上下文恢复：最近文件快照]\n"
            + ("\n\n".join(files) if files else "（无）")
            + "\n\n[上下文恢复：当前可用工具]\n"
            + ("\n".join(tool_rows) if tool_rows else "（无）")
            + f"\n\n[上下文恢复：边界提示]\n{BOUNDARY_NOTICE}"
        )
        return ChatMessage("assistant", content)

    async def _record_auto_failure(self) -> None:
        async with self._breaker_lock:
            self._auto_failures += 1

    async def _record_auto_success(self) -> None:
        async with self._breaker_lock:
            self._auto_failures = 0


async def _request_summary(provider: ChatProvider, messages: tuple[ChatMessage, ...]) -> str:
    chunks: list[str] = []
    async for event in provider.stream(SystemPrompt(SUMMARY_PROMPT), messages, ()):
        if event.type == ProviderEventType.TEXT_DELTA:
            chunks.append(event.text)
        elif event.type == ProviderEventType.TOOL_CALL:
            raise ProviderError("摘要模型违反约束并请求了工具")
    text = "".join(chunks)
    start = text.find("<summary>")
    end = text.find("</summary>", start + 9)
    if start < 0 or end < 0:
        raise ProviderError("摘要响应缺少完整 <summary> 标签")
    summary = text[start + len("<summary>") : end].strip()
    if not summary:
        raise ProviderError("摘要内容为空")
    if not all(f"{index}." in summary for index in range(1, 10)):
        raise ProviderError("摘要未包含完整的九个编号部分")
    return summary


def is_prompt_too_long(error: Exception) -> bool:
    normalized = str(error).lower().replace("-", "_").replace(" ", "_")
    return any(
        marker in normalized
        for marker in ("prompt_too_long", "context_length", "context_window", "too_many_tokens")
    )


def _result_content(result: ToolResult) -> str:
    return result.output if result.success else result.error


def _replace_result_content(result: ToolResult, content: str) -> ToolResult:
    if result.success:
        return replace(result, output=content, truncated=True)
    return replace(result, error=content, truncated=True)


def _write_once(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(content)


def _preview_body(content: str, original_bytes: int, path: Path) -> str:
    lines = "".join(content.splitlines(keepends=True)[:PREVIEW_LINE_LIMIT])
    preview = lines.encode("utf-8")[:PREVIEW_BYTE_LIMIT].decode("utf-8", errors="ignore")
    return (
        f"[工具结果已落盘，原始字节数：{original_bytes}]\n"
        f"[头部预览]\n{preview}\n"
        f"[完整内容路径] {path}\n"
        "如需完整内容，请使用 read_artifact 读取上述路径。"
    )


def _message_text(message: ChatMessage) -> str:
    parts = [message.role, message.content]
    parts.extend(
        json.dumps(
            {"id": call.id, "name": call.name, "arguments": call.arguments},
            ensure_ascii=False,
            sort_keys=True,
        )
        for call in message.tool_calls
    )
    parts.extend(result.model_content() for result in message.tool_results)
    return "\n".join(parts)


def _messages_chars(messages: list[ChatMessage] | tuple[ChatMessage, ...]) -> int:
    return sum(len(_message_text(message)) for message in messages)


def _message_tokens(message: ChatMessage) -> int:
    return math.ceil(len(_message_text(message)) / ESTIMATE_CHARS_PER_TOKEN)


def _raw_token_estimate(messages: tuple[ChatMessage, ...]) -> int:
    return math.ceil((_messages_chars(messages) + len(SUMMARY_PROMPT)) / ESTIMATE_CHARS_PER_TOKEN)


def _recent_messages(messages: tuple[ChatMessage, ...]) -> tuple[ChatMessage, ...]:
    if not messages:
        return ()
    tokens = 0
    count = 0
    start = len(messages)
    while start > 0 and (tokens < RECENT_TOKEN_MINIMUM or count < RECENT_MESSAGE_MINIMUM):
        start -= 1
        tokens += _message_tokens(messages[start])
        count += 1
    while start > 0 and messages[start].role == "tool":
        start -= 1
    return messages[start:]


def _conversation_groups(messages: tuple[ChatMessage, ...]) -> list[list[ChatMessage]]:
    groups: list[list[ChatMessage]] = []
    for message in messages:
        if message.role == "user" or not groups:
            groups.append([message])
        else:
            groups[-1].append(message)
    return groups


def _flatten(groups: list[list[ChatMessage]]) -> tuple[ChatMessage, ...]:
    return tuple(message for group in groups for message in group)
