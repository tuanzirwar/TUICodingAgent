# TUICodingAgent 第 11 章 Checklist

- [x] C1：单文件和目录型 Skill 均可解析，坏文件只告警跳过。
- [x] C2：项目 > 用户 > 内置的同名覆盖和来源标识正确。
- [x] C3：启动仅注入 name/description，完整 SOP 按需激活并热重读。
- [x] C4：多个激活 Skill 每轮固定注入，clear 会清空。
- [x] C5：`$ARGUMENTS` 替换、inline 主历史执行正确。
- [x] C6：fork 的 full/recent/none 上下文与主历史隔离正确。
- [x] C7：工具白名单校验、交集收窄和系统 Skill 工具豁免正确。
- [x] C8：动态 Skill 命令优先覆盖内置命令。
- [x] C9：`/skill list|info|reload` 与补全帮助正确。
- [x] C10：URL 安装限制、暂存校验、原子落盘和热注册正确。
- [x] C11：内置 commit/review/test 可用。

## 端到端

- [x] E1：启动目录只暴露 Skill 摘要与 load_skill 指引。
- [x] E2：`/test` inline 执行并在模型请求中看到激活 SOP。
- [x] E3：`/review` fork 独立执行并把结果回流界面。
- [x] E4：修改项目 Skill 后再次执行即时采用新正文。
- [x] E5：`/clear` 后激活 Skill 不再注入。
