# TUICodingAgent 第 13 章 Spec（已自动批准）

实现统一 SubAgent 机制：稳定的 `Agent` 工具、定义式与 Fork 式启动、Markdown 角色目录、独立运行时、分层权限、后台任务管理和 Task 工具。项目路径统一使用 `.tuicodingagent/agents/`。

定义式从空白上下文和角色系统提示启动；Fork 式复制父历史并追加 `<fork_boilerplate>`，且强制后台。所有子 Agent 禁止递归启动 Agent，后台 Agent 额外受基础工具白名单约束。内置 general-purpose、explore、plan 三个角色，Skill fork 复用同一底座。

后台关闭时所有定义式任务同步执行，Fork 返回结构化错误。本章不实现 worktree 隔离、团队编排、任务跨会话持久化、真插件加载与 TaskCreate。
