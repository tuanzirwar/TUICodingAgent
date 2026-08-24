# TUICodingAgent 第 7 章 Spec

本章实现配置驱动的 MCP 工具客户端。TUICodingAgent 启动 TUI 前并发连接用户级 `~/.tuicodingagent/config.yaml` 与项目级 `.tuicodingagent.yaml` 声明的 stdio / Streamable HTTP Server，使用官方 `mcp` SDK 完成初始化、工具发现、调用与关闭。

成功发现的工具以 `mcp__<server>__<tool>` 注册到现有工具中心，透传描述与 JSON Schema；`readOnlyHint: true` 作为只读工具，其余安全默认视为有副作用。文本结果顺序拼接，远端错误、协议异常和 30 秒超时均转成可回灌的 `ToolResult`。单个 Server 的启动失败或 30 秒超时不影响其他 Server，所有已连接会话在退出时以 5 秒总超时关闭。

MCP 工具复用现有规则语义、权限模式与人在回路。为满足权限包零改动，本章在 MCP 包中提供组合权限适配器：内置工具仍完全委托原权限管理器，MCP 工具按动态只读元数据和原始配置中的名称规则裁决。

环境变量只在 `env` 与 `headers` 的值中展开；缺失变量变为空串并告警。凭据不记录、不回显。资源、提示词、采样、roots、自动重连、健康检查、热加载和非文本结果回灌均不在本章范围。

审批状态：依据用户“涉及到需要审批的位置自动审批”的指示，本 Spec 自动批准。
