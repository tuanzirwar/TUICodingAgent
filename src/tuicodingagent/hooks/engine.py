"""Hook 条件分派、动作执行与会话内状态。"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Any

import httpx

from .models import BLOCKING_EVENTS, HookDispatch, HookEvent, HookRule


@dataclass(frozen=True, slots=True)
class _ActionResult:
    blocked: bool = False
    reason: str = ""
    failure: str = ""


class HookEngine:
    def __init__(self, rules: tuple[HookRule, ...] = (), sources=()) -> None:
        self.rules = rules
        self.sources = tuple(sources)
        self._executed_once: set[str] = set()
        self._prompt_queue: list[str] = []
        self._tasks: set[asyncio.Task[None]] = set()

    async def emit(self, event: HookEvent, payload: dict[str, Any]) -> HookDispatch:
        event_payload = dict(payload)
        event_payload["event"] = event.value
        for rule in self.rules:
            if rule.event != event or rule.name in self._executed_once:
                continue
            if rule.predicate is not None and not rule.predicate.matches(event_payload):
                continue
            if rule.only_once:
                self._executed_once.add(rule.name)
            if rule.async_run:
                task = asyncio.create_task(self._run_background(rule, event_payload))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
                continue
            result = await self._safe_action(rule, event_payload)
            if result.failure:
                self._log_failure(rule, result.failure)
            if result.blocked and event in BLOCKING_EVENTS:
                return HookDispatch(True, rule.name, result.reason or "blocked by hook")
        return HookDispatch()

    def drain_prompts(self) -> tuple[str, ...]:
        prompts = tuple(self._prompt_queue)
        self._prompt_queue.clear()
        return prompts

    def reset_session(self) -> None:
        self._executed_once.clear()
        self._prompt_queue.clear()

    async def close(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    def summary(self) -> str:
        if not self.rules:
            return "No hooks loaded."
        rows: list[str] = []
        for event in HookEvent:
            grouped = [rule for rule in self.rules if rule.event == event]
            if not grouped:
                continue
            rows.append(f"{event.value}:")
            for rule in grouped:
                flags = " ".join(
                    flag
                    for enabled, flag in (
                        (rule.only_once, "[once]"),
                        (rule.async_run, "[async]"),
                    )
                    if enabled
                )
                rows.append(
                    f"  {rule.name}  {event.value}  {rule.action.type}"
                    + (f"  {flags}" if flags else "")
                )
        rows.append("Loaded from: " + ", ".join(str(path) for path in self.sources))
        return "\n".join(rows)

    async def _run_background(self, rule: HookRule, payload: dict[str, Any]) -> None:
        result = await self._safe_action(rule, payload)
        if result.failure:
            self._log_failure(rule, result.failure)

    async def _safe_action(self, rule: HookRule, payload: dict[str, Any]) -> _ActionResult:
        try:
            return await self._run_action(rule, payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # Hook 边界不允许用户动作中断主流程
            return _ActionResult(failure=f"{type(exc).__name__}: {exc}")

    async def _run_action(self, rule: HookRule, payload: dict[str, Any]) -> _ActionResult:
        if rule.action.type == "prompt":
            self._prompt_queue.append(str(rule.action.options["text"]))
            return _ActionResult()
        if rule.action.type == "subagent":
            print(f"[hook subagent] not yet implemented, skipped: {rule.name}", file=sys.stderr)
            return _ActionResult()
        if rule.action.type == "shell":
            return await self._shell(rule, payload)
        return await self._http(rule, payload)

    async def _shell(self, rule: HookRule, payload: dict[str, Any]) -> _ActionResult:
        try:
            process = await asyncio.create_subprocess_shell(
                str(rule.action.options["command"]),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
            stdout, stderr = await asyncio.wait_for(process.communicate(encoded), rule.timeout)
        except TimeoutError:
            if process.returncode is None:
                process.kill()
                await process.wait()
            return _ActionResult(failure=f"timeout after {rule.timeout:g}s")
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            return _ActionResult(failure=f"shell error: {exc}")
        detail = (stderr or stdout).decode(errors="replace").strip()
        if process.returncode == 2 and rule.event in BLOCKING_EVENTS:
            return _ActionResult(True, detail or "shell hook blocked")
        if process.returncode != 0:
            return _ActionResult(failure=f"exit {process.returncode}: {detail}")
        return _ActionResult()

    async def _http(self, rule: HookRule, payload: dict[str, Any]) -> _ActionResult:
        options = rule.action.options
        try:
            body = options.get("body")
            if body is None:
                content = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            else:
                content = str(body).format_map(_PayloadMap(payload))
            headers = dict(options.get("headers", {}))
            if body is None:
                headers.setdefault("content-type", "application/json")
            async with httpx.AsyncClient(timeout=rule.timeout) as client:
                response = await client.request(
                    str(options.get("method", "POST")),
                    str(options["url"]),
                    headers=headers,
                    content=content.encode(),
                )
            if not 200 <= response.status_code < 300:
                return _ActionResult(failure=f"HTTP {response.status_code}")
            if rule.event not in BLOCKING_EVENTS:
                return _ActionResult()
            try:
                decision = response.json()
            except ValueError:
                return _ActionResult(failure="response is not valid JSON")
            if isinstance(decision, dict) and decision.get("decision") == "block":
                return _ActionResult(True, str(decision.get("reason", "HTTP hook blocked")))
            return _ActionResult()
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, TimeoutError, KeyError, ValueError) as exc:
            return _ActionResult(failure=str(exc))

    @staticmethod
    def _log_failure(rule: HookRule, reason: str) -> None:
        print(f"[hook {rule.name}] {rule.event.value} failed: {reason}", file=sys.stderr)


class _PayloadMap(dict[str, object]):
    def __missing__(self, key: str) -> object:
        return ""
