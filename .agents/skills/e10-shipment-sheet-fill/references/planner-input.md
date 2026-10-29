# 三来源计划器输入契约

scripts/plan.py直接处理销货、借出和出库申请，不连接ERP或钉钉，不执行写入。来源字段先按当前MCP元数据准确归一；不得构造不存在的状态或伪装模块。

## 完整性声明

输入须有records_complete:true、sheets_complete:true和modules_complete数组。数组只允许sales、borrow、outbound_application且不重复；即使某模块零记录也须在完整读取后列出。允许明确限定范围的子计划，但只有三模块均声明完整时输出all_modules_complete:true。完整packing-order首次追加前须核对三个模块及完整原始证据，不能把子计划当整任务完成。

--today显式传用户时区确定的YYYY-MM-DD查询日。保留全部原始明细，包括随后被明细规则剔除的行，销货标签判断在剔除前完成。

## records：每个原始明细一条

| 字段 | 要求 |
| --- | --- |
| module | sales / borrow / outbound_application |
| doc_no、detail_id | 完整单号、模块内唯一稳定明细ID |
| doc_date、expected_date | 严格ISO日期；出库申请expected_date来自真实可发货日期 |
| doc_type_name、doc_type_code | 筛选前真实名称和代码；借出代码必填，其他可为空 |
| item_code、item_name | 明细品号、品名字符串 |
| columns | 真实来源列名和值对象；只允许有限数值、字符串、布尔或null |
| outbound_status | 仅销货必填：不需出库 / 未出库 / 部分出库 / 出库结束 |
| borrow_detail_ended | 仅借出必填：按真实“明细结束”元数据归一的JSON布尔值，不接受字符串或0/1 |
| delivery_label | 销货真实明细标签，空串/null表示空；借出、出库申请只能空或省略 |

借出不要求outbound_status；出库申请不套用销货状态、借出结束或标签提前。借出已结束只剔除相应明细，保留同单未结束明细。通用筛选和各模块前缀/单据名称规则以[技能正文](../SKILL.md)为准。

## 列映射

columns保留单头备注和明细备注；非空明细备注、单头备注依次用/连接，不去重。脚本将借出明细_*、出库申请明细_*统一为内部销货明细_*；输出时按目标真实销货明细_*或借出明细_*表头取值，只写存在的列。

出库申请额外映射可发货日期→预计发货日期、申请人员→业务员，使用真实关联显示姓名，创建人仍独立关联真实用户。顶层单号、品号、品名、标签、原名称和expected_date与columns同义字段冲突会报错，不用覆盖遮盖问题。不预填无来源的箱号、排产或流程状态。

同单号的模块、单头日期、预计日期、原名称和类型代码必须一致；销货出库状态也须一致。跨模块同号、明细ID重复或映射冲突均停止。预计日期保留源实际值，不因分表提前而改变。

## sheets：整本目标文档全部工作表

每项包含sheet_id、name、真实顺序headers、全部非空单号doc_nos，以及已确认ISO日期date；非日期表date:null且照常参与去重。headers保留空标题及列位置，不猜MMDD年份，不省略非日期表。重复日期或sheet_id会报错。

借出与出库申请只选实际预计日对应表，缺表列入missing。只有销货预计日不早于后天、全部原始明细标签完整匹配[sS][0-9]+时提前一天；提前目标缺表才选该日之前最近已有日期表。其他缺表不回退。

## 输出与执行

输出status:prepared_only、modules_complete、all_modules_complete、每表headers/values/source、missing、剔除清单及counts。values是待转换文本矩阵，source保留真实模块和明细ID。输入行守恒核对已有、单据过滤、明细过滤、待写和缺表，不把缺表算成功。

输出不是正式成功回执，也未授权所有日期表。主代理核对完整范围、缺表和指定写入范围后，再绑定真实文档身份和allowed_write_sheets；完整流程沿用固定追加执行器，日期/数量明确按类型写入，其他标识保留文本。失败或unknown不重复追加。
