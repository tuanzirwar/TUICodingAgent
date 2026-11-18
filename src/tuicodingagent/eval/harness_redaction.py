"""仅使用人工假数据检查持久工件的脱敏边界。"""
# ruff: noqa: ASYNC240

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import httpx

from ..context import ContextController
from ..memory import MemoryManager
from ..models import ChatMessage, ToolResult
from ..providers.base import _safe_response_detail
from ..session import SessionContext, Writer
from ..trace import TraceEvent, TraceRecorder
from .harness_regression import ScriptedProvider

FAKE_VALUES = {
    "api_key": "sk-proj-HarnessFakeOnly1234567890",
    "bearer": "HarnessBearerFakeOnly1234567890",
    "database_password": "HarnessPasswordFakeOnly1234567890",
    "email": "harness-person@example.invalid",
}
PAYLOAD = (
    f"API_KEY={FAKE_VALUES['api_key']}\n"
    f"Authorization: Bearer {FAKE_VALUES['bearer']}\n"
    f"postgresql://fixture:{FAKE_VALUES['database_password']}@localhost/demo\n"
    f"contact={FAKE_VALUES['email']}\n"
)


async def run(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    if (output / "result.json").exists():
        raise ValueError("使用新目录，避免覆盖失败证据")
    root = output / "workspace"
    root.mkdir()
    context = SessionContext.create(root)
    with Writer(context, "fixture") as writer:
        writer.append(ChatMessage("user", PAYLOAD))
        writer.append(
            ChatMessage("tool", tool_results=(ToolResult("secret", "read_file", True, PAYLOAD),))
        )
    recorder = TraceRecorder.for_workspace(root)
    recorder.record(TraceEvent("probe", metadata={"detail": PAYLOAD}).finish())
    controller = ContextController(root, 80_000, context)
    await controller.offload_and_snip(
        (
            ChatMessage(
                "tool",
                tool_results=(
                    ToolResult("spill", "read_file", True, PAYLOAD + "padding\n" * 10_000),
                ),
            ),
        )
    )
    manager = MemoryManager(root, root / "isolated-home")
    operation = {
        "action": "create",
        "level": "project",
        "type": "project_knowledge",
        "title": "假数据脱敏测试",
        "slug": "fake_security_probe",
        "content": PAYLOAD,
    }
    memory_saved = await manager.update(ScriptedProvider((json.dumps([operation]),)), ())
    response = httpx.Response(401, json={"error": {"message": "invalid " + FAKE_VALUES["api_key"]}})
    provider_detail = _safe_response_detail(response, FAKE_VALUES["api_key"])
    candidates = {
        "provider_error": provider_detail,
        "session": context.conversation_path.read_text(encoding="utf-8"),
        "trace_metadata": recorder.path.read_text(encoding="utf-8"),
        "artifact": "\n".join(p.read_text(encoding="utf-8") for p in context.spill_dir.glob("*")),
        "memory": "\n".join(
            p.read_text(encoding="utf-8") for p in manager.project_dir.glob("*.md")
        ),
    }
    cases = []
    for name, text in candidates.items():
        exposed = {key: value in text for key, value in FAKE_VALUES.items()}
        cases.append(
            {
                "sink": name,
                "scope": "configured API key"
                if name == "provider_error"
                else "general sensitive text",
                "exposed": exposed,
                "credentials_passed": not any(
                    exposed[key] for key in ("api_key", "bearer", "database_password")
                ),
                "personal_data_exposed": exposed["email"],
                "exercised": memory_saved if name == "memory" else bool(text),
                "passed": not any(exposed.values())
                and (memory_saved if name == "memory" else bool(text)),
            }
        )
    result = {
        "real_model_requests": 0,
        "data": "synthetic fake values only",
        "total": len(cases),
        "passed": sum(c["passed"] for c in cases),
        "cases": cases,
    }
    (output / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"passed": result["passed"], "total": result["total"]}))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args().output.resolve()))


if __name__ == "__main__":
    main()
