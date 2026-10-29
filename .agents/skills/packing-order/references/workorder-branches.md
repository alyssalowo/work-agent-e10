# 订单产生工单、手工产生工单与统一审核

完整packing-order先按[第1步填写规则](shipment-sheet-fill.md)完成销货单、借出单、出库申请单填写及快照交接，再执行本控制器：订单产生工单 → 手工产生工单 → 统一审核 → 筛选 → 匹配 → 领料。控制器本身不执行前置填写。有实质工作时按[subagent分工](subagents.md)委派本地准备；主代理统一串行调度E10准备、正式创建、审核、[钉钉表格MCP](dingtalk-mcp.md)及共享状态。不并发开单。两分支均须成功回执，零新增也须显式成功。

按[工单来源分类](../SKILL.md#工单来源分类)分为两类：手工来源在“维护工单”中逐单新建；其余通过“订单产生工单”模块按技能条件批量生成。内部既有 `manual19` 标识及示例文件名继续兼容，业务含义覆盖全部手工来源，不再限定19开头。

## 审核范围

`approval` 须包括 `planner_name`（完整生管人员姓名）、`doc_type_code`（5101）和 `scope`：

- `planner_pending`（默认）：维护工单中该生管人员、单据日期为执行当天的所有未审核包装工单。默认生管人员朱浩龙、单据类型5101、审核状态N；日期在collect_approval运行时按北京时间自动取当天，无额外工厂或本批新增限制。
- `generated_only`：仅本轮生成回执中的工单。仍核对生管人员及类型，其他既有待审核单据保留。已被并发审核的本批工单按实际状态跳过，不再审核。

完整流程和未限定范围的“生成并审核”均使用planner_pending；仅用户明确要求只审核本轮生成对象时使用generated_only。保留当前会话明确的用户排除决定，不为了减少调用缩小授权集合。

审核查询和批量准备均显式传 `doc_date=yyyy-MM-dd`，服务端以该日00:00（含）至次日00:00（不含）限定单据日期。日期写入pending.filters并随完整清单冻结，所有分页、准备、提交及同批续办都沿用同值；跨日不自动扩大或切换范围。下一轮collect重新计算当天。订单来源查询的固定起始日期2026-01-01和后续匹配、领料查询规则不因此改变。

## 控制器调用

控制器为 `scripts/workorder_branches.py`，通过 `mcp_session` 的同一已登录会话调用。先按[订单参数](order-workorders.md)和[手工来源行级范围](manual19-runner.md)形成两个输入文件，再在本轮目录写 `workorders-config.json`：

```json
{"schema_version":1,"order_request":"order-request.json","manual_scope":"manual19-scope.json","approval":{"scope":"planner_pending","planner_name":"朱浩龙","doc_type_code":"5101"}}
```

配置字段为上述四项。`order_request`、`manual_scope` 是相对本轮工作目录的JSON路径；订单文件须包含完整 `generation`，可含 `batch_size`，若带 `approval` 必须与总配置完全相同。手工来源文件须有明确的 `specs` 列表；零新增时用 `[]`，仍提供完整来源快照和明确排除项。审核范围及日期冻结见上节；配置不写死当天。

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

`prepare` 先以本地 `complete_manual_scope` 检查指定表内所有符合条件的手工来源空白行均已进入spec或有明确排除，再串行查询全部订单、准备订单候选和手工来源草稿；不保存正式单据。输出 `workorders/prepared.json` 及 `prepared_sha256`，主代理核对两分支完整计划。正式创建前检查全部草稿仍在原会话的25分钟门槛内，并检查冻结证据。`create` 必须提交准备哈希确切值作为 `approved_sha256`，以及本轮完整包装计划快照 `source_check`；默认直接指向准备时原采集文件，不要求新文件或晚于prepare的修改时间。原文件哈希、全部表身份及手工来源受保护字段仍逐项校验，回执注明复用依据且不声称刷新线上。订单来源版本与既有关联由原MCP在提交前复核，不将包装表伪装成订单接口快照。哈希参数表示主代理已核对计划，不要求用户重复批准已授权业务。手工底层助手 `manual_prepare` / `manual_create` 仅准备和创建、不审核；旧 `manual_execute` 禁用。

订单批次在完整订单范围内先发现候选，再对有候选的批次用实际来源订单准备最终草稿；无候选订单不进入最终保存操作的版本保护集。异常中断、准备恢复及审核续办仅在触发时读取[控制器异常恢复](workorder-branches-recovery.md)。正常流程不预读恢复清单。

只有两分支均通过完整 `creation-gate` 后才调用 `collect_approval`。它返回完整待审核清单及 `pending_sha256`，同时保留全部筛选行和本次selected集合。主代理按已授权的 `approval.scope`、`planner_name`、`doc_type_code` 和本批 `doc_date` 一次核对清单，以pending哈希作为 `approve` 的 `approved_sha256`。`approve` 一次调用 `e10_prepare_workorder_confirmation_batch`，核对整批准备结果，再一次提交同一operation_id；内部完成版本核验、逐单审核和结果保存，主代理不逐单发送请求或读取本地回执。批量提交复用单据级原生审核服务，不是原子集合事务；任一失败/unknown立即停止后续项并保留已成功、失败和未尝试项，不自动重试。`planner_pending`须覆盖当天、人员、单别及N筛选的完整集合；`generated_only` 必须涵盖两分支新单并集。所有分页、准备、提交和同批异常续办沿用冻结的doc_date，下一轮才重新取当天。

`status` 只读本轮阶段状态。控制器整体 `creation-gate` 确认两个分支均有成功创建回执（含零新增）后才开放统一审核。审核结束，主控制器取得实际逐单Y回执后同步调用本地 `complete_after_approval`；它是纯本地函数，不是MCP job或phase，不直接调用MCP，只验证指定手工来源工单的业务字段及实际审核Y。订单分支及完整审核集合由控制器和完整审核回执验证。整体门槛通过后，先按[匹配源工单筛选](workorder-filter.md)取得完整结果，再进入工单匹配；异常、回执缺失或结果未知时停止，不自动重放或重试。后续匹配与领料仍按原技能执行。

## 明确排除后的审核续办

异常时读取[原证据与续办命令](workorder-branches-recovery.md#明确排除后的审核续办)。
