# TUICodingAgent 第 10 章 Checklist

- [x] C1：名称/别名冲突在注册期报出具体键。
- [x] C2：大小写、空白、未知命令、零参数约束正确。
- [x] C3：12 条命令元数据完整且帮助来自单一注册中心。
- [x] C4：Local/UI/Prompt 三类语义及非 idle 互斥正确。
- [x] C5：help/status/memory/permission/session 输出正确且不请求模型。
- [x] C6：clear 开新会话、清历史/用量/轮数并保留旧会话。
- [x] C7：do/review 使用普通用户消息路径持久化并请求模型。
- [x] C8：补全前缀、隐藏、零匹配、多行和关闭条件正确。
- [x] C9：上下/Tab/Enter/Esc 键盘行为正确。
- [x] C10：既有五命令行为、退出清理与状态栏无回归。

## 端到端

- [x] E1：依次执行 help/status/memory/permission/session 均即时响应。
- [x] E2：review 发起真实模型回合并写入 JSONL。
- [x] E3：clear 后旧会话可由 resume 看到并恢复。
- [x] E4：输入 `/`、前缀过滤、Esc 与 Enter 菜单交互正确。
