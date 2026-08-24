"""第 14 章 Worktree 真实终端验收用确定性 SSE 服务。"""

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
        latest = str(messages[-1].get("content", "")) if messages else ""

        if "WORKTREE_E2E_ROLE" in system:
            if messages and messages[-1].get("role") == "tool":
                self._text("ISOLATED_CHILD_OK")
            else:
                self._tool(
                    "isolated-write",
                    "write_file",
                    {"path": "server.py", "content": "VALUE = 'isolated'\n"},
                )
            return

        if messages and messages[-1].get("role") == "tool":
            previous = messages[-2]
            calls = previous.get("tool_calls", [])
            function = calls[0].get("function", {}) if calls else {}
            name = function.get("name", "")
            result = json.loads(latest)
            if name == "write_file":
                self._text("MANUAL_WRITE_OK")
            elif name == "Agent":
                ok = "ISOLATED_CHILD_OK" in str(
                    result.get("output", "")
                ) and "Worktree 保留在" in str(result.get("output", ""))
                self._text("ISOLATION_PARENT_OK" if ok else "ISOLATION_PARENT_FAILED")
            else:
                self._text("UNEXPECTED_TOOL_RESULT")
            return

        if "手动目录写入验收" in latest:
            self._tool(
                "manual-write",
                "write_file",
                {"path": "manual.txt", "content": "MANUAL_ONLY\n"},
            )
        elif "隔离子任务验收" in latest:
            self._tool(
                "isolated-agent",
                "Agent",
                {
                    "prompt": "修改 server.py",
                    "description": "Worktree 隔离修改",
                    "subagent_type": "isolated-e2e",
                },
            )
        else:
            self._text("UNEXPECTED_REQUEST")

    def _start(self) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream; charset=utf-8")
        self.send_header("connection", "close")
        self.end_headers()

    def _text(self, value: str) -> None:
        print(f"CH14 {value}", flush=True)
        self._start()
        event = {"choices": [{"delta": {"content": value}}]}
        self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True

    def _tool(self, identifier: str, name: str, arguments: dict[str, object]) -> None:
        print(f"CH14 TOOL {name}", flush=True)
        self._start()
        call = {
            "index": 0,
            "id": identifier,
            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
        }
        event = {"choices": [{"delta": {"tool_calls": [call]}}]}
        self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True

    def log_message(self, format: str, *args: object) -> None:
        return None


if __name__ == "__main__":
    print("CH14 server listening on 127.0.0.1:8769", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8769), Handler).serve_forever()
