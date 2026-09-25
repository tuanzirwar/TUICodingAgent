"""运行单个低开销评测任务并保存可审计轨迹。"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from ..config import ConfigLoader
from ..models import PermissionMode, RuntimeEvent, RuntimeEventType, ToolCall, ToolResult
from ..permissions import PermissionManager
from ..providers.base import ChatProvider
from ..providers.factory import create_provider
from ..runtime import ConversationRuntime
from ..tools.registry import create_default_registry


@dataclass(frozen=True, slots=True)
class EvalRunResult:
    """单次 Agent 运行的稳定摘要；不包含任何认证信息。"""

    status: str
    provider: str
    model: str
    elapsed_seconds: float
    iterations: int
    tool_calls: int
    tool_failures: int
    input_tokens: int
    output_tokens: int
    limit_reached: bool
    final_text: str
    error: str = ""


async def run_eval(
    *,
    provider: ChatProvider,
    workspace: Path,
    prompt: str,
    output_dir: Path,
    max_iterations: int = 6,
    timeout_seconds: float = 300.0,
) -> EvalRunResult:
    """运行一次隔离评测，写出 manifest、JSONL 轨迹、补丁和结果。"""

    root, destination = await asyncio.to_thread(_prepare_paths, workspace, output_dir)
    if not await asyncio.to_thread(_git_is_clean, root):
        raise ValueError("评测工作区必须是 clean git 仓库，避免把既有改动误记为 Agent 产物")
    permission = PermissionManager(root, mode=PermissionMode.BYPASS)
    runtime = ConversationRuntime(
        provider,
        tool_registry=create_default_registry(root),
        workspace=root,
        permission_manager=permission,
        max_iterations=max_iterations,
    )
    manifest = {
        "provider": provider.config.name,
        "model": provider.config.model,
        "protocol": provider.config.protocol,
        "wire_api": provider.config.wire_api,
        "workspace": str(root),
        "max_iterations": max_iterations,
        "timeout_seconds": timeout_seconds,
    }
    _write_json(destination / "manifest.json", manifest)

    started = time.monotonic()
    events: list[dict[str, object]] = []
    final_text = ""
    error = ""
    status = "completed"
    iterations = 0
    tool_calls = 0
    tool_failures = 0
    limit_reached = False
    try:
        async with asyncio.timeout(timeout_seconds):
            async for event in runtime.chat(prompt):
                events.append(_event_record(event))
                iterations = max(iterations, event.iteration or 0)
                if event.type == RuntimeEventType.TOOL_STARTED:
                    tool_calls += 1
                elif event.type == RuntimeEventType.TOOL_FINISHED:
                    if event.tool_result is not None and not event.tool_result.success:
                        tool_failures += 1
                elif event.type == RuntimeEventType.LIMIT_REACHED:
                    limit_reached = True
                elif event.type == RuntimeEventType.COMPLETED:
                    final_text = event.text
                elif event.type == RuntimeEventType.FAILED:
                    status = "failed"
                    error = event.text
    except TimeoutError:
        runtime.request_cancel()
        status = "timeout"
        error = f"运行超过 {timeout_seconds:g} 秒"
    except Exception as exc:  # 评测必须把 provider/协议失败固化到结果中
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"

    result = EvalRunResult(
        status=status,
        provider=provider.config.name,
        model=provider.config.model,
        elapsed_seconds=round(time.monotonic() - started, 3),
        iterations=iterations,
        tool_calls=tool_calls,
        tool_failures=tool_failures,
        input_tokens=runtime.total_input_tokens,
        output_tokens=runtime.total_output_tokens,
        limit_reached=limit_reached,
        final_text=final_text,
        error=error,
    )
    _write_jsonl(destination / "trajectory.jsonl", events)
    (destination / "final.patch").write_text(_git_diff(root), encoding="utf-8")
    _write_json(destination / "result.json", asdict(result))
    return result


def _event_record(event: RuntimeEvent) -> dict[str, object]:
    record: dict[str, object] = {
        "type": event.type.value,
        "elapsed": event.elapsed,
        "iteration": event.iteration,
    }
    if event.text:
        record["text"] = event.text
    if event.tool_call is not None:
        record["tool_call"] = _call_record(event.tool_call)
    if event.tool_result is not None:
        record["tool_result"] = _result_record(event.tool_result)
    if event.type == RuntimeEventType.USAGE:
        record["usage"] = {
            "input_tokens": event.input_tokens,
            "output_tokens": event.output_tokens,
            "total_input_tokens": event.total_input_tokens,
            "total_output_tokens": event.total_output_tokens,
            "cache_creation_input_tokens": event.cache_creation_input_tokens,
            "cache_read_input_tokens": event.cache_read_input_tokens,
        }
    return record


def _call_record(call: ToolCall) -> dict[str, object]:
    return {"id": call.id, "name": call.name, "arguments": call.arguments}


def _result_record(result: ToolResult) -> dict[str, object]:
    return {
        "tool_call_id": result.tool_call_id,
        "name": result.name,
        "success": result.success,
        "output": result.output,
        "error": result.error,
        "metadata": result.metadata,
        "truncated": result.truncated,
    }


def _git_is_clean(workspace: Path) -> bool:
    completed = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=workspace,
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0 and not completed.stdout.strip()


def _git_diff(workspace: Path) -> str:
    """导出仅含生产代码的补丁，排除代理运行痕迹和测试文件。"""

    tracked = subprocess.run(
        ["git", "diff", "--name-only", "-z", "--no-ext-diff"],
        cwd=workspace,
        capture_output=True,
        check=False,
    )
    tracked_paths = (
        [
            raw.decode("utf-8", errors="surrogateescape")
            for raw in tracked.stdout.split(b"\0")
            if raw
            and not _is_excluded_patch_path(
                raw.decode("utf-8", errors="surrogateescape")
            )
        ]
        if tracked.returncode == 0
        else []
    )
    parts: list[str] = []
    if tracked_paths:
        completed = subprocess.run(
            ["git", "diff", "--binary", "--no-ext-diff", "--", *tracked_paths],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode == 0:
            parts.append(completed.stdout)
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=workspace,
        capture_output=True,
        check=False,
    )
    if untracked.returncode != 0:
        return "".join(parts)
    for raw_path in untracked.stdout.split(b"\0"):
        if not raw_path:
            continue
        path = raw_path.decode("utf-8", errors="surrogateescape")
        if _is_excluded_patch_path(path):
            continue
        added = subprocess.run(
            ["git", "diff", "--binary", "--no-index", "--", "/dev/null", path],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if added.returncode in {0, 1}:
            parts.append(added.stdout)
    return "".join(parts)


def _is_excluded_patch_path(path: str) -> bool:
    """排除评测运行痕迹与测试文件，避免污染官方注入的测试。"""

    normalized = path.replace("\\", "/").lstrip("./")
    parts = normalized.lower().split("/")
    runtime_dirs = {".mewcode", ".tuicodingagent", ".claude", ".codex"}
    if any(part in runtime_dirs for part in parts):
        return True
    if any(part in {"test", "tests"} for part in parts[:-1]):
        return True
    name = parts[-1] if parts else ""
    return name.startswith("test_") or "_test." in name
def _prepare_paths(workspace: Path, output_dir: Path) -> tuple[Path, Path]:
    root = workspace.resolve()
    destination = output_dir.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    return root, destination


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


async def run_from_config(
    *,
    config_path: Path,
    provider_name: str,
    workspace: Path,
    prompt: str,
    output_dir: Path,
    max_iterations: int,
    timeout_seconds: float,
) -> EvalRunResult:
    """从项目配置选择 provider，并确保 HTTP 客户端总会关闭。"""

    config = ConfigLoader.load(config_path)
    selected = next((item for item in config.providers if item.name == provider_name), None)
    if selected is None:
        names = "、".join(item.name for item in config.providers)
        raise ValueError(f"找不到 provider {provider_name!r}；可选：{names}")
    provider = create_provider(selected)
    try:
        return await run_eval(
            provider=provider,
            workspace=workspace,
            prompt=prompt,
            output_dir=output_dir,
            max_iterations=max_iterations,
            timeout_seconds=timeout_seconds,
        )
    finally:
        await provider.close()
