# TUICodingAgent 第 6 章端到端验收

## 环境

- Windows PowerShell / ConPTY；当前系统未安装 tmux 或可用 WSL，因此使用等价的真实伪终端交互。
- 本地确定性 SSE 服务模拟 OpenAI 流式接口，不使用真实 API 密钥。
- 真实启动 `python -m tuicodingagent`，所有请求均从 TUI 输入框发送。

## 结果

- E1：default 模式下 `write_file` 显示工具、参数、理由与三项选择；选择“允许本次”后 Runtime 确实调用写工具。当前执行沙箱的 Windows DACL 拒绝子进程落盘并返回 Errno 13；临时目录自动化测试已验证允许后的真实写入成功。
- E2：选择“拒绝本次”后，界面显示“权限拒绝：用户拒绝了本次工具调用”；模型收到该结果后调用 `read_file(README.md)`，最终回复“已根据权限拒绝改用只读方案并完成”。
- E3：Shift+Tab 在四档模式间循环，状态栏即时显示 `default`、`acceptEdits`、`plan`、`bypassPermissions`；自动化 TUI 测试同时验证 `/plan` 与 `/do`。
- E4：`bypassPermissions` 下模型请求 `bash(rm -rf /)`，界面显示黑名单拒绝，命令未执行；模型随后正常给出说明。

## 自动化覆盖

- 权限规则优先级、glob、友好工具名、四档矩阵、沙箱、危险命令变体、损坏配置降级与永久规则重载。
- 拒绝结果回灌、混合工具保序、只读并发、审批等待取消及取消后的下一轮对话。
- OpenAI 与 Anthropic Provider 均继续只负责协议适配，权限逻辑位于 Runtime 之前。
