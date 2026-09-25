"""SWE-bench Live harness 的稳定命令行入口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .adapters import EXPECTED_MODEL
from .swebench_system import SystemOptions, run_system


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 SWE-bench Live 双代理评测")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--benchmark", default="swebench_live")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--provider", default="CloudVisit")
    parser.add_argument(
        "--agent", action="append", dest="agents", choices=("tuicodingagent", "codex")
    )
    parser.add_argument("--instance-id", action="append", dest="instance_ids")
    parser.add_argument("--sample", type=int)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-iterations", type=int, default=12)
    parser.add_argument("--max-input-tokens", type=int)
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--test-command", default="")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    agents = tuple(args.agents or ("tuicodingagent", "codex"))
    options = SystemOptions(
        config_path=args.config,
        provider=args.provider,
        agents=agents,
        max_iterations=args.max_iterations,
        max_input_tokens=args.max_input_tokens,
        max_output_tokens=args.max_output_tokens,
        timeout_seconds=args.timeout,
        test_command=args.test_command,
        concurrency=args.concurrency,
        resume=args.resume,
        dry_run=args.dry_run,
    )
    try:
        results = run_system(
            dataset=args.dataset,
            output_dir=args.output_dir,
            options=options,
            sample=args.sample,
            instance_ids=set(args.instance_ids or ()),
            benchmark=args.benchmark,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "model": EXPECTED_MODEL,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                ensure_ascii=False,
            )
        )
        return 2
    if args.dry_run:
        print(
            json.dumps(
                {"status": "dry_run", "model": EXPECTED_MODEL, "agents": list(agents)},
                ensure_ascii=False,
            )
        )
        return 0
    print(json.dumps([asdict(result) for result in results], ensure_ascii=False))
    return 0 if all(result.status == "completed" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
