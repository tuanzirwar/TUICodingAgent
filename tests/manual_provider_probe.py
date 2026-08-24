"""手动验证真实 Provider 的最小流式探针；不会输出密钥。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from tuicodingagent.config import ConfigLoader
from tuicodingagent.models import ChatMessage
from tuicodingagent.providers.factory import create_provider


async def main() -> None:
    index = int(os.environ.get("TUICODINGAGENT_PROBE_PROVIDER", "0"))
    provider = create_provider(ConfigLoader.load(Path("config.yaml")).providers[index])
    try:
        async for event in provider.stream(
            "你是 TUICodingAgent 的真实 API 验收助手。",
            [ChatMessage("user", "只回复：TUICodingAgent API OK")],
        ):
            if event.text:
                print(event.text, end="", flush=True)
            if event.input_tokens or event.output_tokens:
                print(
                    f"\n[usage input={event.input_tokens} output={event.output_tokens}]",
                    flush=True,
                )
        print()
    finally:
        await provider.close()


if __name__ == "__main__":
    asyncio.run(main())
