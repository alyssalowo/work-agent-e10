# 本地分页校验与证据索引

本约定供主代理交付的本地子代理任务使用。`scripts/local_review.py` 只读输入，在专用新目录输出，不连接E10/浏览器或网络。分页转换和证据定位可合并交付，已有完整结果直接复用；现有计划/回执已能定位证据时不额外建索引。主代理负责正式业务、共享状态、异常处置与放行。

## 筛选分页与匹配源

命令：

```text
python scripts/local_review.py workorders --manifest <manifest.json> --out-dir <新输出目录>
```

manifest 为 JSON，至少包含：

```json
{
  "schema_version": 1,
  "task_id": "本轮任务标识",
  "filters": {
    "doc_no_prefix": "5101-",
    "date_from": "2026-01-01",
    "excluded_approval_status": "V",
    "excluded_statuses": ["y", "Y"]
  },
  "pages": [
    {"path": "pages/page-001.json", "sha256": "文件SHA256"}
  ]
}
```

`filters` 只允许以上四个键，值必须是本轮主代理确认的筛选条件；示例对应当前已确认的筛选条件，不从旧说明自行推断或补默认值。当前原生查询和助手支持排除V及y/Y；用户改变状态语义时先由主代理核实工具支持，不改写原始结果冒充新条件。`pages` 须列出本次查询全部分页，按原顺序排列；每个 `path` 相对 manifest 所在目录，且提供对应原始文件的 SHA256。分页是否完整由脚本依据原始数据及所给文件检查；输入缺页、内容不符或哈希错误时停止并报告。

成功输出 `workorder-source.tsv` 和 `source-summary.json`。TSV 使用原包装工单匹配源五列表头与字段映射。脚本只验证本地完整分页、筛选字段、必需字段、重复/连续性等可由原始分页核验的完整性项目并生成源文件；它不查询ERP，也不替代后续匹配技能的四字段计划、审计和写前检查。子代理不得重算匹配或精确数量。

## 工单/冲突/最终报告证据索引

命令：

```text
python scripts/local_review.py evidence --manifest <manifest.json> --out-dir <新目录>
```

manifest 至少包含：

```json
{
  "schema_version": 1,
  "task_id": "本轮任务标识",
  "purpose": "pre_picking_inventory",
  "inputs": [
    {
      "role": "本地证据用途",
      "path": "receipts/source.json",
      "sha256": "文件SHA256",
      "pointers": ["/records/0"]
    }
  ]
}
```

`purpose` 只能是 `pre_picking_inventory`、`conflict_evidence` 或 `final_report`。`inputs` 每项须有 `role`、相对 manifest 的 `path` 和 SHA256；JSON 文件可选提供 `pointers`（JSON Pointer 数组）定位原始记录。路径和指针只用于定位，不授权改写输入。成功仅输出 `evidence-index.json`，记录来源身份、哈希、原始JSON定位或CSV记录数等可核查信息；不推断是否匹配、是否审核、是否可领料或如何处置冲突。

领料前任务从原匹配结果、audit材料及真实审核回执整理工单逐行对应和实际审核Y证据；冲突任务整理原匹配/audit/回执中可定位的冲突依据；最终报告任务汇总主代理已完成的报告与回执。领料前核对任务须覆盖全部应处理行及用户排除行：非排除行需要原字段比较与实际Y回执，排除行需要用户已确认的排除依据；任一缺失即返回 `blocked`。冲突任务缺少定位冲突所需的字段比较、原值或原始记录时返回 `blocked`，材料齐备也只表示冲突证据已整理，不表示冲突已解决。均列明缺项和证据路径。不得以脚本成功、索引生成或子代理 `ready` 取代主代理原有门槛，不得自造审核Y。最终报告子代理只汇总主代理已完成材料，不扩大处理范围。

回报及主代理接收按[subagent契约](luna-subagent.md#主代理接收与放行)，完整证据留产物，不重跑同一索引或展开正常成功行。
