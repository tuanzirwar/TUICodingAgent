# TUICodingAgent 第 7 章端到端验收

## 环境

- Windows PowerShell / ConPTY。当前系统没有 tmux 或可用 WSL，使用真实伪终端完成等价交互验收。
- 本地 OpenAI 兼容 SSE 服务负责确定性地产生工具调用；官方 `mcp` SDK 拉起真实 stdio MCP Server。
- MCP Server 提供只读 `echo` 与有副作用 `mutate` 两个工具，并通过 `env` 接收测试标记。

## 真实对话

1. 输入“`MCP只读验收`”。模型请求 `mcp__local__echo`，无需审批直接执行；TUI 显示 `echo:真实端到端;marker:chapter-07-e2e;pid:41468`，随后模型回复“MCP 只读工具调用成功。”
2. 输入“`MCP权限拒绝验收`”。模型请求 `mcp__local__mutate`，TUI 显示工具名、参数、default 模式 Ask 理由与三项选择。
3. 选择“拒绝本次”。拒绝结果被回灌；模型随即改用 `mcp__local__echo`，最终回复“MCP 写操作被拒绝后，已改用只读 MCP 工具完成。”
4. 输入 `/exit`。退出前 MCP 子进程 PID 41468 存在，退出后再次查询已不存在；仅本地 SSE 测试服务仍在，随后也已清理。

## 自动化

- 官方 SDK 真实 stdio：初始化、列工具、调用、env 注入、关闭所有者任务。
- 官方 SDK 真实 Streamable HTTP：初始化、列工具、调用、客户端关闭；另验证 headers 注入。
- 覆盖两层完整覆盖、非法配置隔离、凭据不回显、缺失变量、命名校验、重复工具、Server 失败与启动超时隔离、启动取消清理、远端错误、调用超时、非文本过滤、动态权限和只读并发批次。
- Provider 与 `permissions/` 在第 7 章均未修改，MCP 权限由 `mcp_client/permission.py` 组合适配。
