"""`python -m tuicodingagent.eval` 命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from .runner import run_from_config


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行一次 TUICodingAgent 无界面评测")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--provider", required=True, help="config.yaml 中的 provider 名称")
    parser.add_argument("--workspace", type=Path, required=True)
    prompt = parser.add_mutually_exclusive_group(required=True)
    prompt.add_argument("--prompt")
    prompt.add_argument("--prompt-file", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-iterations", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=300.0)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.max_iterations <= 0:
        raise SystemExit("--max-iterations 必须大于 0")
    if args.timeout <= 0:
        raise SystemExit("--timeout 必须大于 0")
    prompt = args.prompt
    if args.prompt_file is not None:
        prompt = args.prompt_file.read_text(encoding="utf-8")
    result = asyncio.run(
        run_from_config(
            config_path=args.config,
            provider_name=args.provider,
            workspace=args.workspace,
            prompt=prompt,
            output_dir=args.output_dir,
            max_iterations=args.max_iterations,
            timeout_seconds=args.timeout,
        )
    )
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
