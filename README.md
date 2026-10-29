# Work Agent for E10

包装计划、包装工单、领料申请及明确指定的生产退料助手。项目规则和技能通过私有 Git 仓库同步，每台电脑独立运行 Codex 和 MCP。

**私有仓库：[alyssalowo/work-agent-e10](https://github.com/alyssalowo/work-agent-e10)，主分支为 `main`。**

## 从哪里开始

- [项目规则](AGENTS.md)：Codex 在这个项目中工作的约束。
- [项目说明](reference/项目说明.md)：业务范围、授权及异常处理规则。
- [本机配置](docs/本机配置.md)：每台电脑各自配置 MCP、凭据和运行环境。
- [跨电脑使用](docs/跨电脑使用.md)：项目地址、另一台电脑接入和日常同步。

## 已收齐的技能

| 技能 | 用途 |
| --- | --- |
| [packing-order](.agents/skills/packing-order/SKILL.md) | 包装计划至工单、领料的完整流程 |
| [e10-shipment-sheet-fill](.agents/skills/e10-shipment-sheet-fill/SKILL.md) | 销货、借出、出库申请明细填写包装计划 |
| [packaging-workorder-match](.agents/skills/packaging-workorder-match/SKILL.md) | 包装工单匹配回填 |
| [packaging-picking-match](.agents/skills/packaging-picking-match/SKILL.md) | 领料号按工单、仓库分类匹配回填 |

技能正文及其脚本一起同步，技能间使用项目内相对路径。正常操作和异常恢复按需读取各自说明。

## 离线验证

在仓库根目录运行：

```text
python -m unittest discover -s tests -p "test_*.py" -v
node tests/test_shipment_submit.cjs
```

测试只使用合成数据和本地模拟响应，覆盖三来源分表、字段映射、全表去重、接续检查及填写执行器，不连接 E10 或钉钉。通过测试不代表实际业务已完成；正式操作仍须满足原控制器和真实回执门槛。

## 同步边界

Git 保存规则、技能、脚本及使用文档。本机 MCP 配置、集成程序、登录信息、业务快照、执行回执和检查点不纳入版本管理，相关路径已加入 `.gitignore`。

Git 同步需要主动拉取和推送，不会自动同步聊天、个人记忆、定时任务或当前运行中的 MCP 会话。换电脑继续未完成的业务前，先按使用文档交接执行记录；同一批业务只由一台电脑执行。

本版本从原 ChatGPT 项目镜像复制整理，原项目文件保留。正式使用时将整个目录放在普通工作目录中，避免依赖会重新同步的 ChatGPT 项目镜像目录。
