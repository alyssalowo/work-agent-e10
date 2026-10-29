# 工单控制器异常恢复

仅创建/审核中断或失败时读取。先遵守[主流程范围与哈希门](workorder-branches.md)，不缩小planner_pending，不重放unknown。

## 创建准备恢复

若提交中断，保留 `started`、失败、每批prepare/commit及已完成的真实回执。用以下纯本地命令从state、失败prepare/commit、operation状态、实际关联查询和审核清单构造恢复凭据；状态必须明确为prepared，实际结果可以包含准备前已有的关联工单，但不能出现任何新增ID：

```text
python 实际技能目录/scripts/workorder_branches.py --build-recovery-manifest --base 本轮工作区 --run-dir 本轮分支目录 --status 失败operation-status.json --result 失败operation-actual.json --audit 完整成功审计.json --out 恢复清单.json
```

python使用本轮按[会话说明](mcp-session.md)核实的解释器，脚本用实际技能路径；所有路径须满足--base边界。

最小恢复清单由本地工具生成，输入路径均在 `--base` 内并自动记录SHA256；`discards` 可为空，首次显式恢复会再次核对状态和实际来源关联，再逐个丢弃明确prepared的旧草稿。清单形状如下，引用必须是文件相对路径及其SHA256，批次数量从state推导：

```json
{
  "schema_version": 1,
  "mode": "generation_recovery",
  "failed": {"stem": "generation/00100", "operation_id": "...", "prepare": {"path": "...", "sha256": "..."}, "status": {"path": "...", "sha256": "..."}, "commit": {"path": "...", "sha256": "..."}, "result": {"path": "...", "sha256": "..."}, "source_ids": ["..."]},
  "untouched": [],
  "completed": [],
  "discards": [],
  "audit": {"path": "...", "sha256": "..."}
}
```

随后经mcp_session显式发送完整控制器命令：

```json
{"job":"workorders","phase":"reconcile_prepare","config":"workorders-config.json","directory":"workorders","recovery":"恢复清单.json"}
```

`resume_prepare` 是同一实现的兼容别名（两者都调用resume_prepare方法）；本文统一写reconcile_prepare，不将别名当作另一条可绕过状态的恢复路径。助手先封存原证据，再在每次新prepare前持久化checkpoint意图，返回后保存原始packet、SHA256、operation、会话和时间。若后续批次明确拒绝，使用相同凭据再次显式调用，只复用已验证、同会话、未过期且operation仍为prepared的草稿，并接续剩余批次；不重放已成功的prepare或commit。超时、状态未知、证据哈希变化、会话变化、草稿过期或已完成checkpoint均停止并保留现场，不自动重试。成功回执不重建；恢复完成后原凭据再次调用会被拒绝。

## 明确排除后的审核续办

批量审核失败后先保留原pending、batch-commit、state中的failure/in_flight及原操作记录。只有用户明确排除失败对象，且主代理已取得排除对象的实际N单据和操作状态回执，才生成恢复证据；原unknown操作永远不重交。remaining_ids严格指原回执证明尚未尝试的项目，不包含failed_item或已确认成功项。

命令中的recovery文件路径相对本轮MCP工作目录；恢复JSON内的证据path则相对订单分支目录（默认workorders/orders），须由实际回执路径生成并核对SHA256，不内联或伪造实际单据：

```json
{"original_pending_sha256":"原pending文件字节SHA256","excluded_ids":["用户明确排除ID"],"excluded_reasons":{"用户明确排除ID":"用户确认原因"},"excluded_documents":[{"path":"evidence/get-document.json","sha256":"实际回执SHA256"}],"unattempted_ids":["原remaining减排除后的ID"],"operation_status":{"path":"evidence/operation-status.json","sha256":"实际回执SHA256"},"batch_commit":{"path":"approval/batch-commit.json","sha256":"原始提交回执SHA256"}}
```

主代理核对完整证据后发送两个分离阶段：

```json
{"job":"workorders","phase":"resume_approval","config":"workorders-config.json","directory":"workorders","recovery":"approval-recovery.json"}
```

控制器核对所有引用哈希、原批次身份、已成功/未知/未尝试互斥、排除对象实际N及完整身份，并重取当前全部N清单冻结；返回continuation_id及pending_sha256。新的selected只取原remaining减明确排除，不把新出现N或未知项自动加入。主代理一次核对新清单，再发送：

```json
{"job":"workorders","phase":"approve_continuation","config":"workorders-config.json","directory":"workorders","continuation_id":"返回的32位标识","approved_sha256":"返回的新pending文件SHA256"}
```

续办结果与原批次分开保存并明确关联，不清除原失败状态。手工来源只为实际Y生成bindings，明确排除的N项单列为下游排除，不生成伪Y或伪绑定；原checkpoint保持不变。
