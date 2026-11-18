"""持久化边界的凭据脱敏；工作文件和内存中的任务内容不修改。"""

from __future__ import annotations

import re
from typing import Any

MASK = "[REDACTED]"
_SECRET_FIELDS = re.compile(
    r"^(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|client[_-]?secret|authorization)$",
    re.IGNORECASE,
)
_ASSIGNMENT = re.compile(
    r"(?im)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|client[_-]?secret)[\"']?\s*[:=]\s*)([\"']?)([^\s\"';,]+)([\"']?)"
)
_BEARER = re.compile(r"(?i)(\bBearer\s+)[a-z0-9._~+/=-]+")
_KEY = re.compile(r"\bsk-(?:proj-|ant-)?[a-zA-Z0-9_-]{16,}")
_URL_PASSWORD = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s/:@]+:)[^\s/@]+(@)", re.IGNORECASE)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z]+ )?PRIVATE KEY-----", re.DOTALL
)
_EMAIL = re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b")


def redact(value: Any, *, personal_data: bool = False) -> Any:
    """返回脱敏副本。个人联系方式在任务存档中保留，在审计元数据中可隐藏。"""
    if isinstance(value, str):
        text = _PRIVATE_KEY.sub(MASK, value)
        text = _ASSIGNMENT.sub(lambda m: m[1] + m[2] + MASK + m[4], text)
        text = _BEARER.sub(lambda m: m[1] + MASK, text)
        text = _KEY.sub(MASK, text)
        text = _URL_PASSWORD.sub(lambda m: m[1] + MASK + m[2], text)
        return _EMAIL.sub(MASK, text) if personal_data else text
    if isinstance(value, dict):
        return {
            key: MASK
            if _SECRET_FIELDS.fullmatch(str(key)) and item
            else redact(item, personal_data=personal_data)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, personal_data=personal_data) for item in value]
    return value
