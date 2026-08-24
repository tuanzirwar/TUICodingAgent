# TUICodingAgent 第 13 章端到端验收报告

## 环境

- 日期：2026-08-19
- 平台：Windows PowerShell
- 终端：真实 ConPTY 会话；本机无 tmux/WSL，使用 Windows 等价伪终端
- 模型端：本地确定性 OpenAI SSE 服务

## 真实对话结果

1. “定义式探索验收”：主 Agent 调用 `Agent(subagent_type=explore)`，子 Agent 返回 `EXPLORE_CHILD_OK`，主 Agent 回流 `DEFINED_PARENT_OK`。
2. “Fork后台验收”：工具立即返回 `task-0002 / async_launched`，主对话继续并返回 `FORK_LAUNCHED_OK`；后台结果为 `Scope: FORK_CHILD_OK`。
3. “通知验收”：下一轮模型上下文检测到 `<task-notification>` 和 Fork 结果，返回 `NOTIFICATION_OK`。
4. “任务查询验收”：模型依次调用 `TaskList` 与 `TaskGet`，返回 `TASK_QUERY_OK`。
5. “自定义角色验收”：项目角色 `custom-e2e` 仅看到 `read_file`，完成真实读取并由主 Agent 返回 `CUSTOM_PARENT_OK`。
6. “失败启动/通知验收”：后台 general-purpose 子 Agent 收到 HTTP 500，任务转为 failed；主进程保持可交互，下一轮收到失败通知并返回 `FAILURE_ISOLATED_OK`。

## 额外发现

首次验收暴露主 Agent 工具回流换轮时的 TUI 计时器挂载竞态，表现为新 `ChatEntry` 尚未挂载时查询 `.message-body`。已增加精确 `NoMatches` 容错并从新会话重跑全部链路，未再崩溃。

结论：第 13 章真实终端验收全部通过。
