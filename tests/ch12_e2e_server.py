"""第 12 章 Hook 真实终端验收用确定性 SSE 服务。"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        payload = json.loads(self.rfile.read(length))
        messages = payload.get("messages", [])
        latest = str(messages[-1].get("content", "")) if messages else ""
        if messages and messages[-1].get("role") == "tool":
            result = json.loads(latest)
            previous = messages[-2] if len(messages) >= 2 else {}
            calls = previous.get("tool_calls", []) if isinstance(previous, dict) else []
            function = calls[0].get("function", {}) if calls else {}
            tool_name = function.get("name", "") if isinstance(function, dict) else ""
            if tool_name == "write_file":
                ok = not result.get("success") and "[hook deny-write] blocked by hook" in str(
                    result.get("error", "")
                )
                self._text("PRETOOL_BLOCK_OK" if ok else "PRETOOL_BLOCK_FAILED")
            elif tool_name == "read_file":
                self._text("POSTTOOL_CONTINUED_OK")
            else:
                self._text("TOOL_OK")
            return
        if "Hook工具拦截验收" in latest:
            self._tool(
                "write-hook",
                "write_file",
                {"path": "blocked.txt", "content": "不应写入"},
            )
            return
        if "异步后置验收" in latest:
            self._tool("read-hook", "read_file", {"path": "README.md"})
            return
        if "首轮验收" in latest:
            result = "SESSION_PROMPT_OK" if "用 zh-CN 回复" in latest else "SESSION_PROMPT_FAILED"
            self._text(result)
            return
        if "重置验收" in latest:
            self._text("CLEAR_SESSION_OK" if "用 zh-CN 回复" in latest else "CLEAR_SESSION_FAILED")
            return
        self._text("UNEXPECTED_REQUEST")

    def _start(self) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream; charset=utf-8")
        self.send_header("connection", "close")
        self.end_headers()

    def _text(self, value: str) -> None:
        print(f"CH12 {value}", flush=True)
        self._start()
        event = {"choices": [{"delta": {"content": value}}]}
        self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True

    def _tool(self, identifier: str, name: str, arguments: dict[str, str]) -> None:
        print(f"CH12 TOOL {name}", flush=True)
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
    print("CH12 server listening on 127.0.0.1:8767", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8767), Handler).serve_forever()
