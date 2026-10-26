# 第1步：销货单、借出单填写

读取并执行本项目的 [e10-shipment-sheet-fill](../../e10-shipment-sheet-fill/SKILL.md)，按需读取其计划器输入说明。该技能维护筛选、日期分配、字段映射和备注合并的完整规则，不在本目录复制一份。若项目技能缺失，先恢复项目完整版本并报告具体依赖，不跳过本阶段。仅修改技能不因此执行业务。

## 范围与已确认例外

- 沿用本次文档链接。指定工作表时，本轮填写和后续工单/领料只写这些表；仍读取整本文档所有sheet的“单号”列精确去重，包括非日期表。先依据完整日期sheet目录执行日期分配，再限制写入范围，不能将范围外日期改填到指定表。范围外待填单号、日期和明细数单列，区别于缺表。未指定工作表且无法从上下文确定下游范围时，只澄清下游范围，不自行扩大工单或领料操作。
- 计划器的销货/借出适用边界见该技能输入说明；本页不覆盖或补造其输入。填写阶段S263/S630与后续建单S263/L630分别沿用各自规则。

## 读取、提交与交接

1. 复用packing-order同一E10会话完整读取两模块，按[钉钉快照生命周期](dingtalk-mcp.md#快照生命周期)完成首轮采集；本页只补充新明细追加特例。其他表参与去重不产生下游写入授权，会话在整轮结束统一退出。
2. 本阶段保留完整原始单据/明细、去重目录、真实目标表头和源字段映射；查询日及筛选按填写技能固定。
3. 首次追加前完成两个模块全部候选的筛选、日期分配和字段映射。保留原表头、原行及流程列；无真实来源的包装工单、领料号等列留空。一次核对文档/表身份、完整去重范围、拟写空白范围和全部计划；用官方电子表格MCP追加，保存started、明确A1范围、完整values、明细ID、原始请求与实际回执，不套用只更新现有格的匹配提交契约。
4. 全部计划写入明确成功（或完整筛选结果明确零新增）后才进入工单阶段。失败、部分成功或unknown先停止依赖操作，按原异常规则查清实际结果，不能重放追加。已按规则暂缓的缺表、范围外或数据异常记录不计成功、不进入本次下游；报告原因后其余成功范围可继续。
5. 将实际确认成功的新增行按原目标表头、真实sheetId和A1坐标合入指定表完整快照，保留原有行、更新本地range和连续row_indices，并记录原快照、计划、回执哈希及原采集时间，标明 `snapshot_basis:local_receipt_projection`。写入新文件，不改原采集，也不声称新线上读取。现有 `sheet_snapshot_project.py` 仅处理匹配阶段现有格更新，不能直接拿来追加行；使用本轮本地追加适配并核对完整性。无新增直接沿用原快照。
6. 第2步的19来源scope、source_check，第4步完整匹配计划均引用上述填写后的最新完整快照，包含指定表原有行和本轮成功新增行。新增借出明细中的19来源也须纳入原完整范围校验。不得使用填写前旧快照漏掉新行，不将本次新增行集合替代全部指定表。工单成功回填后再按原助手投影，供第5步使用。

最终报告增加：两模块实际新增单号/明细数及各sheet行数、已有单号剔除、明细剔除、借出已结束明细、缺表/范围外及异常。恢复或明确接续后续阶段时，依据已有真实回执跳过完成的填写阶段，不重新追加。

## 保留字段类型的固定追加入口

上文第3步使用以下固定入口，不再将所有列经字符串接口提交后直接记完成。单号、来源号、品号、特征码等保持文本；预计发货日期为日期数值，销货明细_业务数量为数值。用户已手工转换的数据不自动重写。

完整审计后的source-plan须包含status=prepared_only、allowed_write_sheets及各表真实headers/values/source明细ID；使用完整快照绑定真实追加行号：

```text
python scripts/shipment_append_plan.py --source-plan 本轮已审计填写计划.json --snapshot 本轮完整快照.json --sheets 本次授权表名逗号分隔 --out 本轮typed-append-plan.json
```

主代理加载scripts/shipment_sheet_submit.js的shipmentSheetSubmit和既有sheet_mcp_submit.js的packingSheetSubmit，调用：

```javascript
await shipmentSheetSubmit.runWithTools({tools,workDir,planPath,outputDir,progress,
  persistRaw:packingSheetSubmit.persistRawWithTools});
```

outputDir为本轮工作目录内全新目录。执行器先完整校验，再用set_range_from_csv(autoConvert=false,allowOverwrite=false)写入文本矩阵，仅转换真实表头预计发货日期、销货明细_业务数量。日期严格校验ISO日期并计算电子表格日期序号，设置yyyy-mm-dd；数量校验十进制数值，设置General。其他列不推断类型。文本以等号开头、日期非法或数值超出精度时在任何写入前停止。

started独占保存完整操作计划。任一步失败或unknown即停，不重放追加；仅文本写入、类型转换和格式步骤全部明确成功才进入工单阶段。交接投影须保留完整阶段回执，正常不做写后整表读回。

全部步骤成功后用同一固定助手交接完整快照；助手核对绑定哈希、started及全部逐步真实响应，缺少类型转换/格式成功证据时拒绝投影：

```text
python scripts/shipment_append_plan.py --plan 本轮typed-append-plan.json --receipt 本轮新回执目录/receipt.json --snapshot 本轮原完整快照.json --out 本轮追加后完整快照.json
```
