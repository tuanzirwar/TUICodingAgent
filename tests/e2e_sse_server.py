"""第 2～4 章本地端到端测试用 OpenAI 兼容 SSE 服务。"""

from __future__ import annotations

import json
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SUMMARY_PATH = str(Path(tempfile.gettempdir()) / "tuicodingagent-e2e-summary.md")
PLAN_RESULT_PATH = str(Path(tempfile.gettempdir()) / "tuicodingagent-e2e-plan-result.md")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        payload = json.loads(self.rfile.read(length))
        if self.headers.get("authorization") != "Bearer e2e-test-key":
            body = json.dumps({"error": {"message": "invalid key"}}).encode()
            self.send_response(401)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        messages = payload.get("messages", [])
        system_text = "\n".join(
            str(message.get("content", ""))
            for message in messages
            if message.get("role") == "system"
        )
        if "负责更新编程助手的长期记忆" in system_text:
            self._send_text(
                '[{"action":"create","level":"project",'
                '"type":"project_knowledge","title":"验收代号",'
                '"slug":"acceptance_code","content":"项目验收代号是青鸟-42。"}]',
                delay=0.01,
            )
            return
        if "九个编号部分" in system_text:
            self._send_text(
                "<analysis>梳理历史与当前进度。</analysis><summary>\n"
                "1. 主要请求和意图：继续完成端到端验收。\n"
                "2. 关键技术概念：上下文压缩。\n"
                "3. 文件和代码段：测试工作区。\n"
                "4. 错误和修复：无。\n"
                "5. 问题解决过程：已完成普通对话。\n"
                "6. 所有用户消息原文：保留此前用户请求。\n"
                "7. 待办任务：验证压缩后继续。\n"
                "8. 当前工作：正在执行第 8 章真实终端验收。\n"
                "9. 可能的下一步：发送下一条消息。\n</summary>"
            )
            return
        latest = str(messages[-1].get("content", "")) if messages else ""
        all_text = "\n".join(str(item.get("content", "")) for item in messages)
        turn = _current_turn(messages)
        tool_names = [
            item.get("function", {}).get("name", "")
            for message in turn
            for item in message.get("tool_calls", [])
        ]
        tool_ids = [
            str(item.get("id", "")) for message in turn for item in message.get("tool_calls", [])
        ]
        user_messages = "\n".join(
            str(message.get("content", "")) for message in messages if message.get("role") == "user"
        )
        if "多轮规划提示" in user_messages:
            kind = "full" if "等待用户输入 /do" in user_messages else "short"
            print(f"REMINDER iteration={len(tool_names) + 1} kind={kind}", flush=True)
        if messages and messages[-1].get("role") == "tool":
            result = json.loads(latest)
            last_tool_id = str(messages[-1].get("tool_call_id", ""))
            if "MCP权限拒绝验收" in all_text:
                if not result.get("success") and tool_names == ["mcp__local__mutate"]:
                    self._send_tool_call(
                        "mcp-adjust", "mcp__local__echo", {"text": "权限拒绝后调整"}
                    )
                else:
                    self._send_text("MCP 写操作被拒绝后，已改用只读 MCP 工具完成。")
                return
            if "MCP只读验收" in all_text:
                self._send_text(
                    "MCP 只读工具调用成功。" if result.get("success") else "MCP 工具调用失败。"
                )
                return
            if "上下文落盘验收" in all_text:
                self._send_text(
                    "大工具结果已由上下文管理器处理。"
                    if "工具结果已落盘" in latest
                    else "大工具结果未落盘。"
                )
                return
            if "权限拒绝后调整" in all_text:
                if not result.get("success") and tool_names == ["write_file"]:
                    self._send_tool_call("adjust-read", "read_file", {"path": "README.md"})
                else:
                    self._send_text("已根据权限拒绝改用只读方案并完成。")
                return
            if "危险黑名单验收" in all_text:
                self._send_text("危险命令已被黑名单拒绝，未执行。")
                return
            if "权限允许验收" in all_text:
                self._send_text(
                    "写入工具已执行。" if result.get("success") else "写入工具执行失败。"
                )
                return
            if last_tool_id.startswith("cadence-") or any(
                identifier.startswith("cadence-") for identifier in tool_ids
            ):
                if len(tool_names) < 5:
                    self._send_tool_call(
                        f"cadence-{len(tool_names) + 1}",
                        "read_file",
                        {"path": "README.md"},
                    )
                else:
                    self._send_text("多轮规划完成；补充提醒未被当作用户问题复述。")
                return
            if "自动多步" in all_text:
                if tool_names == ["read_file"]:
                    self._send_tool_call(
                        "write-summary",
                        "write_file",
                        {
                            "path": SUMMARY_PATH,
                            "content": "# 摘要\n\nTUICodingAgent E2E 工作区。",
                        },
                    )
                elif tool_names == ["read_file", "write_file"]:
                    self._send_tool_call("verify-summary", "read_file", {"path": SUMMARY_PATH})
                else:
                    self._send_text("多步任务已完成：已读取、写入并验证 SUMMARY.md。")
                return
            if "根据上文已经制定的计划" in all_text:
                if tool_names == ["write_file"]:
                    self._send_tool_call("verify-plan", "read_file", {"path": PLAN_RESULT_PATH})
                else:
                    self._send_text("计划已执行并验证完成。")
                return
            if "制定计划" in all_text:
                self._send_text("计划：读取现状；待 /do 后写入 PLAN_RESULT.md 并验证。")
                return
            if result.get("success"):
                reply = "已读取 README.md。总结：这是 TUICodingAgent 的本地端到端测试工作区。"
            else:
                reply = "读取失败：文件不存在。请检查路径后重试；当前会话仍可继续。"
            self._send_text(reply)
            return
        if "自动多步" in latest:
            self._send_tool_call("read-source", "read_file", {"path": "README.md"})
            return
        if "MCP只读验收" in latest:
            self._send_tool_call("mcp-echo", "mcp__local__echo", {"text": "真实端到端"})
            return
        if "上下文落盘验收" in latest:
            self._send_tool_call("large-result", "mcp__local__large_result", {})
            return
        if "MCP权限拒绝验收" in latest:
            self._send_tool_call("mcp-mutate", "mcp__local__mutate", {"value": "不应执行"})
            return
        if "权限允许验收" in latest:
            self._send_tool_call(
                "permission-write",
                "write_file",
                {"path": "APPROVAL_TARGET.md", "content": "权限允许验收已完成。"},
            )
            return
        if "权限拒绝后调整" in latest:
            self._send_tool_call(
                "permission-deny",
                "write_file",
                {"path": "APPROVAL_TARGET.md", "content": "不应写入"},
            )
            return
        if "危险黑名单验收" in latest:
            self._send_tool_call("dangerous", "bash", {"command": "rm -rf /"})
            return
        if "环境感知验收" in latest:
            environment = str(messages[1].get("content", "")) if len(messages) > 1 else ""
            labels = ("工作目录", "平台", "当前日期", "Git 状态", "应用版本", "当前模型")
            if all(label in environment for label in labels):
                self._send_text("已感知完整运行环境；补充提醒仅作为约束，不会直接复述。")
            else:
                self._send_text("环境信息不完整。")
            return
        if "指令记忆验收" in latest:
            if "E2E 项目指令已加载" in system_text and "验收代号" in system_text:
                self._send_text("项目指令和长期记忆索引均已注入。")
            else:
                self._send_text("项目指令或长期记忆索引缺失。")
            return
        if "多轮规划提示" in latest:
            self._send_tool_call("cadence-1", "read_file", {"path": "README.md"})
            return
        if "制定计划" in latest:
            self._send_tool_call("plan-read", "read_file", {"path": "README.md"})
            return
        if "根据上文已经制定的计划" in latest:
            self._send_tool_call(
                "do-write",
                "write_file",
                {"path": PLAN_RESULT_PATH, "content": "计划已执行。"},
            )
            return
        if "长回复取消" in latest:
            self._send_text("这是用于取消测试的长流式响应。" * 40, delay=0.2)
            return
        if "读取 README.md" in latest:
            self._send_tool_call("read-README", "read_file", {"path": "README.md"})
            return
        if "不存在文件" in latest:
            self._send_tool_call("read-missing", "read_file", {"path": "missing.txt"})
            return
        if "代号" in latest and "记住" in latest:
            reply = "好的，我记住了：你的临时代号是青鸟-42。"
        elif "临时代号" in latest and "青鸟-42" in all_text:
            reply = "你刚才告诉我的临时代号是 **青鸟-42**。"
        else:
            reply = (
                "结果如下：\n\n- 流式响应正常\n- Markdown 正常\n\n"
                "```python\nprint('TUICodingAgent')\n```"
            )

        self._send_text(reply)

    def _start_stream(self) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream; charset=utf-8")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()

    def _send_text(self, reply: str, delay: float = 0.12) -> None:
        self._start_stream()
        for chunk in _chunks(reply, 6):
            event = {"choices": [{"delta": {"content": chunk}}]}
            try:
                self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
                return
            time.sleep(delay)
        usage = {
            "choices": [],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": len(reply) // 3 + 1,
                "cache_creation_input_tokens": 16,
                "prompt_tokens_details": {"cached_tokens": 8},
            },
        }
        self.wfile.write(f"data: {json.dumps(usage)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True

    def _send_tool_call(self, identifier: str, name: str, arguments: dict[str, str]) -> None:
        self._start_stream()
        raw = json.dumps(arguments, ensure_ascii=False)
        midpoint = len(raw) // 2
        fragments = (
            {
                "index": 0,
                "id": identifier[:5],
                "function": {"name": name[:4], "arguments": raw[:midpoint]},
            },
            {
                "index": 0,
                "id": identifier[5:],
                "function": {"name": name[4:], "arguments": raw[midpoint:]},
            },
        )
        for fragment in fragments:
            event = {"choices": [{"delta": {"tool_calls": [fragment]}}]}
            self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
            time.sleep(0.12)
        usage = {
            "choices": [],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "cache_creation_input_tokens": 16,
                "prompt_tokens_details": {"cached_tokens": 8},
            },
        }
        self.wfile.write(f"data: {json.dumps(usage)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True

    def log_message(self, format: str, *args: object) -> None:
        print(f"E2E {self.command} {self.path} {args[1]}", flush=True)


def _chunks(text: str, size: int) -> list[str]:
    return [text[index : index + size] for index in range(0, len(text), size)]


def _current_turn(messages: list[dict[str, object]]) -> list[dict[str, object]]:
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            return messages[index:]
    return messages


if __name__ == "__main__":
    print("E2E SSE server listening on 127.0.0.1:8765", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
