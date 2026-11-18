"""有持久请求上限的真实模型小仓库与记忆对照实验。"""
# ruff: noqa: ASYNC240

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

from ..config import ConfigLoader
from ..errors import ProviderError
from ..memory import MemoryManager
from ..models import ChatMessage, RuntimeEventType
from ..providers import create_provider
from ..session import SessionContext, Writer, restore_session, scan_sessions
from .harness_regression import ScriptedProvider, _runtime


class BudgetProvider:
    """发出请求之前 fsync 计数，失败和在途请求也占预算。"""

    def __init__(self, provider, ledger: Path, maximum: int) -> None:
        self.provider = provider
        self.config = provider.config
        self.ledger = ledger
        self.maximum = maximum
        self.exhausted = False

    async def stream(self, system_prompt, messages, tools=()):
        import os

        usage = json.loads(self.ledger.read_text(encoding="utf-8"))
        if usage["attempted"] >= self.maximum:
            self.exhausted = True
            raise ProviderError("实验请求预算耗尽")
        usage["attempted"] += 1
        with self.ledger.open("w", encoding="utf-8") as stream:
            json.dump(usage, stream)
            stream.flush()
            os.fsync(stream.fileno())
        async with asyncio.timeout(90):
            async for event in self.provider.stream(system_prompt, messages, tools):
                yield event

    async def close(self):
        await self.provider.close()


async def episode(
    root: Path,
    config,
    ledger: Path,
    maximum: int,
    prompt: str,
    *,
    memory=None,
    iterations=3,
    restored=None,
) -> dict:
    provider = BudgetProvider(create_provider(config), ledger, maximum)
    runtime = _runtime(root, provider, memory=memory, iterations=iterations)
    if restored is not None:
        await runtime.restore(restored, Writer(restored.info.context, config.model))
    events = []
    started = time.perf_counter()
    error = ""
    try:
        async with asyncio.timeout(180):
            async for event in runtime.chat(prompt):
                events.append(event)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        await runtime.close()
    tools = [e for e in events if e.type == RuntimeEventType.TOOL_FINISHED]
    text = next((e.text for e in reversed(events) if e.type == RuntimeEventType.COMPLETED), "")
    failure = next((e.text for e in events if e.type == RuntimeEventType.FAILED), error)
    limited = (
        any(e.type == RuntimeEventType.LIMIT_REACHED for e in events)
        or provider.exhausted
        or "实验请求预算耗尽" in failure
    )
    trajectory = [
        {
            "type": e.type.value,
            "text": e.text,
            "tool": e.tool_call.name if e.tool_call else None,
            "arguments": e.tool_call.arguments if e.tool_call else None,
            "success": e.tool_result.success if e.tool_result else None,
            "output": e.tool_result.model_content() if e.tool_result else None,
        }
        for e in events
    ]
    return {
        "final_text": text,
        "error": failure,
        "budget_exhausted": limited,
        "read_calls": sum(e.tool_call.name == "read_file" for e in tools),
        "tool_calls": len(tools),
        "tool_failures": sum(not e.tool_result.success for e in tools),
        "input_tokens_reported": runtime.total_input_tokens,
        "output_tokens_reported": runtime.total_output_tokens,
        "time_to_first_edit_seconds": next(
            (e.elapsed for e in tools if e.tool_call.name in {"write_file", "edit_file"}), None
        ),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "trajectory": trajectory,
    }


def json_answer(text: str) -> dict:
    try:
        first, last = text.index("{"), text.rindex("}")
        data = json.loads(text[first : last + 1])
        return data if isinstance(data, dict) else {}
    except (ValueError, json.JSONDecodeError):
        return {}


async def verify(root: Path) -> dict:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "-p",
        "no:randomly",
        cwd=root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 40)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    return {"exit_code": process.returncode, "output": output.decode("utf-8", errors="replace")}


