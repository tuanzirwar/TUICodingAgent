"""SWE-bench Live 的 TUICodingAgent 与 Codex CLI 适配器。"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .runner import run_from_config

EXPECTED_MODEL = "gpt-5.6-luna"


class AdapterError(RuntimeError):
    """适配器无法安全启动时抛出的错误。"""


@dataclass(frozen=True, slots=True)
class AdapterResult:
    """两个代理统一的运行摘要。"""

    status: str
    model: str
    elapsed_seconds: float
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    retries: int = 0
    final_text: str = ""
    stdout: str = ""
    stderr: str = ""
    events: tuple[dict[str, object], ...] = ()
    error: str = ""


class TUICodingAgentAdapter:
    """复用现有 headless runner，并锁定配置中的模型。"""

    name = "tuicodingagent"

    def __init__(self, config_path: Path, provider_name: str) -> None:
        self.config_path = config_path
        self.provider_name = provider_name

    def validate(self) -> None:
        from ..config import ConfigLoader

        config = ConfigLoader.load(self.config_path)
        provider = next(
            (item for item in config.providers if item.name == self.provider_name), None
        )
        if provider is None:
            raise AdapterError(f"找不到 TUICodingAgent provider：{self.provider_name}")
        if provider.model != EXPECTED_MODEL:
            raise AdapterError(
                f"模型锁失败：TUICodingAgent 配置为 {provider.model!r}，要求 {EXPECTED_MODEL!r}"
            )

    def run(
        self,
        *,
        workspace: Path,
        prompt: str,
        output_dir: Path,
        max_iterations: int,
        timeout_seconds: float,
    ) -> AdapterResult:
        self.validate()
        started = time.monotonic()
        try:
            result = asyncio.run(
                run_from_config(
                    config_path=self.config_path,
                    provider_name=self.provider_name,
                    workspace=workspace,
                    prompt=prompt,
                    output_dir=output_dir,
                    max_iterations=max_iterations,
                    timeout_seconds=timeout_seconds,
                )
            )
        except Exception as exc:  # 适配器错误由 harness 固化，不伪造成功结果
            return AdapterResult(
                status="failed",
                model=EXPECTED_MODEL,
                elapsed_seconds=round(time.monotonic() - started, 3),
                error=f"{type(exc).__name__}: {exc}",
            )
        events = _read_events(output_dir / "trajectory.jsonl")
        usage = _sum_usage(events)
        return AdapterResult(
            status=result.status,
            model=result.model,
            elapsed_seconds=result.elapsed_seconds,
            input_tokens=result.input_tokens or usage[0],
            output_tokens=result.output_tokens or usage[1],
            cached_input_tokens=usage[2],
            tool_calls=result.tool_calls,
            tool_failures=result.tool_failures,
            final_text=result.final_text,
            stdout=result.final_text,
            events=tuple(events),
            error=result.error,
        )


class CodexCLIAdapter:
    """调用本机 codex exec 的全新 ephemeral JSONL 会话。"""

    name = "codex"

    def __init__(self, executable: str = "codex") -> None:
        self.executable = executable

    def validate(self) -> None:
        if shutil.which(self.executable) is None:
            raise AdapterError(f"找不到 codex CLI：{self.executable}")
        help_result = subprocess.run(
            [self.executable, "exec", "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
        help_text = help_result.stdout + help_result.stderr
        required = ("--json", "--ephemeral", "--model")
        missing = [flag for flag in required if flag not in help_text]
        if help_result.returncode != 0 or missing:
            detail = f"，缺少参数：{', '.join(missing)}" if missing else ""
            raise AdapterError(f"codex exec 不满足机器解析要求{detail}")

    def build_command(self, workspace: Path, *, timeout_seconds: float) -> list[str]:
        """构造可审计的命令；模型固定，不允许静默替换。"""

        del timeout_seconds  # 超时由父进程硬截止，codex 没有统一的 timeout 参数
        return [
            self.executable,
            "exec",
            "--json",
            "--ephemeral",
            "--approve-for-me",
            "-m",
            EXPECTED_MODEL,
            "-C",
            str(workspace),
            "-",
        ]

    def run(
        self,
        *,
        workspace: Path,
        prompt: str,
        timeout_seconds: float,
    ) -> AdapterResult:
        self.validate()
        command = self.build_command(workspace, timeout_seconds=timeout_seconds)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=workspace,
                timeout=timeout_seconds,
                check=False,
            )
            status = "completed" if completed.returncode == 0 else "failed"
            error = "" if completed.returncode == 0 else _tail(completed.stderr)
        except subprocess.TimeoutExpired as exc:
            stdout = _as_text(exc.stdout)
            stderr = _as_text(exc.stderr)
            return AdapterResult(
                status="timeout",
                model=EXPECTED_MODEL,
                elapsed_seconds=round(time.monotonic() - started, 3),
                stdout=stdout,
                stderr=stderr,
                events=tuple(_decode_jsonl(stdout)),
                error=f"运行超过 {timeout_seconds:g} 秒",
            )
        except OSError as exc:
            return AdapterResult(
                status="failed",
                model=EXPECTED_MODEL,
                elapsed_seconds=round(time.monotonic() - started, 3),
                error=f"codex CLI 启动失败：{exc}",
            )
        events = _decode_jsonl(completed.stdout)
        usage = _extract_codex_usage(events)
        final_text = _extract_final_text(events)
        return AdapterResult(
            status=status,
            model=EXPECTED_MODEL,
            elapsed_seconds=round(time.monotonic() - started, 3),
            input_tokens=usage[0],
            output_tokens=usage[1],
            cached_input_tokens=usage[2],
            tool_calls=_count_codex_tools(events),
            tool_failures=_count_codex_failures(events),
            final_text=final_text,
            stdout=completed.stdout,
            stderr=completed.stderr,
            events=tuple(events),
            error=error,
        )


def _read_events(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    return _decode_jsonl(path.read_text(encoding="utf-8"))


def _decode_jsonl(text: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _sum_usage(events: list[dict[str, object]]) -> tuple[int, int, int]:
    input_tokens = output_tokens = cached = 0
    for event in events:
        usage = event.get("usage")
        if isinstance(usage, dict):
            input_tokens += _int(usage.get("input_tokens"))
            output_tokens += _int(usage.get("output_tokens"))
            cached += _int(usage.get("cache_read_input_tokens"))
    return input_tokens, output_tokens, cached


def _extract_codex_usage(events: list[dict[str, object]]) -> tuple[int, int, int]:
    input_tokens = output_tokens = cached = 0
    for event in events:
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        input_tokens += _int(usage.get("input_tokens", usage.get("prompt_tokens")))
        output_tokens += _int(usage.get("output_tokens", usage.get("completion_tokens")))
        cached += _int(usage.get("cached_input_tokens", usage.get("cache_read_input_tokens")))
    return input_tokens, output_tokens, cached


def _extract_final_text(events: list[dict[str, object]]) -> str:
    texts: list[str] = []
    for event in events:
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str):
                texts.append(text)
        text = event.get("text")
        if isinstance(text, str) and event.get("type") in {"agent_message", "final"}:
            texts.append(text)
    return texts[-1] if texts else ""


def _count_codex_tools(events: list[dict[str, object]]) -> int:
    count = 0
    for event in events:
        item = event.get("item")
        if isinstance(item, dict):
            item_type = str(item.get("type", ""))
            if item_type.endswith("_call") or item_type in {"command_execution", "file_change"}:
                count += 1
    return count


def _count_codex_failures(events: list[dict[str, object]]) -> int:
    count = 0
    for event in events:
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        if item.get("status") in {"failed", "error"}:
            count += 1
        if _int(item.get("exit_code")) not in {0}:
            count += 1
    return count


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _tail(value: str, limit: int = 1000) -> str:
    return value[-limit:].strip() or "命令返回非零状态"


def _as_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


__all__ = [
    "AdapterError",
    "AdapterResult",
    "CodexCLIAdapter",
    "EXPECTED_MODEL",
    "TUICodingAgentAdapter",
]
