"""TUICodingAgent 上下文管理公共接口。"""

from .manager import (
    AUTO_FAILURE_LIMIT,
    AUTO_SAFETY_MARGIN,
    BOUNDARY_NOTICE,
    DEFAULT_CONTEXT_WINDOWS,
    ESTIMATE_CHARS_PER_TOKEN,
    MANUAL_SAFETY_MARGIN,
    MESSAGE_AGGREGATE_LIMIT,
    SINGLE_RESULT_LIMIT,
    SUMMARY_OUTPUT_RESERVE,
    CompactResult,
    ContextController,
    is_prompt_too_long,
)

__all__ = [
    "AUTO_FAILURE_LIMIT",
    "AUTO_SAFETY_MARGIN",
    "BOUNDARY_NOTICE",
    "DEFAULT_CONTEXT_WINDOWS",
    "ESTIMATE_CHARS_PER_TOKEN",
    "MANUAL_SAFETY_MARGIN",
    "MESSAGE_AGGREGATE_LIMIT",
    "SINGLE_RESULT_LIMIT",
    "SUMMARY_OUTPUT_RESERVE",
    "CompactResult",
    "ContextController",
    "is_prompt_too_long",
]
