"""受限、原子化的远程 Skill 安装器。"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import httpx

from ..models import ToolCall, ToolDefinition, ToolResult
from ..tools.base import require_string
from .parser import parse_skill_file

MAX_FILE_SIZE = 1024 * 1024
MAX_TOTAL_SIZE = 8 * 1024 * 1024
MAX_FILES = 64
MAX_DEPTH = 4


class SkillInstaller:
    def __init__(self, user_root: Path, client: httpx.AsyncClient | None = None) -> None:
        self.user_root = user_root.resolve()
        self.client = client

    async def install(self, url: str) -> tuple[str, Path]:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise ValueError("仅支持 HTTPS Skill URL")
        self.user_root.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".skill-install-", dir=self.user_root))
        try:
            if parsed.netloc.casefold() == "github.com":
                await self._download_github_tree(parsed, stage)
            elif parsed.netloc.casefold() == "raw.githubusercontent.com":
                await self._download_raw(url, stage)
            elif parsed.netloc.casefold() == "skills.sh":
                await self._download_skills_sh(url, stage)
            else:
                raise ValueError("仅支持 skills.sh、GitHub tree 或 raw.githubusercontent.com")
            entry = _locate_entry(stage)
            skill = parse_skill_file(entry.parent if entry.name == "SKILL.md" else entry)
            destination = self.user_root / skill.name
            if destination.exists():
                raise ValueError(f"Skill 已存在：{skill.name}")
            payload_root = entry.parent if entry.name == "SKILL.md" else stage
            os.replace(payload_root, destination)
            return skill.name, destination
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    async def _download_github_tree(self, parsed, stage: Path) -> None:
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 5 or parts[2] != "tree":
            raise ValueError("GitHub URL 必须是仓库 tree 目录")
        owner, repo, ref = parts[0], parts[1], parts[3]
        subpath = "/".join(parts[4:])
        api = f"https://api.github.com/repos/{owner}/{repo}/contents/{subpath}"
        await self._download_contents(api, stage, ref, 0, [0, 0])

    async def _download_contents(
        self, api: str, target: Path, ref: str, depth: int, counters: list[int]
    ) -> None:
        if depth > MAX_DEPTH:
            raise ValueError("Skill 目录深度超过限制")
        response = await self._get(api, params={"ref": ref})
        payload = response.json()
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("GitHub Contents API 返回格式无效")
            name = item.get("name")
            kind = item.get("type")
            if not isinstance(name, str) or not _safe_component(name):
                raise ValueError("远程 Skill 含不安全路径")
            if kind == "dir":
                child = target / name
                child.mkdir(parents=True, exist_ok=True)
                await self._download_contents(str(item["url"]), child, ref, depth + 1, counters)
            elif kind == "file":
                size = item.get("size", 0)
                if not isinstance(size, int) or size > MAX_FILE_SIZE:
                    raise ValueError("Skill 单文件超过 1 MiB")
                counters[0] += 1
                counters[1] += size
                _check_counters(counters)
                download_url = item.get("download_url")
                if not isinstance(download_url, str):
                    raise ValueError("远程文件缺少下载地址")
                content = (await self._get(download_url)).content
                _write_limited(target / name, content, counters, declared=size)

    async def _download_raw(self, url: str, stage: Path) -> None:
        content = (await self._get(url)).content
        _write_limited(stage / "SKILL.md", content, [1, len(content)])

    async def _download_skills_sh(self, url: str, stage: Path) -> None:
        parsed = urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        if parts[:3] == ["api", "v1", "skills"]:
            api = url
        elif len(parts) >= 3 and parts[0] != "p":
            api = f"https://skills.sh/api/v1/skills/{'/'.join(parts[:3])}"
        else:
            raise ValueError("skills.sh URL 必须指向单个 Skill 页面")
        payload = (await self._get(api)).json()
        files = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(files, list) or not files:
            raise ValueError("skills.sh API 响应缺少文件列表")
        counters = [0, 0]
        for item in files:
            if not isinstance(item, dict):
                raise ValueError("skills.sh 文件记录无效")
            relative = item.get("path")
            contents = item.get("contents")
            if not isinstance(relative, str) or not isinstance(contents, str):
                raise ValueError("skills.sh 文件记录缺少路径或内容")
            path = PurePosixPath(relative)
            if path.is_absolute() or ".." in path.parts or len(path.parts) > MAX_DEPTH + 1:
                raise ValueError("skills.sh Skill 含不安全路径或目录过深")
            content = contents.encode("utf-8")
            counters[0] += 1
            counters[1] += len(content)
            _write_limited(stage.joinpath(*path.parts), content, counters, declared=len(content))

    async def _get(self, url: str, **kwargs) -> httpx.Response:
        owns = self.client is None
        client = self.client or httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10))
        try:
            response = await client.get(url, follow_redirects=True, **kwargs)
            response.raise_for_status()
            return response
        finally:
            if owns:
                await client.aclose()


class InstallSkillTool:
    read_only = False
    definition = ToolDefinition(
        "install_skill",
        "从受支持的 HTTPS URL 安装 Skill，并立即刷新 Skill 目录。",
        {
            "type": "object",
            "properties": {"url": {"type": "string", "format": "uri"}},
            "required": ["url"],
            "additionalProperties": False,
        },
    )

    def __init__(
        self,
        installer: SkillInstaller,
        reload_callback: Callable[[], Awaitable[None] | None],
    ) -> None:
        self.installer = installer
        self.reload_callback = reload_callback

    async def execute(self, call: ToolCall) -> ToolResult:
        url = require_string(call, "url")
        try:
            name, path = await self.installer.install(url)
            refreshed = self.reload_callback()
            if refreshed is not None:
                await refreshed
        except (ValueError, OSError, httpx.HTTPError) as exc:
            return ToolResult(call.id, call.name, False, error=f"安装 Skill 失败：{exc}")
        return ToolResult(call.id, call.name, True, output=f"已安装 Skill {name}：{path}")


def _locate_entry(stage: Path) -> Path:
    direct = stage / "SKILL.md"
    if direct.is_file():
        return direct
    matches = list(stage.rglob("SKILL.md"))
    if len(matches) != 1:
        raise ValueError("安装包必须且只能包含一个 SKILL.md")
    return matches[0]


def _safe_component(name: str) -> bool:
    path = PurePosixPath(name)
    return len(path.parts) == 1 and name not in {".", ".."}


def _check_counters(counters: list[int]) -> None:
    if counters[0] > MAX_FILES:
        raise ValueError("Skill 文件数超过 64")
    if counters[1] > MAX_TOTAL_SIZE:
        raise ValueError("Skill 总大小超过 8 MiB")


def _write_limited(
    path: Path, content: bytes, counters: list[int], *, declared: int | None = None
) -> None:
    if len(content) > MAX_FILE_SIZE:
        raise ValueError("Skill 单文件超过 1 MiB")
    if declared is not None and len(content) != declared:
        counters[1] += len(content) - declared
    _check_counters(counters)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
