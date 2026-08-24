"""第 13 章 SubAgent 真实终端验收用确定性 SSE 服务。"""

from __future__ import annotations

import json
import time
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

        if "CUSTOM_E2E_ROLE" in system:
            if messages and messages[-1].get("role") == "tool":
                self._text("CUSTOM_ROLE_OK")
            else:
                self._tool("custom-read", "read_file", {"path": "README.md"})
            return
        if "Explore 子 Agent" in system:
            self._text("EXPLORE_CHILD_OK")
            return
        if "<fork_boilerplate>" in latest:
            time.sleep(0.3)
            self._text("Scope: FORK_CHILD_OK")
            return
        if "FAIL_CHILD" in latest and "通用子 Agent" in system:
            self.send_error(500, "SUBAGENT_E2E_BOOM")
            return

        if messages and messages[-1].get("role") == "tool":
            result = json.loads(latest)
            previous = messages[-2]
            calls = previous.get("tool_calls", [])
            function = calls[0].get("function", {}) if calls else {}
            tool_name = function.get("name", "")
            arguments = json.loads(function.get("arguments", "{}"))
            if tool_name == "Agent":
                if "EXPLORE_CHILD_OK" in str(result.get("output", "")):
                    self._text("DEFINED_PARENT_OK")
                elif arguments.get("prompt") == "FAIL_CHILD":
                    self._text("FAILURE_LAUNCHED_OK")
                elif arguments.get("subagent_type") == "custom-e2e":
                    self._text("CUSTOM_PARENT_OK")
                else:
                    self._text("FORK_LAUNCHED_OK")
            elif tool_name == "TaskList":
                outer = json.loads(latest)
                tasks = json.loads(outer.get("output", "[]"))
                target = next(
                    (item["id"] for item in reversed(tasks) if item["status"] == "completed"),
                    tasks[-1]["id"],
                )
                self._tool("task-get", "TaskGet", {"task_id": target})
            elif tool_name == "TaskGet":
                self._text("TASK_QUERY_OK")
            else:
                self._text("UNEXPECTED_TOOL_RESULT")
            return

        if "定义式探索验收" in latest:
            self._tool(
                "agent-defined",
                "Agent",
                {
                    "prompt": "探索 README",
                    "description": "只读探索",
                    "subagent_type": "explore",
                },
            )
        elif "Fork后台验收" in latest:
            self._tool(
                "agent-fork",
                "Agent",
                {"prompt": "检查父上下文", "description": "Fork 检查", "name": "forker"},
            )
        elif "失败通知验收" in latest:
            ok = "<task-notification>" in latest and "failed" in latest
            self._text("FAILURE_ISOLATED_OK" if ok else "FAILURE_ISOLATED_FAILED")
        elif "通知验收" in latest:
            ok = "<task-notification>" in latest and "FORK_CHILD_OK" in latest
            self._text("NOTIFICATION_OK" if ok else "NOTIFICATION_FAILED")
        elif "任务查询验收" in latest:
            self._tool("task-list", "TaskList", {})
        elif "自定义角色验收" in latest:
            self._tool(
                "agent-custom",
                "Agent",
                {
                    "prompt": "按角色执行",
                    "description": "自定义角色",
                    "subagent_type": "custom-e2e",
                },
            )
        elif "失败启动验收" in latest:
            self._tool(
                "agent-fail",
                "Agent",
                {
                    "prompt": "FAIL_CHILD",
                    "description": "失败隔离",
                    "subagent_type": "general-purpose",
                    "run_in_background": True,
                    "name": "failer",
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
        print(f"CH13 {value}", flush=True)
        self._start()
        event = {"choices": [{"delta": {"content": value}}]}
        self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True

    def _tool(self, identifier: str, name: str, arguments: dict[str, object]) -> None:
        print(f"CH13 TOOL {name}", flush=True)
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
    print("CH13 server listening on 127.0.0.1:8768", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8768), Handler).serve_forever()