async def run(output: Path, config_path: Path, maximum: int, provider_index: int) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    ledger = output / "api_usage.json"
    if ledger.exists():
        raise ValueError("请使用新的实验目录，避免覆盖已发请求账本")
    ledger.write_text(json.dumps({"attempted": 0, "maximum": maximum}), encoding="utf-8")
    config = ConfigLoader.load(config_path).providers[provider_index]
    manifest = {
        "provider": config.name,
        "model": config.model,
        "wire_api": config.wire_api,
        "maximum_requests": maximum,
        "memory_seed": "fixture facts via MemoryManager",
        "memory_design": "4 paired episodes, 3 file facts per episode; 12 correlated facts",
        "suite_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    rows = []

    def save():
        (output / "result.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    for batch in range(4):
        expected = {
            f"service_{batch}_{j}": f"retry={j + 2};timeout={17 + batch * 3 + j}" for j in range(3)
        }
        for enabled in (False, True):
            root = output / "workspace" / f"memory-{batch}-{int(enabled)}"
            root.mkdir(parents=True)
            for name, fact in expected.items():
                (root / f"{name}.txt").write_text(fact, encoding="utf-8")
            memory = None
            if enabled:
                memory = MemoryManager(root, root / "isolated-home")
                operations = [
                    {
                        "action": "create",
                        "level": "project",
                        "type": "project_knowledge",
                        "title": name,
                        "slug": name,
                        "content": fact,
                    }
                    for name, fact in expected.items()
                ]
                await memory.update(ScriptedProvider((json.dumps(operations),)), ())
                memory = MemoryManager(root, root / "isolated-home")
            prompt = (
                "请给出这三个服务的重试次数与超时值："
                + ", ".join(expected)
                + "。如果已有可靠项目记忆，不必重复读取；否则查看同名 .txt 文件。"
                + '仅输出 JSON 对象，每个服务的值格式为 "retry=N;timeout=N"。'
            )
            observation = await episode(root, config, ledger, maximum, prompt, memory=memory)
            answer = json_answer(observation["final_text"])
            correct = sum(answer.get(name) == fact for name, fact in expected.items())
            rows.append(
                {
                    "family": "memory",
                    "batch": batch,
                    "enabled": enabled,
                    "correct_facts": correct,
                    "total_facts": 3,
                    "passed": correct == 3 and not observation["error"],
                    **observation,
                }
            )
            save()
            print(
                json.dumps(
                    {
                        "family": "memory",
                        "batch": batch,
                        "enabled": enabled,
                        "correct": correct,
                        "reads": observation["read_calls"],
                    }
                ),
                flush=True,
            )
            if observation["error"]:
                break
        if rows[-1]["error"]:
            break

    tasks = [
        (
            "deduplicate",
            "def unique(values):\n    return list(set(values))\n",
            "from implementation import unique\n"
            "def test_order():\n    assert unique(['b', 'a', 'b', 'c']) == ['b', 'a', 'c']\n"
            "def test_unhashable():\n    assert unique([[1], [1], [2]]) == [[1], [2]]\n"
            "def test_empty():\n    assert unique([]) == []\n",
            "修复 unique：按输入首次出现顺序去重，支持 list 等不可哈希元素，保持接口。",
        ),
        (
            "expiry",
            "class Cache:\n    def __init__(self):\n        self.items = {}\n"
            "    def set(self, key, value, now, ttl):\n"
            "        self.items[key] = (value, now + ttl)\n"
            "    def get(self, key, now):\n"
            "        if key not in self.items:\n            return None\n"
            "        value, expiry = self.items[key]\n"
            "        if now > expiry:\n            return None\n"
            "        return value\n",
            "from implementation import Cache\n"
            "def test_boundary():\n    c = Cache()\n    c.set('x', 42, 0, 5)\n"
            "    assert c.get('x', 4) == 42\n    assert c.get('x', 5) is None\n"
            "def test_expired_cleanup():\n    c = Cache()\n    c.set('x', 42, 0, 5)\n"
            "    c.get('x', 6)\n    assert 'x' not in c.items\n",
            "修复 Cache：到达过期时刻即失效，并删除过期条目。保持现有接口。",
        ),
    ]
    if not rows[-1]["error"]:
        for name, implementation, tests, request in tasks:
            root = output / "workspace" / name
            root.mkdir(parents=True)
            (root / "implementation.py").write_text(implementation, encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "test_contract.py").write_text(tests, encoding="utf-8")
            (root / ".gitignore").write_text(
                ".tuicodingagent/\n__pycache__/\n.pytest_cache/\n", encoding="utf-8"
            )
            for args in (
                ["init", "-q"],
                ["add", "."],
                [
                    "-c",
                    "user.name=Harness",
                    "-c",
                    "user.email=harness@example.invalid",
                    "commit",
                    "-qm",
                    "frozen fixture",
                ],
            ):
                await asyncio.to_thread(
                    subprocess.run, ["git", *args], cwd=root, check=True, stdout=subprocess.DEVNULL
                )
            baseline = await verify(root)
            observation = await episode(
                root,
                config,
                ledger,
                maximum,
                request + "读取 implementation.py 与 tests/test_contract.py，修改实现并运行测试。"
                "不要修改测试。工作区只有这些文件，无需搜索项目配置。",
                iterations=6,
            )
            after = await verify(root)
            tests_unchanged = (root / "tests" / "test_contract.py").read_text(
                encoding="utf-8"
            ) == tests
            diff = await asyncio.to_thread(
                subprocess.run,
                ["git", "diff"],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            (output / f"{name}.patch").write_text(diff.stdout, encoding="utf-8")
            rows.append(
                {
                    "family": "coding",
                    "task": name,
                    "baseline": baseline,
                    "verifier": after,
                    "tests_unchanged": tests_unchanged,
                    "passed": baseline["exit_code"] != 0
                    and after["exit_code"] == 0
                    and tests_unchanged
                    and not observation["budget_exhausted"]
                    and not observation["error"],
                    **observation,
                }
            )
            save()
            print(
                json.dumps({"family": "coding", "task": name, "passed": rows[-1]["passed"]}),
                flush=True,
            )
            if observation["error"]:
                break

    # 余额允许时补测旧记忆与文件变化的冲突；与无变化收益单独计分。
    usage = json.loads(ledger.read_text(encoding="utf-8"))
    if usage["attempted"] <= maximum - 2 and rows and not rows[-1]["error"]:
        root = output / "workspace" / "drift"
        root.mkdir(parents=True)
        file = root / "service.txt"
        file.write_text("timeout=10", encoding="utf-8")
        context = SessionContext.create(root)
        from ..models import ToolCall
        from ..tools.registry import create_default_registry

        call = ToolCall("read", "read_file", {"path": "service.txt"})
        result = await create_default_registry(root).execute(call)
        with Writer(context, config.model) as writer:
            writer.append(ChatMessage("user", "查询服务设置"))
            writer.append(ChatMessage("assistant", tool_calls=(call,)))
            writer.append(ChatMessage("tool", tool_results=(result,)))
        file.write_text("timeout=37", encoding="utf-8")
        restored = restore_session(scan_sessions(root)[0])
        observation = await episode(
            root,
            config,
            ledger,
            maximum,
            '服务当前 timeout 是多少？仅输出 JSON 对象 {"timeout": 数值}。',
            restored=restored,
        )
        rows.append(
            {
                "family": "drift",
                "passed": json_answer(observation["final_text"]).get("timeout") == 37
                and observation["read_calls"] >= 1,
                **observation,
            }
        )
        save()
    usage = json.loads(ledger.read_text(encoding="utf-8"))
    print(json.dumps({"attempted": usage["attempted"], "episodes": len(rows)}), flush=True)
    return {"cases": rows, "usage": usage}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--maximum", type=int, default=24)
    parser.add_argument("--provider-index", type=int, default=0)
    args = parser.parse_args()
    asyncio.run(run(args.output.resolve(), args.config, args.maximum, args.provider_index))


if __name__ == "__main__":
    main()
