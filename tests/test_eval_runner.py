# ruff: noqa: ASYNC221, ASYNC240
from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from tuicodingagent.eval.runner import run_eval
from tuicodingagent.models import (
    ChatMessage,
    ProviderConfig,
    ProviderEvent,
    ProviderEventType,
    SystemPrompt,
    ToolCall,
    ToolDefinition,
)


class FakeProvider:
    def __init__(self) -> None:
        self.config = ProviderConfig("fake", "openai", "fake-model", "secret")
        self.calls = 0

    async def stream(
        self,
        system_prompt: SystemPrompt | str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        del system_prompt, messages, tools
        self.calls += 1
        if self.calls == 1:
            yield ProviderEvent(
                ProviderEventType.TOOL_CALL,
                tool_call=ToolCall(
                    "call-1", "write_file", {"path": "answer.txt", "content": "ok\n"}
                ),
            )
            yield ProviderEvent(ProviderEventType.USAGE, input_tokens=10, output_tokens=3)
        else:
            yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="完成")
            yield ProviderEvent(ProviderEventType.USAGE, input_tokens=12, output_tokens=2)

    async def close(self) -> None:
        return None


async def test_eval_runner_records_patch_trajectory_and_usage(tmp_path: Path) -> None:
    await asyncio.to_thread(subprocess.run, ["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    await asyncio.to_thread(subprocess.run, ["git", "add", "seed.txt"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Eval",
            "-c",
            "user.email=eval@example.invalid",
            "commit",
            "-qm",
            "seed",
        ],
        cwd=tmp_path,
        check=True,
    )
    output = tmp_path.parent / "output"
    subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path, check=True)

    result = await run_eval(
        provider=FakeProvider(),
        workspace=tmp_path,
        prompt="完成任务",
        output_dir=output,
        max_iterations=3,
        timeout_seconds=10,
    )

    assert result.status == "completed"
    assert result.tool_calls == 1
    assert result.tool_failures == 0
    assert result.input_tokens == 22
    assert result.output_tokens == 5
    assert (tmp_path / "answer.txt").read_text(encoding="utf-8") == "ok\n"
    assert "answer.txt" in (output / "final.patch").read_text(encoding="utf-8")
    assert "secret" not in (output / "manifest.json").read_text(encoding="utf-8")
    records = [
        json.loads(line)
        for line in (output / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(item["type"] == "tool_finished" for item in records)
