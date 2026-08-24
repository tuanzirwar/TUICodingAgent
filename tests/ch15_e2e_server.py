"""第 15 章 Team 真实终端验收用确定性 SSE 服务。"""

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

        if "<team-context>" in system:
            if messages and messages[-1].get("role") == "tool":
                self._text("BOB_CHILD_DONE")
            elif "再写" in latest:
                self._tool(
                    "bob-write-world",
                    "write_file",
                    {"path": "team-result.txt", "content": "hello\nworld\n"},
                )
            else:
                self._tool(
                    "bob-write-hello",
                    "write_file",
                    {"path": "team-result.txt", "content": "hello\n"},
                )
            return

        if messages and messages[-1].get("role") == "tool":
            previous = messages[-2]
            calls = previous.get("tool_calls", [])
            name = calls[0].get("function", {}).get("name", "") if calls else ""
            labels = {
                "TeamCreate": "TEAM_CREATED",
                "Agent": "BOB_STARTED",
                "SendMessage": "BOB_CONTINUED",
                "TeamDelete": "TEAM_DELETED",
            }
            self._text(labels.get(name, "UNEXPECTED_TOOL_RESULT"))
            return

        if "创建团队" in latest:
            self._tool("create-team", "TeamCreate", {"team_name": "inproc"})
        elif "启动队员" in latest:
            self._tool(
                "spawn-bob",
                "Agent",
                {
                    "team_name": "inproc",
                    "name": "bob",
                    "subagent_type": "general-purpose",
                    "description": "写入团队验收文件",
                    "prompt": "在独立 Worktree 写 team-result.txt",
                },
            )
        elif "续派队员" in latest:
            self._tool(
                "continue-bob",
                "SendMessage",
                {"to": "bob", "content": "再写一行 world"},
            )
        elif "删除团队" in latest:
            self._tool("delete-team", "TeamDelete", {"team_name": "inproc", "force": True})
        else:
            self._text("UNEXPECTED_REQUEST")

    def _start(self) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream; charset=utf-8")
        self.send_header("connection", "close")
        self.end_headers()

    def _text(self, value: str) -> None:
        print(f"CH15 {value}", flush=True)
        self._start()
        event = {"choices": [{"delta": {"content": value}}]}
        self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True

    def _tool(self, identifier: str, name: str, arguments: dict[str, object]) -> None:
        print(f"CH15 TOOL {name}", flush=True)
        self._start()
        call = {
            "index": 0,
            "id": identifier,
            "function": {
                "name": name,
                "arguments": json.dumps(arguments, ensure_ascii=False),
            },
        }
        event = {"choices": [{"delta": {"tool_calls": [call]}}]}
        self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True

    def log_message(self, format: str, *args: object) -> None:
        return None


if __name__ == "__main__":
    print("CH15 server listening on 127.0.0.1:8770", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8770), Handler).serve_forever()
