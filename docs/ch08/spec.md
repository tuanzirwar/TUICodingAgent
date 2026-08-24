# TUICodingAgent 第 8 章 Spec

实现两层上下文管理：工具结果按 50,000 字节单条阈值与 200,000 字节单轮阈值确定性落盘；整体估算接近 Provider 上下文窗口时，以无工具的 LLM 请求生成九段结构化摘要，并恢复近期原文、最近文件快照、当轮同一份工具定义与固定边界提示。

支持 `/compact` 手动摘要、自动摘要三次失败熔断、Provider `prompt_too_long` 紧急摘要与单次重试。Token 估算以上次 usage 为锚点，增量按 3.5 字符/token；Provider 可配置 `context_window`，默认 OpenAI 128K、Anthropic 200K。

所有会话文件写入 `.tuicodingagent/sessions/<session_id>/tool-results/<tool_use_id>`。替换账本、文件追踪和熔断计数并发安全，落盘失败安全降级为保留原文。

审批状态：依据用户自动审批指示，本 Spec 已批准。
