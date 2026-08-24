# TUICodingAgent 第 13 章 Checklist

- [x] C1：Agent 工具 schema 稳定，未知角色结构化报错。
- [x] C2：项目、用户、内置、插件占位的加载优先级正确。
- [x] C3：非法角色文件隔离，非法 model/mode 回退并警告。
- [x] C4：定义式空白上下文与 Fork 父历史前缀正确。
- [x] C5：Fork placeholder 与 boilerplate 正确。
- [x] C6：子 Agent 消息、权限、读缓存、Token 独立。
- [x] C7：全局、后台、角色黑白名单过滤正确并阻止嵌套。
- [x] C8：dontAsk 与带来源的主 TUI 审批正确。
- [x] C9：显式后台、超时后台、ESC 手动后台与关闭开关正确。
- [x] C10：任务完成/失败通知只注入下一轮模型 reminder。
- [x] C11：TaskList、TaskGet、TaskStop、SendMessage 正确。
- [x] C12：Skill fork 复用 SubAgent 公共底座。

## 端到端

- [x] E1：定义式 Explore 执行真实只读任务并回流结果。
- [x] E2：Fork 异步启动并在后续轮次收到通知。
- [x] E3：TaskList/TaskGet 可查询任务状态和结果。
- [x] E4：自定义角色覆盖和工具约束生效。
- [x] E5：子 Agent 异常不影响主对话。
