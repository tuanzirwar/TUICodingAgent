# TUICodingAgent 第 7 章 Checklist

- [x] C1：两层配置按 Server 名完整覆盖。
- [x] C2：非法文件与 Server 安全跳过并告警。
- [x] C3：仅 env/headers 展开变量，未定义值为空并告警。
- [x] C4：stdio 握手、列工具、调用和退出终止通过。
- [x] C5：HTTP 握手、列工具、headers 与关闭通过。
- [x] C6：命名空间、Schema、描述、只读提示正确。
- [x] C7：文本拼接、远端错误、异常与调用超时正确回灌。
- [x] C8：多 Server 并发启动、30 秒超时与失败隔离正确。
- [x] C9：退出关闭总超时 5 秒且无任务泄漏。
- [x] C10：MCP allow/ask/deny、模式兜底和只读并发正确。
- [x] C11：`permissions/` 与 Provider 源码零改动。
- [x] C12：既有功能、格式、静态检查、编译与测试无回归。
- [x] C13：示例配置无明文凭据。

## 端到端

- [x] E1：真实 TUI 启动时发现 stdio MCP 工具。
- [x] E2：真实对话触发 MCP 工具，显示调用并生成最终回复。
- [x] E3：有副作用 MCP 工具弹审批，拒绝后模型继续。
- [x] E4：退出后 MCP 子进程不残留。
