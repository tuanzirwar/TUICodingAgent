# TUICodingAgent 第 2 章端到端验收报告

## 环境

- 日期：2026-08-19
- Python：3.14.2
- TUI：Textual 7.5.0
- 终端：Codex Windows PTY，80×24
- Provider：`tests/e2e_sse_server.py` 本地 OpenAI 兼容 SSE 服务
- 配置：`tests/e2e_workspace/config.yaml`（测试密钥，不含真实凭据）
- tmux：2026-08-24 使用临时便携 MSYS2 tmux 3.7b；未安装到系统

## 执行步骤与实际结果

1. 启动本地 SSE 服务，控制台显示监听 `127.0.0.1:8765`。
2. 在独立 PTY、`tests/e2e_workspace` 工作目录运行 `python -m tuicodingagent`。
3. 启动页实际显示猫咪、TUICodingAgent、版本 `0.2.0`、绝对工作目录、就绪行、带 `❯` 的输入框、占位说明及 `本地 E2E OpenAI / deterministic-test-model` 状态栏。
4. 输入“请用 Markdown 给出一个列表和 Python 代码块”。首增量前显示 `Imagining… (0s)`；服务端分 6 字符发送 SSE，界面逐段更新纯文本；结束后列表变为项目符号，Python 代码块定型，并显示 `完成 · 1.5s`。
5. 输入“请记住：我的临时代号是青鸟-42”，收到确认。
6. 输入“我的临时代号是什么？”，服务端从本轮请求携带的完整消息历史中识别前文，回复“青鸟-42”，证明多轮上下文已发送。
7. 输入 `/exit`，应用退出并恢复备用屏幕和光标。
8. 再次启动应用并发送 Ctrl+C，应用同样退出并恢复终端。
9. 服务端日志共记录 4 个成功 POST（其中第 4 个来自第一次快速 PTY 注入时未完整送达的 `/exi`，随后以分离按键方式正确验证 `/exit`）。

## 自动化证据

- `python -m ruff check src tests`：通过。
- `python -m pytest -q`：25 passed。
- `python -m compileall -q src tests`：通过。

## 未通过/未验证（截至 2026-08-19）

- 未使用真实 OpenAI 或 Anthropic 模型，无法证明真实服务兼容性和模型语义。
- 该历史轮次未在 tmux 中执行；2026-08-24 已补齐真实 tmux pane 验收，见下节。
- 2026-08-19 补测错误恢复：真实 ConPTY 先以错误密钥请求，界面显示 `认证失败（invalid key）` 并恢复输入；改正密钥重启后成功流式渲染列表和代码块，服务端日志依次为 401、200。

## 2026-08-22 真实兼容端点探测

- 已按用户提供配置连接 `new_api` 的 Chat Completions 端点；网络、TLS、URL 与请求格式均成功到达服务端。
- 两枚候选密钥均得到服务端“余额不足”响应，未产生模型文本，故不能作为 C4/E1 的通过证据。
- 第二个域名的密钥归属尚未确认，未跨域发送密钥。

## 2026-08-24 真实 CloudVisit Responses 验收

- CloudVisit 官方配置声明 `wire_api=responses`；TUICodingAgent 增加对应 YAML `wire_api: responses` 支持，服务根地址会安全补全为 `/v1/responses`。
- 真实 Windows PTY 启动完整 TUI，凭据通过隐藏输入仅进入当前进程，没有写入 `config.yaml`、会话报告或日志。
- 一次真实用户对话触发两轮模型调用：第 1 轮流式返回 `read_file(README.md)`，第 2 轮在工具结果回灌后流式输出项目名称与安装命令。
- TUI 正确显示工具行、带行号结果、迭代 1/2、Markdown 定型与累计 Token（最终 `↑7487 ↓67`），空闲 Ctrl+C 退出码为 0。
- Responses 使用 `store: false`，本地保留并重放加密 reasoning item；测试结束后两个本轮 `.tuicodingagent/sessions` 目录均已删除。
- 真实 OpenAI Responses 已通过；真实 Anthropic 仍待具备对应凭据后验收。

## 2026-08-24 真实 tmux 补测

- 从 MSYS2 官方仓库下载并校验临时便携 tmux 3.7b/libevent，仅解压到系统临时目录。
- 在真实 tmux pane 中启动完整 TUICodingAgent，输入“请读取 README.md 后总结项目”。
- pane 输出显示 `Read(README.md)`、工具内容、最终总结、`完成 · 1.4s` 和 `Token ↑20 ↓21`。
- Ctrl+C 后应用、pane 与专用 tmux server 一并退出；socket 和服务进程已清理。便携运行时文件因安全审查要求显式删除确认而暂留系统临时目录。
