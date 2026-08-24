# TUICodingAgent 第 4 章实现计划

1. 扩充 Provider/Runtime 事件模型，加入 Token 用量、迭代、取消和模式。
2. 为工具注册中心增加只读分类与保序分批计划。
3. 将 ConversationRuntime 改为多轮 ReAct 循环，处理全部停止条件和合法历史。
4. 升级 OpenAI/Anthropic 用量解析。
5. 接入 TUI 迭代、累计用量、取消快捷键与 `/plan`、`/do`。
6. 添加运行时、并发、协议、取消和 TUI 测试。
7. 执行 ruff、pytest、compileall 与 PTY 真实对话验收。

## 自动审批记录

设计与实现计划已按用户授权自动批准。
