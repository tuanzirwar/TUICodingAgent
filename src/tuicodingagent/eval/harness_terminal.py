"""在真实终端启动本地 HTTP 合同服务与完整 TUI。"""

from __future__ import annotations

import argparse
import importlib.util
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from ..models import AppConfig, ProviderConfig
from ..tui.app import TUICodingAgentApp


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    root = args.workspace.resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text(
        "# Harness E2E\n\n真实终端测试事实：青鸟-20261010。\n", encoding="utf-8"
    )
    source = Path(__file__).resolve().parents[3] / "tests" / "e2e_sse_server.py"
    spec = importlib.util.spec_from_file_location("local_e2e", source)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    server = ThreadingHTTPServer(("127.0.0.1", 0), fixture.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    (root / "endpoint.json").write_text(
        json.dumps({"port": port, "mode": "local-http"}), encoding="utf-8"
    )
    config = AppConfig(
        (
            ProviderConfig(
                "本地 HTTP 合同",
                "openai",
                "fixture",
                "e2e-test-key",
                base_url=f"http://127.0.0.1:{port}/v1/chat/completions",
            ),
        )
    )
    try:
        TUICodingAgentApp(config, workspace=root).run()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
