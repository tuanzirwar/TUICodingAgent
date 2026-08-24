"""第 11 章真实终端验收用确定性 OpenAI SSE 服务。"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        payload = json.loads(self.rfile.read(length))
        messages = payload.get("messages", [])
        system = "\n".join(
            str(message.get("content", ""))
            for message in messages
            if message.get("role") == "system"
        )
        users = [
            str(message.get("content", "")) for message in messages if message.get("role") == "user"
        ]
        latest = users[-1] if users else ""
        if "目录验收" in latest:
            ok = (
                "## Available Skills" in system
                and "test: 端到端热更新 Skill" in system
                and "E2E-SOP-V1" not in system
            )
            reply = "CATALOG_ONLY_OK" if ok else "CATALOG_ONLY_FAILED"
        elif "Skill `test`" in latest:
            if "E2E-SOP-V2" in system:
                reply = "INLINE_V2_OK"
            elif "E2E-SOP-V1" in system:
                reply = "INLINE_V1_OK"
            else:
                reply = "INLINE_FAILED"
        elif "Skill `review`" in latest:
            reply = "REVIEW_FORK_OK" if "## Active Skills" in system else "REVIEW_FORK_FAILED"
        elif "清理验收" in latest:
            reply = "CLEAR_OK" if "## Active Skills" not in system else "CLEAR_FAILED"
        else:
            reply = "OK"
        print(f"CH11 {reply}", flush=True)
        self._send_text(reply)

    def _send_text(self, text: str) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream; charset=utf-8")
        self.send_header("connection", "close")
        self.end_headers()
        event = {"choices": [{"delta": {"content": text}}]}
        self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True

    def log_message(self, format: str, *args: object) -> None:
        return None


if __name__ == "__main__":
    print("CH11 server listening on 127.0.0.1:8766", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8766), Handler).serve_forever()
