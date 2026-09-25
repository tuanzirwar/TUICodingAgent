"""兼容入口：复用已验证的本地 Codex CLI 适配器。"""

from __future__ import annotations

from .adapters import CodexCLIAdapter

__all__ = ["CodexCLIAdapter"]
