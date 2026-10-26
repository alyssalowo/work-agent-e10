---
name: packaging-picking-match
description: "将本地领料申请明细按工单匹配，通过官方钉钉MCP分类合并机器/物料领料号并处理成品提示；只做匹配回填，不生成或审核领料。"
---

# 包装领料单匹配

只处理本次明确文档、工作表和字段。先完成全量匹配及修改计划，再提交确定项；不默认导出，不在写后全表读回或刷新。独立调用只做本匹配阶段；由packing-order调用时按它的阶段门槛接续，不在本技能另行生成或审核业务。

## 匹配与分类

以网页的 **包装工单** 精确匹配 XLSX 的 **领料申请单明细_工单单号**。文件还可能有名为“工单单号”的列，它可能为空或与明细列不同，不能替代明细列。

| 源记录条件 | 目标字段 | 处理 |
| --- | --- | --- |
| 材料品号不以 1- 开头，仓库名称为“量产/研发交付裸机良品仓” | 机器领料单号 | 汇总申请单号 |
| 材料品号不以 1- 开头，仓库名称为**上述仓库之外的任意非空仓库** | 物料领料单号 | 汇总申请单号，包括原材料、电子、包材、成品等仓库 |
| 领料申请单明细_材料品号以 1- 开头 | 是否加急 | 只标记“内有成品”，该条记录不提供待回填的领料号码 |

- 源字段使用“申请单号”“领料申请单明细_仓库名称”和“领料申请单明细_材料品号”。按明细记录先排除 1- 材料，再分类号码；同一申请单的其他非 1- 材料记录仍可贡献号码，不能跳过整个工单或整张申请单。
- 同一工单可对应多个申请单号。每个目标字段内按**不同申请单号**去重，用 / 分隔；例如 001、001、002、003、003 → 001/002/003。保留编号前导零、连字符及大小写，只去首尾空白；不做模糊匹配。
- 包装工单单元格含多个工单号时，识别实际使用的换行或 / 分隔，对全部工单取申请单号的并集，再在每个目标字段内去重。相同申请单号可以合法出现在不同工单行，不套用上一套工单匹配的“不复用工单号”分配逻辑。
- 工单、申请单号或仓库名称缺失时报告缺字段，不能猜测。没有候选的字段保持原值。

## 已有内容与处理标记

**一次形成完整号码结果**：写入前汇总所有符合条件的申请单号。空白格一次写入全部唯一候选，不能先写第一个号码再去寻找其他号码。已有号码保持原有内容和顺序，本次完整结果由原有号码与全部候选合并形成，不删除、替换或清空旧号码；新号码按编号稳定排序，用 / 连接。已有号码在本次源文件找不到时列入核对报告并保留，不能把它当成应删除的无效号码。若用户本次明确只填空白格，使用 --only-empty。

**是否加急只填空白格**：有内容时保留，即使不是“内有成品”也不覆盖。“内有成品”是供用户处理的临时提示；用户处理后删除的标记不再补回。它不是必须始终留在表格里的保存结果。

为同一文档复用处理状态文件，按 **文档、工作表、成品工单号** 记录已提交、已确认保存或已处理的标记。已记录工单后来标记变空，不自动重新标记；用户明确要求重新标记时再更新该记录。正常流程只对工具明确接受提交的实际提示行运行 `record-submitted`，记录 submitted，不额外读回；submitted 不代表已复核持久保存。现有 marked 历史继续保留；仅在用户明确要求复核或异常恢复时已有快照确认标记存在，才用 `remember-markers` 记录 marked。用户明确说明已处理后记录 processed，不把未验证的提交说成已经确认保存。

状态文件与业务数据放在已授权的工作目录，不放入 skill。Codex 桌面有跨聊天共用的可写工作区时，可使用其 workflow-state/packaging-picking-match/文档标识SHA256.json；否则保存在本次任务 work/，并保留记录位置。下一次调用查找或使用同文档的已有状态文件。新会话缺少处理历史时不能仅凭空白猜测是否已处理；用户明确提供的处理信息优先于旧计划。

## 执行与交付

1. 复用本次已确认的源文件、文档及授权表范围；缺失且无法从上下文确定才询问。源文件只读，计划与输出写本轮新目录，不放入技能目录。
2. 只用[官方钉钉MCP契约](../packing-order/references/dingtalk-mcp.md)完整采集或复用同轮有效快照；缺能力停止并说明。普通表与AI表使用各自身份，不执行浏览器兼容脚本。
3. 按[本技能batch输入](references/batch-run.md)一次prepare全部指定表。原audit负责完整候选、精确计算和分配；主代理验收输入/产物身份、哈希、完整性、表覆盖、关键计数及全部异常，不重复重算成功行。
4. 由主代理按共用MCP契约生成submit-spec、运行一次本地check-before并提交。报告以实际回执为依据；失败/unknown按[停止与恢复](../packing-order/references/performance.md#停止与恢复)，不重放整批。
5. 逐格报告工作表、行或记录身份、字段、工单、原值、完整结果及本次新增号码；另列缺字段、未匹配和保留原因。不把请求已发出或submitted说成独立持久保存复核。

本地批量委派及接收按[共用分工](../packing-order/references/luna-subagent.md)，正常输出按[执行约定](../packing-order/references/performance.md)。主代理负责正式提交；领料report与提示历史写入也由主代理执行。

## 离线助手

scripts/match_picking.py 使用 Python、openpyxl 和标准库，不控制浏览器，不修改输入 XLSX/TSV。Codex 桌面可通过 load_workspace_dependencies 找到捆绑 Python。每次用 skill 内的真实脚本路径调用，计划及快照留在任务工作目录。

~~~text
python scripts/match_picking.py state-key --target-id <本次文档URL或稳定标识>
python scripts/match_picking.py plan --source 领料单.xlsx --target before.tsv --sheet-name <工作表名> --target-id <文档标识> --state picking-state.json --out plan.json
python scripts/match_picking.py check-before --plan plan.json --snapshot latest.tsv
python scripts/match_picking.py record-submitted --plan plan.json --rows <实际已提交提示的行号> --state picking-state.json
python scripts/match_picking.py record-processed --plan plan.json --rows <用户确认已处理的行号> --state picking-state.json
~~~

- `verify`、`remember-markers` 保留供用户明确要求复核或异常恢复时使用；正常填写流程不运行，也不为它们额外采集写后快照。
- 状态文件首次不存在时按空状态读取；每个工作表共用同文档的状态文件。verify 和 check-before 可用 --state 指定较计划更新的处理记录。
- 支持源/目标 XLSX、TSV、CSV。源默认读取有完整表头的工作表，必要时用可重复的 --source-sheet 指定；目标多表 XLSX 用 --target-sheet 选择。MCP导出的本地TSV矩阵 用 --sheet-name 标识真实工作表，采用从A1起的完整矩阵。
- 默认前 20 行找表头；--header-search-rows 可调整。截取范围不从 A1 开始时用零起点的 --row-offset、--column-offset，后续快照保持相同范围。目标确实使用已确认的工单列别名时才用 --workorder-column，默认“包装工单”。
- check-before 检查包装工单是否变化以及拟改格是否仍是原值/已经是结果，不受无关列更新影响。verify 核对计划写入的号码、原有号码保留、受保护的已有内容；已记录的处理标记可被用户清空，不自动重写。
