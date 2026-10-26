# 双来源工单创建与统一审核

完整packing-order先按[第1步填写规则](shipment-sheet-fill.md)完成销货单、借出单填写及快照交接，再执行本控制器：订单创建 → 19来源创建 → 统一审核 → 筛选 → 匹配 → 领料。控制器本身不执行前置填写。有实质工作时按[subagent分工](luna-subagent.md)委派本地准备；主代理统一串行调度E10准备、正式创建、审核、[钉钉表格MCP](dingtalk-mcp.md)及共享状态。不并发开单。两分支均须成功回执，零新增也须显式成功。

## 审核范围

`approval` 须包括 `planner_name`（完整生管人员姓名）、`doc_type_code`（5101）和 `scope`：

- `planner_pending`（默认）：维护工单中该生管人员、单据日期为执行当天的所有未审核包装工单。默认生管人员朱浩龙、单据类型5101、审核状态N；日期在collect_approval运行时按北京时间自动取当天，无额外工厂或本批新增限制。
- `generated_only`：仅本轮生成回执中的工单。仍核对生管人员及类型，其他既有待审核单据保留。已被并发审核的本批工单按实际状态跳过，不再审核。

完整流程和未限定范围的“生成并审核”均使用planner_pending；仅用户明确要求只审核本轮生成对象时使用generated_only。保留当前会话明确的用户排除决定，不为了减少调用缩小授权集合。

审核查询和批量准备均显式传 `doc_date=yyyy-MM-dd`，服务端以该日00:00（含）至次日00:00（不含）限定单据日期。日期写入pending.filters并随完整清单冻结，所有分页、准备、提交及同批续办都沿用同值；跨日不自动扩大或切换范围。下一轮collect重新计算当天。订单来源查询的固定起始日期2026-01-01和后续匹配、领料查询规则不因此改变。

## 控制器调用

控制器为 `scripts/workorder_branches.py`，通过 `mcp_session` 的同一已登录会话调用。先按[订单参数](order-workorders.md)和[19来源行级范围](manual19-runner.md)形成两个输入文件，再在本轮目录写 `workorders-config.json`：

```json
{"schema_version":1,"order_request":"order-request.json","manual_scope":"manual19-scope.json","approval":{"scope":"planner_pending","planner_name":"朱浩龙","doc_type_code":"5101"}}
```

配置字段为上述四项。`order_request`、`manual_scope` 是相对本轮工作目录的JSON路径；订单文件须包含完整 `generation`，可含 `batch_size`，若带 `approval` 必须与总配置完全相同。19来源文件须有明确的 `specs` 列表；零新增时用 `[]`，仍提供完整来源快照和明确排除项。审核范围及日期冻结见上节；配置不写死当天。

请求JSON的阶段调用字段如下，所有文件路径相对MCP工作目录：

```json
{"job":"workorders","phase":"prepare","config":"workorders-config.json","directory":"workorders"}
{"job":"workorders","phase":"create","config":"workorders-config.json","directory":"workorders","approved_sha256":"prepare回执中的prepared_sha256","source_check":"workorders-before-create.json"}
{"job":"workorders","phase":"collect_approval","config":"workorders-config.json","directory":"workorders"}
{"job":"workorders","phase":"approve","config":"workorders-config.json","directory":"workorders","approved_sha256":"collect_approval回执中的pending_sha256"}
{"job":"workorders","phase":"status","config":"workorders-config.json","directory":"workorders"}
```

两分支本地数据准备可并行；MCP准备和正式写入不得并行。默认完整流程只发送 `job=workorders` 控制器请求，不直接调用底层手工助手。主代理核对完整计划后，串行调用 `create`：控制器复用本轮完整包装表快照并核对各自准备哈希，再正式创建；每个分支都须产生成功回执，零新增也必须显式成功。创建失败、超时或状态未知时停止，不自动重试，先用status与实际业务查询厘清结果。

