# 普通表按需采集

`scripts/sheet_read_plan.cjs` 生成确定性读取计划，已接入 `sheet_mcp_submit.js` 的 `collectWithTools({tools,workDir,rawPath,targetUrl,nodeId,sheets,readPlan})`。它只规划读取，不授权业务写入。

输入是 schema_version=1 的 JSON：唯一 round_id，workflow 为 complete / independent_fill / independent_match，document 包含实际 node_id、target_url、完整 sheets 目录。每表含真实 sheet_id/name、nonEmptyRange（range、lastRow、lastColumn、lastCell）及由真实表头确认的物理 doc_no_column。范围必须从 A1 开始且边界一致，URL 的 /i/nodes/ 节点必须匹配 node_id，单号列必须在范围内。

- 完整流程 complete 使用 target_sheet_id，目标表一次完整读取，其他表仅读完整单号列。
- 独立填写 independent_fill 默认所有表仅读单号列。确认实际新增 confirmed_additions=true 或必要匹配 match_required=true 后，才对 target_sheet_id 读全文。
- 独立匹配 independent_match 使用 target_sheet_ids，只对指定目标表读全文，其他表仅读单号列。
- same_round_cache 为原始采集文件引用数组，每项为绝对 path 和 sha256。计划与采集时均重新校验哈希、同 round_id、文档/表身份、原始 metadata、真实 CSV 矩阵/连续索引及精确完整范围；失败、恢复中或列级采集不充当完整缓存。主代理仍需判断同轮有效性和已知协作变动。
- conflicts 数组只含受影响的 sheet_id、range 和 reason，产生有界 recheck_range 及 recovery_blocked 状态。补查结果不能自动解除未知提交或冲突，不作完整去重/业务快照。

```text
node scripts/sheet_read_plan.cjs --input 本轮读取请求.json --out 本轮读取计划.json
```

输出独占创建。采集器读取实际元数据与计划范围，校验身份、边界、连续行列索引、无截断及 CSV 矩阵。单号列表头必须是“单号”。所有原始响应及失败仍落盘；省略 readPlan 保留原全表采集模式。

混合采集应分别产出全目录去重索引与实际完整目标快照：

```text
python scripts/sheet_snapshot.py --raw 本轮混合原始采集.json --doc-index --node-id 实际节点 --out 本轮单号目录.json
python scripts/sheet_snapshot.py --raw 本轮混合原始采集.json --full-sheets 实际目标表名 --node-id 实际节点 --out 本轮目标完整快照.json
```

转换会先验证全部混合采集，单号目录显式为 document_numbers_only，仅供原始输入 sheets[].doc_nos 的离线整理；保留原始文件与哈希及全表目录，不能将其伪装成完整业务快照。完整目标表继续使用真实表头、TSV、行列索引。CLI 不覆盖旧输出。无新增沿用有效完整快照；有成功变更按真实回执投影，不重新生成同义报告。

本地读取/缓存/投影不是线上原子条件更新，也不能证明协作者未修改线上表格。已知冲突和 unknown 仍按[异常恢复](dingtalk-mcp-recovery.md)处理。权限开放状态见[第二轮控制门槛](round2-controls.md)。
