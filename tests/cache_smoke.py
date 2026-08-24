"""第 5 章本地兼容端点缓存字段烟囱程序。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from tuicodingagent.models import ProviderConfig
from tuicodingagent.providers import OpenAIProvider
from tuicodingagent.runtime import ConversationRuntime


async def main() -> None:
    config = ProviderConfig(
        "缓存烟囱",
        "openai",
        "deterministic-test-model",
        "e2e-test-key",
        "http://127.0.0.1:8765/v1/chat/completions",
    )
    runtime = ConversationRuntime(OpenAIProvider(config), workspace=Path.cwd())
    try:
        _ = [event async for event in runtime.chat("环境感知验收")]
        print(
            "cache_creation_input_tokens="
            f"{runtime.total_cache_creation_input_tokens} "
            f"cache_read_input_tokens={runtime.total_cache_read_input_tokens}"
        )
    finally:
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
