# TUICodingAgent 第 14 章 Checklist

- [x] C1：slug、扁平目录与分支命名安全。
- [x] C2：创建、快速恢复、进入、退出、删除正确。
- [x] C3：配置、hooks、软链和 worktreeinclude 初始化正确。
- [x] C4：变更与新增/未推送 commit 保护 fail-closed。
- [x] C5：session 原子持久化、失效恢复与 stale sweep 正确。
- [x] C6：六个核心工具显式 cwd 且 schema 不变。
- [x] C7：角色 isolation 解析、通知、创建和自动清理正确。
- [x] C8：`/worktree` create/list/enter/exit/remove 正确。

## 端到端

- [x] E1：真实 Git Worktree 中 SubAgent 写文件不影响主目录。
- [x] E2：有变更的临时 Worktree 被保留并回流路径/分支。
- [x] E3：手动 Worktree 的变更保护和 discard 删除正确。
