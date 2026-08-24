"""跨模块共享的不可变领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

ProtocolName = Literal["openai", "anthropic"]
WireAPI = Literal["chat_completions", "responses", "messages"]
MessageRole = Literal["user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class SystemPrompt:
    """物理分离的稳定缓存块与动态环境块。"""

    stable: str
    environment: str = ""

    def __str__(self) -> str:
        return "\n\n".join(part for part in (self.stable, self.environment) if part)

    def __contains__(self, value: str) -> bool:
        return value in str(self)


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """一份已校验的模型服务配置。"""

    name: str
    protocol: ProtocolName
    model: str
    api_key: str = field(repr=False)
    base_url: str | None = None
    thinking: bool = False
    context_window: int | None = None
    wire_api: WireAPI | None = None


@dataclass(frozen=True, slots=True)
class AppConfig:
    providers: tuple[ProviderConfig, ...]
    enable_subagent_background: bool = True
    features: dict[str, bool] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: MessageRole
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()
    provider_items: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, object]


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, object]
    raw_arguments: str = ""
    parse_error: str = ""


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool_call_id: str
    name: str
    success: bool
    output: str = ""
    error: str = ""
    metadata: dict[str, object] = field(default_factory=dict)
    truncated: bool = False

    def model_content(self) -> str:
        """生成稳定、可由模型理解的 JSON 结果文本。"""

        import json

        return json.dumps(
            {
                "success": self.success,
                "output": self.output,
                "error": self.error,
                "metadata": self.metadata,
                "truncated": self.truncated,
            },
            ensure_ascii=False,
        )


class ProviderEventType(StrEnum):
    TEXT_DELTA = "text_delta"
    TOOL_CALL = "tool_call"
    USAGE = "usage"
    PROVIDER_ITEM = "provider_item"


@dataclass(frozen=True, slots=True)
class ProviderEvent:
    type: ProviderEventType
    text: str = ""
    tool_call: ToolCall | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    provider_item: dict[str, object] | None = None


class RuntimeEventType(StrEnum):
    STARTED = "started"
    TEXT_DELTA = "text_delta"
    COMPLETED = "completed"
    FAILED = "failed"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    LIMIT_REACHED = "limit_reached"
    ITERATION_STARTED = "iteration_started"
    USAGE = "usage"
    CANCELLED = "cancelled"
    PERMISSION_REQUESTED = "permission_requested"
    CONTEXT_STATUS = "context_status"


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    type: RuntimeEventType
    text: str = ""
    elapsed: float | None = None
    tool_call: ToolCall | None = None
    tool_result: ToolResult | None = None
    iteration: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    total_cache_creation_input_tokens: int = 0
    total_cache_read_input_tokens: int = 0


class AgentMode(StrEnum):
    EXECUTE = "execute"
    PLAN = "plan"


class PermissionMode(StrEnum):
    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    PLAN = "plan"
    BYPASS = "bypassPermissions"
    DONT_ASK = "dontAsk"


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PermissionChoice(StrEnum):
    ALLOW_ONCE = "allow_once"
    ALLOW_ALWAYS = "allow_always"
    DENY_ONCE = "deny_once"
    CANCEL = "cancel"


class SessionState(StrEnum):
    IDLE = "idle"
    STREAMING = "streaming"
    RESUMING = "resuming"
