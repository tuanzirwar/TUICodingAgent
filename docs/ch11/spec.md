# TUICodingAgent 第 11 章 Spec（已批准）

实现可复用 Skill 能力包：YAML frontmatter 描述元信息、Markdown 正文承载 SOP；项目、用户、内置三级来源按优先级覆盖。启动仅注入目录，模型通过 `load_skill` 渐进加载完整指令。

Skill 支持 inline 与 fork 两种执行模式、`$ARGUMENTS` 参数、工具白名单、热更新、动态斜杠命令、`/skill list|info|reload`、远程 GitHub URL 原子安装，以及内置 commit/review/test 样板。激活的 SOP 每轮固定注入动态环境，`/clear` 清除激活状态。

目录命名统一采用 `.tuicodingagent/skills`，不实现市场分发和版本管理。
