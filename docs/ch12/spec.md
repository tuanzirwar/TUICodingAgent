# TUICodingAgent 第 12 章 Spec（已批准）

实现声明式 Hook 生命周期系统。规则由 event、可选 if、action 组成；支持 11 个事件、exact/not/regex/glob 共享匹配器、shell/prompt/http/subagent 四类动作、only_once/async/timeout 控制，以及 PreToolUse 与 UserPromptSubmit 同步拦截。

配置使用项目 `.tuicodingagent/hooks.yaml` 与用户 `~/.tuicodingagent/hooks.yaml`，两层按声明顺序叠加。错误逐条输出 stderr 并跳过，不阻断应用；subagent 本章仅保留固定日志占位。
