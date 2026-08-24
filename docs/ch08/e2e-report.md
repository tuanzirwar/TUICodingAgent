# TUICodingAgent 第 8 章端到端报告

日期：2026-08-19

## 环境

- Windows PowerShell；当前主机未安装 tmux/WSL。
- 使用带真实 Windows ConPTY 的交互终端启动 `python -m tuicodingagent`，等价覆盖键盘输入、全屏 TUI、异步请求与退出清理链路。
- 使用本地确定性 OpenAI 兼容 SSE 服务，避免依赖外部 API。

## 对话验收

1. 输入 `你好`：服务端收到一次请求，TUI 流式显示 Markdown 回复。
2. 输入 `/compact`：TUI 显示“正在压缩上下文...”，服务端收到独立摘要请求，随后显示压缩前后 Token。
3. 输入 `/unknown`：TUI 显示可用命令列表；服务端请求计数保持不变。
4. 输入 `压缩后继续`：服务端收到新请求，TUI 正常完成下一轮回复。
5. 输入 `/exit`：主进程正常退出，MCP stdio 子进程随连接关闭。

短对话的结构化九段摘要可能比原历史更长，因此本次显示 `57 -> 652`；这不影响手动压缩链路。长历史压缩与近期原文边界由自动化用例覆盖。

## 落盘验收

自动化用例在可写临时工作区真实创建 `.tuicodingagent/sessions/<unix-ts>-<6hex>/tool-results/`，验证 60,000 字节单结果、聚合阈值、完整原文重读、冻结、幂等、失败重试和并发一致性。

当前受控 ConPTY 子进程对仓库内新建 `.tuicodingagent` 目录被 Windows 沙箱 DACL 拒绝。实测应用会记录警告、保留原始工具结果并继续运行，符合安全降级要求；物理落盘能力由上述真实文件系统测试完成验收。

## 自动化结果

- `python -m pytest -q`：105 passed，1 skipped。
- `python -m ruff check .`：通过。
- `python -m compileall -q src tests`：通过。
- 唯一跳过项为 Windows 环境无法创建测试符号链接，与本章功能无关。
