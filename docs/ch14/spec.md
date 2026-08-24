# TUICodingAgent 第 14 章 Spec（已自动批准）

使用 Git Worktree 为主 Agent 与 SubAgent 提供文件系统隔离，路径统一为 `.tuicodingagent/worktrees/`，会话文件为 `.tuicodingagent/worktree_session.json`。实现严格 slug、完整生命周期、环境初始化、变更保护、过期清理、explicit cwd 工具上下文、`isolation: worktree` 角色和 `/worktree` 命令。

不实现 Worktree 合并策略、跨目录同步、多 Agent 团队编排和跨进程共享。
