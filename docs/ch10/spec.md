# TUICodingAgent 第 10 章 Spec

按 `TODO/ch10.md` 实现统一斜杠命令注册、分发、UI 抽象和补全菜单。内置 12 条命令：`/clear`、`/compact`、`/do`、`/exit`、`/help`、`/memory`、`/permission`、`/plan`、`/resume`、`/review`、`/session`、`/status`。

注册期检查名称/别名冲突；命令大小写不敏感且本期拒绝参数；本地命令任何状态可查询，UI/Prompt 命令仅空闲执行；隐藏命令可分发但不出现在帮助和补全中。
