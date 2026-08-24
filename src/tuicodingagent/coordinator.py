"""仅编排、不直接实现的 Coordinator Mode。"""

from __future__ import annotations

import os

COORDINATOR_ALLOWED_TOOLS = [
    "Agent",
    "TeamCreate",
    "TeamDelete",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskUpdate",
    "SendMessage",
    "read_file",
    "glob",
    "grep",
    "bash",
]

SYSTEM_PROMPT_SUFFIX = """<coordinator-mode>
你处于 Coordinator Mode，按 Research、Decomposition、Delegation、Synthesis/Verification 四阶段工作。
派出 Agent 或 SendMessage 后必须停止本轮，只用一行说明已派出的队员和目标，等待自然任务通知。
禁止随后自行调用 read_file/glob/grep/bash 重复探索，也禁止 sleep 或用 TaskList 轮询凑时间。
Research 首次定位可读代码；Synthesis 只读队员报告；
Verification 可用 bash 做 diff、status、merge 和测试。
</coordinator-mode>"""


def env_truthy(value: str) -> bool:
    return value.strip().casefold() in {"1", "true", "yes"}


def feature_has(config, name: str) -> bool:
    return bool(getattr(config, "features", {}).get(name.casefold(), False))


def is_enabled(config) -> bool:
    return feature_has(config, "COORDINATOR_MODE") and env_truthy(
        os.environ.get("TUICODINGAGENT_COORDINATOR_MODE", "")
    )


__all__ = [
    "COORDINATOR_ALLOWED_TOOLS",
    "SYSTEM_PROMPT_SUFFIX",
    "env_truthy",
    "feature_has",
    "is_enabled",
]
