# TUICodingAgent 第 3 章端到端验收报告

## 环境

- 日期：2026-08-19
- Python：3.14.2
- 终端：Codex Windows PTY，80×24
- Provider：本地 OpenAI 兼容 SSE 服务
- tmux：主机未安装，WSL 也未安装，因此使用真实 PTY 等价验证终端交互
- 真实 API：项目根目录无 `config.yaml`，环境变量无 OpenAI/Anthropic API 密钥

## 成功工具链路

1. 启动 `tests/e2e_sse_server.py`，再从 `tests/e2e_workspace` 启动 `python -m tuicodingagent`。
2. 输入“读取 README.md 后总结”。首个请求携带六个工具定义；服务以两个 SSE 碎片返回 `read_file` 调用。
3. 界面显示 `● Read(README.md)` 和“执行中…”，工具执行后更新为带行号的文件摘要。
4. 第二个请求携带 assistant `tool_calls` 与 role=tool 的结构化结果；服务返回最终总结。
5. 界面最终显示“已读取 README.md。总结：这是 TUICodingAgent 的本地端到端测试工作区。”及完成耗时 1.3 秒。

## 失败回灌与恢复链路

1. 在同一会话输入“请读取不存在文件并说明结果”。
2. 界面显示失败样式的 `● Read(missing.txt)`，摘要包含文件不存在错误。
3. 结构化失败结果被回灌；第二阶段回复“读取失败：文件不存在。请检查路径后重试；当前会话仍可继续。”
4. 随后输入“继续测试”，应用正常完成普通流式 Markdown 回复，证明失败未锁死会话。
5. 输入 `/exit` 后退出码为 0，备用屏幕与光标恢复。

## 自动化证据

- `python -m ruff check src tests`：通过。
- `python -m pytest -q`：38 passed。
- `python -m compileall -q src tests`：通过。

## 尚待真实凭据验证

- OpenAI 与 Anthropic 的请求体、SSE 碎片和工具结果格式均由 MockTransport 自动化测试验证。
- 因没有真实 API 配置，尚未完成两个真实服务的同任务对照；这不会阻断后续章节的本地开发。

## 2026-08-22 真实 ConPTY 复验

- 重新启动本地 SSE 服务和完整 TUICodingAgent TUI，输入真实请求“请读取 README.md 后总结”。
- 界面显示 `Agent 迭代 1`、`● Read(README.md)`、带行号结果，第二轮逐字流式输出总结并在结束后定型为 Markdown。
- 状态栏用量从 `Token ↑0 ↓0` 更新到 `Token ↑20 ↓21`，本轮显示 `完成 · 1.3s`。
- 空闲时发送 Ctrl+C，进程退出码为 0，备用屏幕、鼠标模式和光标状态均恢复；本次临时 session 目录已清理。

## 2026-08-24 真实 CloudVisit Responses 复验

- 先以最小请求确认 CloudVisit 的 `/v1/responses` 与 `/v1/chat/completions` 均可鉴权；随后停止探索性调用，在本地实现并用 MockTransport 验证 Responses wire 模式。
- 真实 Windows PTY 启动完整 TUICodingAgent，凭据通过隐藏输入仅传入当前进程，没有写入仓库、配置或报告。
- 输入“请调用 `read_file` 读取 `README.md`，然后依据工具结果说明项目名称及安装命令”。
- 第 1 轮真实模型返回 FC，界面显示 `● Read(README.md)` 和带行号结果；状态栏更新为 `Token ↑1396 ↓19`。
- 第 2 轮请求重放加密 reasoning item、function call 与 `function_call_output`，模型流式回答项目名为 TUICodingAgent 并给出安装命令；累计用量更新为 `Token ↑7487 ↓67`。
- 空闲时 Ctrl+C 退出码为 0，终端备用屏幕和光标恢复；本次真实对话完成后不再调用该凭据。
- 最终回归：`ruff format --check src tests`、`ruff check src tests`、`compileall` 通过，`pytest -q` 为 196 passed、1 skipped。
