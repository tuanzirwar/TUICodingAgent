from __future__ import annotations

import asyncio
from pathlib import Path

import tuicodingagent.context.manager as context_module
from tuicodingagent.context import ContextController
from tuicodingagent.models import ChatMessage, ToolResult


def _tool_message(*items: tuple[str, str]) -> ChatMessage:
    return ChatMessage(
        "tool",
        tool_results=tuple(
            ToolResult(identifier, "read_file", True, output=text) for identifier, text in items
        ),
    )


async def test_single_large_result_offloads_with_stable_preview(tmp_path: Path) -> None:
    controller = ContextController(tmp_path, 128_000)
    content = "\n".join(f"第 {index} 行" for index in range(10_000))
    original_bytes = len(content.encode("utf-8"))
    assert original_bytes > 50_000
    managed, count = await controller.offload_and_snip([_tool_message(("large-id", content))])
    result = managed[0].tool_results[0]
    path = controller.results_dir / "large-id"
    assert count == 1 and path.read_text(encoding="utf-8") == content
    assert f"原始字节数：{original_bytes}" in result.output
    assert "[头部预览]" in result.output and "[完整内容路径]" in result.output
    assert "文件读取工具" in result.output
    preview = result.output.split("[头部预览]\n", 1)[1].split("\n[完整内容路径]", 1)[0]
    assert len(preview.encode("utf-8")) <= 2_048
    assert len(preview.splitlines()) <= 20

    modified = path.stat().st_mtime_ns
    again, second_count = await controller.offload_and_snip([_tool_message(("large-id", content))])
    assert second_count == 0 and path.stat().st_mtime_ns == modified
    assert again[0].tool_results[0].output == result.output


async def test_aggregate_offloads_minimum_largest_result(tmp_path: Path) -> None:
    controller = ContextController(tmp_path, 128_000)
    message = _tool_message(*((f"id-{index}", chr(65 + index) * 45_000) for index in range(5)))
    managed, count = await controller.offload_and_snip([message])
    assert count == 1
    remaining = sum(
        len(result.output.encode("utf-8"))
        for result in managed[0].tool_results
        if "工具结果已落盘" not in result.output
    )
    assert remaining == 180_000


async def test_keep_and_replace_decisions_freeze_and_failed_write_retries(
    tmp_path: Path, monkeypatch
) -> None:
    controller = ContextController(tmp_path, 128_000)
    kept, _ = await controller.offload_and_snip([_tool_message(("keep", "短内容"))])
    changed, _ = await controller.offload_and_snip([_tool_message(("keep", "x" * 60_000))])
    assert kept[0].tool_results[0].output == "短内容"
    assert changed[0].tool_results[0].output == "x" * 60_000

    original_write = context_module._write_once

    def fail_write(path, content):
        raise OSError("磁盘只读")

    monkeypatch.setattr(context_module, "_write_once", fail_write)
    failed, count = await controller.offload_and_snip([_tool_message(("retry", "y" * 60_000))])
    assert count == 0 and failed[0].tool_results[0].output == "y" * 60_000
    assert "retry" not in controller.seen_ids
    monkeypatch.setattr(context_module, "_write_once", original_write)
    retried, count = await controller.offload_and_snip([_tool_message(("retry", "y" * 60_000))])
    assert count == 1 and "工具结果已落盘" in retried[0].tool_results[0].output


async def test_concurrent_offload_uses_one_frozen_replacement(tmp_path: Path) -> None:
    controller = ContextController(tmp_path, 128_000)
    first, second = await asyncio.gather(
        controller.offload_and_snip([_tool_message(("same", "a" * 60_000))]),
        controller.offload_and_snip([_tool_message(("same", "b" * 60_000))]),
    )
    assert first[0][0].tool_results[0].output == second[0][0].tool_results[0].output
    assert sum((first[1], second[1])) == 1


def test_usage_anchor_replaces_and_estimates_only_increment(tmp_path: Path) -> None:
    controller = ContextController(tmp_path, 128_000)
    base = (ChatMessage("user", "a" * 350),)
    controller.update_usage(
        base,
        input_tokens=100,
        output_tokens=20,
        cache_creation_input_tokens=30,
        cache_read_input_tokens=40,
    )
    assert controller.usage_anchor == 190
    assert controller.estimate_tokens(base) == 190
    grown = (*base, ChatMessage("assistant", "b" * 350))
    assert controller.estimate_tokens(grown) == 293
    controller.update_usage(
        grown,
        input_tokens=10,
        output_tokens=5,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    assert controller.usage_anchor == 15
