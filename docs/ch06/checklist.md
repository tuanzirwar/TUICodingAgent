# TUICodingAgent 第 6 章 Checklist

- [x] C1：危险命令在 bypassPermissions 下仍硬拦截。
- [x] C2：项目外、`..` 和外链路径被拒；项目内新路径允许。
- [x] C3：精确/glob/无模式规则正确匹配。
- [x] C4：六个友好工具名正确路由。
- [x] C5：跨层冲突按 deny > ask > allow。
- [x] C6：缺失/损坏配置安全降级。
- [x] C7：四档模式矩阵正确。
- [x] C8：五层流水线正确短路与跳层。
- [x] C9：Shift+Tab、`/plan`、`/do` 与状态栏正确。
- [x] C10：人在回路三选一、永久精确规则可重载。
- [x] C11：拒绝与放行结果保序回灌，Loop 继续。
- [x] C12：等待审批可取消且之后能继续。
- [x] C13：只读并发不退化。
- [x] C14：双协议行为一致且 Provider 不承载权限逻辑。
- [x] C15：未知工具和坏参数安全默认。
- [x] C16：ch04/ch05 能力无回归。
- [x] C17：ruff、pytest、compileall 通过且密钥不泄漏。
- [x] C18：defaultMode 本地 > 项目 > 用户。
- [x] C19：权限包保持局部可扩展。

## 端到端

- [x] E1：PTY 中默认模式写文件弹出审批，允许本次后执行。
- [x] E2：PTY 中拒绝一次调用后模型调整并继续。
- [x] E3：PTY 中 Shift+Tab 循环、状态栏和 plan 工具集正确。
- [x] E4：PTY 中危险命令在 bypassPermissions 仍被拒绝。
