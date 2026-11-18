"""真实记忆提取、独立工作区配对召回与源文件漂移实验。"""
# ruff: noqa: ASYNC240

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import time
from pathlib import Path

from ..config import ConfigLoader
from ..memory import MemoryManager
from ..models import ChatMessage, ToolCall
from ..providers import create_provider
from ..tools.registry import create_default_registry
from ..trace import TraceRecorder, TracingProvider
from .harness_live import BudgetProvider, episode, json_answer

TASKS = (
    "retry_policy", "route_prefix", "cache_namespace", "date_format",
    "pagination_limit", "worker_pool", "log_level", "locale_fallback",
    "feature_switch", "import_order", "artifact_suffix", "report_header",
)


def fixture(index: int) -> tuple[str, str, str]:
    name = TASKS[index]
    stamp = hashlib.sha256(("frozen-memory-extraction-v1/" + name).encode()).hexdigest()[:8]
    value = f"{name}-contract-{index + 17}-{stamp}"
    filename = f"config/{name}.json"
    return name, filename, value


def prompt_for(filename: str) -> str:
    return (
        f"请给出 {filename} 中的 value 配置值。若可靠项目记忆已包含该事实，可直接回答；"
        "否则用工具核对原文件。若原文件不存在则回答 missing。"
        '只输出 JSON 对象，例如 {"value":"..."}，不要修改任何文件。'
    )


async def extract(root: Path, filename: str, config, ledger: Path, maximum: int) -> dict:
    memory = MemoryManager(root, root / "isolated-home")
    call = ToolCall("observed-source", "read_file", {"path": filename})
    result = await create_default_registry(root).execute(call)
    if not result.success:
        raise RuntimeError("初始读取失败，不能进行提取实验")
    messages = (
        ChatMessage("user", "请将刚确认的文件配置约定保存为项目知识，以便后续任务召回。"),
        ChatMessage("assistant", tool_calls=(call,)),
        ChatMessage("tool", tool_results=(result,)),
    )
    # 不提供 explicit_user_text：禁用显式指令的确定性内容回退，不手写记忆操作。
    traced = TracingProvider(create_provider(config), TraceRecorder(root))
    traced.set_context("memory-extraction")
    provider = BudgetProvider(traced, ledger, maximum)
    started = time.perf_counter()
    before = json.loads(ledger.read_text(encoding="utf-8"))["attempted"]
    try:
        updated = await memory.update(provider, messages)
    finally:
        await provider.close()
    after = json.loads(ledger.read_text(encoding="utf-8"))["attempted"]
    notes = {
        str(path.relative_to(root)): path.read_text(encoding="utf-8")
        for directory in (memory.project_dir, memory.user_dir)
        for path in directory.glob("*.md") if path.name != "MEMORY.md"
    }
    return {"reported_updated": updated, "requests": after - before,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "observed_read": {"arguments": call.arguments, "output": result.output, "metadata": result.metadata},
            "notes": notes, "index": memory.index_text, "budget_exhausted": provider.exhausted}


async def run(output: Path, config_path: Path, maximum: int, provider_index: int) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    ledger = output / "api_usage.json"
    if ledger.exists():
        raise ValueError("使用新目录，禁止覆盖已发请求账本")
    ledger.write_text(json.dumps({"attempted": 0, "maximum": maximum}), encoding="utf-8")
    config = ConfigLoader.load(config_path).providers[provider_index]
    order = list(range(len(TASKS)))
    random.Random(20261010).shuffle(order)
    data = {"planned_pairs": len(TASKS), "pairs": [], "drift": []}
    manifest = {"provider": config.name, "model": config.model, "wire_api": config.wire_api,
                "maximum_requests": maximum, "pair_order": order,
                "design": "12 independent workspaces, one exact file fact each; real LLM extraction from actual observed ReadFile result; fixture initial reading is not autonomous exploration",
                "fallback_disabled": True, "suite_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    def save():
        data["usage"] = json.loads(ledger.read_text(encoding="utf-8"))
        (output / "result.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    save()
    for position, index in enumerate(order):
        if maximum - data["usage"]["attempted"] < 4:
            break
        name, filename, value = fixture(index)
        row = {"task": name, "index": index, "expected": value, "arms": {}, "arm_order": [False, True] if position % 2 == 0 else [True, False]}
        for enabled in row["arm_order"]:
            root = output / "workspace" / f"{index}-{int(enabled)}"
            (root / "config").mkdir(parents=True)
            (root / filename).write_text(json.dumps({"value": value}), encoding="utf-8")
            memory = None
            if enabled:
                extraction = await extract(root, filename, config, ledger, maximum)
                extraction["exact_fact_stored"] = any(value in text for text in extraction["notes"].values())
                row["extraction"] = extraction
                memory = MemoryManager(root, root / "isolated-home")
            observation = await episode(root, config, ledger, maximum, prompt_for(filename), memory=memory)
            observation["source_read_calls"] = sum(
                event["tool"] == "read_file" and event["arguments"].get("path") == filename
                for event in observation["trajectory"] if event["type"] == "tool_finished"
            )
            observation["correct"] = json_answer(observation["final_text"]).get("value") == value
            row["arms"][str(int(enabled))] = observation
            # 每个 arm 后即保存，服务失败也保留实际轨迹和请求分母。
            data["current_pair"] = row
            save()
            print(json.dumps({"task": name, "enabled": enabled, "correct": observation["correct"], "reads": observation["source_read_calls"], "requests_used": data["usage"]["attempted"]}), flush=True)
            if observation["error"] or observation["budget_exhausted"]:
                break
        row["paired_complete"] = len(row["arms"]) == 2
        data["pairs"].append(row)
        data.pop("current_pair", None)
        save()
        if not row["paired_complete"] or any(arm["error"] for arm in row["arms"].values()):
            break
    # 漂移实验复用已经真实提取的记忆，外部修改/删除原文件，不预先通知模型。
    for index, action in ((order[0], "changed"), (order[1], "deleted")):
        if maximum - data["usage"]["attempted"] < 3:
            break
        paired = next((row for row in data["pairs"] if row["index"] == index and row["paired_complete"]), None)
        if paired is None:
            continue
        _, filename, old = fixture(index)
        root = output / "workspace" / f"{index}-1"
        expected = old + "-v2" if action == "changed" else "missing"
        if action == "changed":
            (root / filename).write_text(json.dumps({"value": expected}), encoding="utf-8")
        else:
            (root / filename).unlink()
        memory = MemoryManager(root, root / "isolated-home")
        observation = await episode(root, config, ledger, maximum, prompt_for(filename), memory=memory)
        data["drift"].append({"action": action, "index": index, "expected": expected,
                              "old_value": old, "correct": json_answer(observation["final_text"]).get("value") == expected,
                              "stale_value_returned": json_answer(observation["final_text"]).get("value") == old, **observation})
        save()
        print(json.dumps({"drift": action, "correct": data["drift"][-1]["correct"], "requests_used": data["usage"]["attempted"]}), flush=True)
    data["not_run_indices"] = [index for index in order if not any(pair["index"] == index for pair in data["pairs"])]
    save()
    return data


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--maximum", type=int, default=64)
    parser.add_argument("--provider-index", type=int, default=0)
    args = parser.parse_args()
    asyncio.run(run(args.output.resolve(), args.config, args.maximum, args.provider_index))


if __name__ == "__main__":
    main()
