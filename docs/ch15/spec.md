# 第 15 章 Spec：Agent Teams

## 目标

在既有 SubAgent 与 Git Worktree 之上提供长期 Team：Lead 创建团队、并行派出具名队员、通过共享任务图和持久化邮箱协作，并能恢复、续派、收敛和清理资源。

## 范围

- Team/Teammate 持久化、名称规范化、恢复与强制删除保护。
- tmux、iTerm2、in-process 后端抽象与自动探测。
- `TeamCreate`、`TeamDelete`、扩展 `Agent(team_name=...)`。
- 队员专用 TaskCreate/Get/List/Update、SendMessage，消息广播、文件锁与 stale-lock 恢复。
- 队员独立 Worktree、完成后的 idle 通知、停止后的会话续派和 Plan 审批响应。
- `/team list|info|delete|kill`。
- 配置与环境变量双开关的 Coordinator Mode、工具白名单、状态标签与四阶段提示词。

## 兼容性

项目名称、运行目录和环境变量统一使用 TUICodingAgent、`.tuicodingagent` 与 `TUICODINGAGENT_COORDINATOR_MODE`。
