# TUICodingAgent ch01–ch15 完成度审计

审计日期：2026-08-24。

## 审计方法

- 确认 `TODO/chN - start.md` 与 `TODO/chN.md` 依次存在，并确认每章均有
  `spec.md`、`plan.md`、`task.md`、`checklist.md` 和 `e2e-report.md`。
- 以各章原始 F/N/AC 条目为范围，以源码、自动化测试、真实 Git/本地服务和终端输出为证据；
  不用 MockTransport 代替真实云服务，不用 ConPTY 代替明确要求的 tmux/iTerm2 pane。
- 最终本机回归为 `ruff check src tests`、`python -m compileall -q src tests` 通过，
  `pytest -q` 为 196 passed、1 skipped。

## 逐章结论

| 章节 | 本机可验证范围 | 主要证据 | 结论 |
| --- | --- | --- | --- |
| ch01 | 流程、文档、命名、测试治理 | `docs/ch01/`、全库命名扫描、全量回归 | 本机项完成；真实 Anthropic 待外部环境 |
| ch02 | YAML、Chat Completions/Responses/Anthropic、SSE、多轮和基础 TUI | `test_config.py`、`test_providers.py`、`test_openai_responses.py`、真实 CloudVisit ConPTY、真实 tmux pane | OpenAI Responses 与 tmux E1 完成；真实 Anthropic 待验收 |
| ch03 | 六个工具、工具协议、回灌、错误恢复 | `test_tools.py`、`test_tool_providers.py`、`test_openai_responses.py`、真实 CloudVisit ConPTY | OpenAI Responses 工具链完成；真实 Anthropic 待验收 |
| ch04 | Agent Loop、并发、停止条件、取消、Plan/Do | `test_agent_loop.py`、`test_runtime.py`、真实 CloudVisit 两轮 Agent Loop | OpenAI Responses 完成；真实 Anthropic 待验收 |
| ch05 | 模块化提示、环境块、缓存布局、reminder | `test_prompts.py`、`test_prompt_providers.py`、`test_context_runtime.py` | 本机项完成；真实 Anthropic cache 待验收 |
| ch06 | 黑名单、沙箱、规则、四模式、人在回路 | `test_permissions.py`、`test_permission_tui.py` | 完成 |
| ch07 | stdio/HTTP MCP、命名空间、权限和关闭 | `test_mcp_*.py`、本地真实 MCP Server E2E | 完成 |
| ch08 | 结果落盘、压缩、PTL、熔断与恢复 | `test_context_offload.py`、`test_context_compact.py`、`test_context_tui.py` | 完成 |
| ch09 | 指令、JSONL session、恢复和长期记忆 | `test_instructions.py`、`test_session.py`、`test_memory.py` | 完成 |
| ch10 | 命令注册、分发、帮助与补全 UI | `test_commands.py`、`test_command_tui.py` | 完成 |
| ch11 | Skill 解析、覆盖、执行、隔离和安装 | `test_skills.py`、本地确定性 E2E | 完成 |
| ch12 | 11 个 Hook、匹配、动作和失败隔离 | `test_hooks.py`、本地确定性 E2E | 完成 |
| ch13 | SubAgent、Fork、后台、任务通知与续派 | `test_subagent.py`、本地确定性 E2E | 完成 |
| ch14 | Worktree 创建、保护、恢复、隔离与清理 | `test_worktree.py`、真实临时 Git 仓库 E2E | 完成 |
| ch15 | Team、任务、邮箱、续派、Plan、Coordinator | `test_team.py`、真实 Git Worktree/ConPTY E2E、真实 tmux pane | in-process、tmux 新 session 与同会话 split-window 完成；iTerm2 待验收 |

## 当前唯一未闭环项

1. ch02 C4、ch03 E3、ch04 E4：OpenAI Responses 已完成，需要可用的真实 Anthropic 凭据补齐对照。
2. ch05 E4：需要真实 Anthropic 服务返回 prompt-cache 创建和读取用量。
3. ch15 iTerm2 后端实机复验：需要 macOS + iTerm2 + `it2`。

当前主机检测结果：根目录无 `config.yaml`，凭据未落盘且进程退出后已清除；无 Anthropic
凭据、无系统级 tmux、无可运行 WSL 发行版、无 iTerm2。临时便携 tmux 已完成可执行项；服务与 socket 已清理，便携文件待用户显式确认删除。其余项目保持未通过状态。
