# TUICodingAgent 第 4 章端到端报告

## 环境

- Windows PowerShell，Python 3.14.2，Textual 7.5.0。
- 当前环境没有 tmux/WSL，因此使用 Codex 提供的 Windows PTY 启动真实全屏 TUI。
- 使用 `tests/e2e_sse_server.py` 提供确定性的 OpenAI 兼容流式服务；未使用真实 API 密钥。
- 沙箱中的 TUI 子进程不能在工作区新建文件，因此写入验收目标放在系统临时目录，读、写、再读验证链路不变。

## 验收结果

1. 自动多步任务：输入“自动多步：读取 README.md，写入摘要并验证”。Agent 连续完成 Read、Write、Read，并在第 4 轮自然结束；状态栏累计显示 `↑40 ↓25`。
2. Plan Mode：输入 `/plan` 后界面显示 PLAN，模型侧只收到 Read/Glob/Grep。规划完成后输入 `/do`，界面切回 EXEC，并立即完成 Write、Read、最终答复；状态栏累计显示 `↑50 ↓32`。
3. 取消恢复：输入“长回复取消”，流式输出期间按 Esc，当前回复提前停止且输入框恢复可用；随后输入 `ok` 得到正常回复，证明运行时与界面均可继续使用。

## 自动化证据

- Runtime 测试覆盖四轮自然完成、迭代上限、未知工具熔断、Provider 错误恢复、Token 累计、只读并发、副作用串行、Plan Mode、协作式取消以及工具执行期间的任务取消。
- Provider 测试覆盖 OpenAI 与 Anthropic 的工具碎片和 Token 用量解析。
- TUI Pilot 测试覆盖模式切换、`/do` 立即执行、状态栏和 Esc 取消。
- 最终命令与结果以本章收尾回归为准。

## 未执行项

- 未配置真实 OpenAI/Anthropic API，E4 保留为未验收；这不影响本地协议兼容服务和自动化测试通过。
