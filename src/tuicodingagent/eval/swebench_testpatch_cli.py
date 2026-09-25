"""带 test_patch 注入和 baseline gate 的 SWE-bench Live CLI。"""

from __future__ import annotations

from .swebench_testpatch_cli_v2 import main

if __name__ == "__main__":
    raise SystemExit(main())
