"""不可配置、不可绕过的危险命令启发式黑名单。"""

from __future__ import annotations

import re

_DANGEROUS_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\brm\s+(?:-[a-z]*r[a-z]*f[a-z]*|-[a-z]*f[a-z]*r[a-z]*|--[a-z-]*recursive[a-z-]*force)[^\n]*(?:\s/\s*$|\s~(?:/|\s|$)|\$HOME)",
        r"\b(?:mkfs(?:\.[a-z0-9]+)?|format)\b[^\n]*(?:/dev/|[a-z]:)",
        r"\bdd\b[^\n]*\bof\s*=\s*/dev/(?:sd|hd|nvme|disk)",
        r">\s*/dev/(?:sd|hd|nvme|disk)",
        r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",
        r"\bRemove-Item\b[^\n]*(?:-Recurse[^\n]*-Force|-Force[^\n]*-Recurse)[^\n]*(?:[A-Z]:\\|\$HOME)",
    )
)


def blacklist_denial(command: str) -> str | None:
    if any(pattern.search(command) for pattern in _DANGEROUS_PATTERNS):
        return "危险命令黑名单拒绝：检测到不可绕过的高危操作"
    return None
