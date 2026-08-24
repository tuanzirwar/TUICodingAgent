"""长期协作 Agent Team。"""

from .backend import BackendType, InProcessBackend, detect_backend
from .core import (
    AgentNameRegistry,
    LeadMessage,
    Manager,
    Team,
    TeamHasActiveMembersError,
    TeammateInfo,
    sanitize_name,
)
from .mailbox import Mailbox, Message
from .tasks import SharedTask, TaskStore

__all__ = [
    "AgentNameRegistry",
    "BackendType",
    "InProcessBackend",
    "LeadMessage",
    "Mailbox",
    "Manager",
    "Message",
    "SharedTask",
    "TaskStore",
    "Team",
    "TeamHasActiveMembersError",
    "TeammateInfo",
    "detect_backend",
    "sanitize_name",
]
