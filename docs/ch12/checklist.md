# TUICodingAgent 第 12 章 Checklist

- [x] C1：exact/glob/regex/not 共享匹配语义及权限兼容正确。
- [x] C2：两层 hooks.yaml 叠加、冲突与逐条错误降级正确。
- [x] C3：11 个事件 payload 和触发位置正确。
- [x] C4：all_of/any_of、嵌套字段与非法混用校验正确。
- [x] C5：shell、prompt、HTTP、subagent 占位动作正确。
- [x] C6：PreToolUse 拦截回灌失败工具结果且跳过权限和执行。
- [x] C7：UserPromptSubmit 拦截不写历史并回显原因。
- [x] C8：only_once、async、timeout 和会话重置正确。
- [x] C9：Hook 失败只写 stderr，取消可传播。
- [x] C10：`/hooks` 分组列表和来源输出正确。

## 端到端

- [x] E1：SessionStart prompt 只注入首轮。
- [x] E2：PreToolUse 能拦截 write_file 并让模型调整。
- [x] E3：PostToolUse 异步动作不阻塞主流程。
- [x] E4：UserPromptSubmit 关键词拦截不进入历史。
- [x] E5：only_once 在 clear 后重新触发。