订单分支沿用 `order_workorders.py` 的订单筛选、每批上限50、完整候选检查和源版本保护；手工分支沿用 `manual19` 的行级来源、BOM及reference检查。控制器统一检查创建完整性。单独明确只要求订单生成/审核时，可直接使用 `order_workorders.py` 原阶段。

## 哈希门与统一审核

prepare产生的准备哈希及collect_approval产生的pending哈希从对应文件直接读取并写请求，不手抄长值；主代理先核对实际内容与文件身份。已有完整控制器回执足以定位时不再创建一份同义审计或证据索引。

`prepare` 先以本地 `complete_manual_scope` 检查指定表内所有符合条件的19来源空白行均已进入spec或有明确排除，再串行查询全部订单、准备订单候选和19来源草稿；不保存正式单据。输出 `workorders/prepared.json` 及 `prepared_sha256`，主代理核对两分支完整计划。正式创建前检查全部草稿仍在原会话的25分钟门槛内，并检查冻结证据。`create` 必须提交准备哈希确切值作为 `approved_sha256`，以及本轮完整包装计划快照 `source_check`；默认直接指向准备时原采集文件，不要求新文件或晚于prepare的修改时间。原文件哈希、全部表身份及19来源受保护字段仍逐项校验，回执注明复用依据且不声称刷新线上。订单来源版本与既有关联由原MCP在提交前复核，不将包装表伪装成订单接口快照。哈希参数表示主代理已核对计划，不要求用户重复批准已授权业务。手工底层助手 `manual_prepare` / `manual_create` 仅准备和创建、不审核；旧 `manual_execute` 禁用。

订单批次在完整订单范围内先发现候选，再对有候选的批次用实际来源订单准备最终草稿；无候选订单不进入最终保存操作的版本保护集。若提交中断，保留 `started`、失败、每批prepare/commit及已完成的真实回执。用以下纯本地命令从state、失败prepare/commit、operation状态、实际关联查询和审核清单构造恢复凭据；状态必须明确为prepared，实际结果可以包含准备前已有的关联工单，但不能出现任何新增ID：

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

只有两分支均通过完整 `creation-gate` 后才调用 `collect_approval`。它返回完整待审核清单及 `pending_sha256`，同时保留全部筛选行和本次selected集合。主代理按已授权的 `approval.scope`、`planner_name`、`doc_type_code` 和本批 `doc_date` 一次核对清单，以pending哈希作为 `approve` 的 `approved_sha256`。`approve` 一次调用 `e10_prepare_workorder_confirmation_batch`，核对整批准备结果，再一次提交同一operation_id；内部完成版本核验、逐单审核和结果保存，主代理不逐单发送请求或读取本地回执。批量提交复用单据级原生审核服务，不是原子集合事务；任一失败/unknown立即停止后续项并保留已成功、失败和未尝试项，不自动重试。`planner_pending`须覆盖当天、人员、单别及N筛选的完整集合；`generated_only` 必须涵盖两分支新单并集。所有分页、准备、提交和同批异常续办沿用冻结的doc_date，下一轮才重新取当天。

`status` 只读本轮阶段状态。控制器整体 `creation-gate` 确认两个分支均有成功创建回执（含零新增）后才开放统一审核。审核结束，主控制器取得实际逐单Y回执后同步调用本地 `complete_after_approval`；它是纯本地函数，不是MCP job或phase，不直接调用MCP，只验证指定19来源工单的业务字段及实际审核Y。订单分支及完整审核集合由控制器和完整审核回执验证。整体门槛通过后，先按[匹配源工单筛选](workorder-filter.md)取得完整结果，再进入工单匹配；异常、回执缺失或结果未知时停止，不自动重放或重试。后续匹配与领料仍按原技能执行。

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

续办结果与原批次分开保存并明确关联，不清除原失败状态。19来源只为实际Y生成bindings，明确排除的N项单列为下游排除，不生成伪Y或伪绑定；原checkpoint保持不变。
