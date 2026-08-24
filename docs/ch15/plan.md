# 第 15 章实施计划

1. 建立 Team、Teammate、后端与名称注册模型。
2. 实现原子 config/tasks/mailbox 持久化及跨任务文件锁。
3. 把 TeamService 接入 SubAgent、Worktree、权限与后台任务管理器。
4. 注册 Team 工具及队员上下文过滤，完成消息续派、idle 通知和 Plan 切换。
5. 实现 `/team` 操作和 Coordinator 双开关、白名单、提示词、状态栏。
6. 添加单元/集成测试，执行完整回归和真实 ConPTY in-process 对话。
