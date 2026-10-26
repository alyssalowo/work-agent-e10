# 订单产生包装工单及审核

本页维护订单分支的generation字段、固定默认值及候选/提交保护。完整packing-order由[双分支控制器](workorder-branches.md)调用；仅明确指定独立订单阶段时才直接启动本页助手，不提前审核完整流程的订单分支。

## 运行参数

仅当用户当次明确只要求订单生成/审核阶段时，使用 `scripts/order_workorders.py` 独立完成该范围。packing-order 默认完整流程不得使用本页直接审核，而须按 workorder-branches.md 等两来源创建后统一审核。脚本通过同一个 `mcp.ClientSession` 保持登录和内存计划；同一E10会话不并发生成或审核。

在本轮工作目录写UTF-8请求JSON；不放在技能目录、不复用历史工作目录。根字段为 `generation`、`approval` 和可选 `batch_size`；仅生成可省略approval，仅审核可省略generation。日期、姓名和查询范围优先取本次明确要求，否则采用下列用户确认的固定默认值，不能从历史截图猜日期或人员。

`generation` 须包括：

| 字段 | 内容 |
| --- | --- |
| plant_name | 完整工厂名称；不使用历史GUID |
| date_from | 本次订单单据日期含起日，yyyy-MM-dd |
| excluded_sales_departments | 完整销售部门排除名称列表 |
| excluded_order_tokens | 订单号“不包含”列表，单别后保留连字符 |
| doc_type_code | `5101`，仍由MCP核对当前名称、类别、状态及是否自动审核 |
| excluded_1_v_name_keywords | 1-V品号的品名排除词列表 |
| excluded_1_h_name_keywords | 1-H品号的品名排除词列表 |

全部订单筛选按AND组合，沿用原版MO_B002订单选择器及其已审核、未结束、交货工厂约束。仅1-开头成品；1-V与其品名排除词为AND，1-H同理，多个排除词为OR；字母/品名不区分大小写，基础`1-`前缀仍精确匹配。指定单别值为2，不合并不同订单/客户，不重复产生已抛转未指定完工工单，自动审核关闭。当前已确认的排除词是1-V的S263/L630、1-H的DSN；只有用户当次明确修改时才替换，不把词扩展到其他品号。

默认工厂为大寰工厂；订单查询起始日期固定为2026-01-01（含当日，不随执行日期滚动）；生管人员完整姓名为朱浩龙；默认排除销售部门主计划、计划部；默认订单号不包含2319-、2302-、2304-、2318-、2320-、2324-。用户已明确要求以后均沿用，跨对话直接采用，无需再次确认；当次明确修改时以当次要求为准。工厂、人员及部门ID仍从当前E10主数据核实，不能固定历史GUID。此起始日期仅用于订单查询，不改变新单单据日期或表中排产日期。

`approval` 的planner_name、doc_type_code、scope及默认完整集合、doc_date冻结统一按[审核范围](workorder-branches.md#审核范围)。本页独立订单助手采用同一口径；完整流程的审核仍由双分支控制器执行。

## 一次会话完成

E10集成自己的Python环境须已包含MCP SDK，服务器路径从当前安装配置核实。执行帮助和本地请求校验不启动MCP、不登录：

```text
python 技能目录/scripts/order_workorders.py --request 本轮请求.json --validate-request
python -u 技能目录/scripts/order_workorders.py --request 本轮请求.json --server 已安装E10目录/server.py --work-dir 本轮工作目录
```

取得终端session ID后，逐条发送短JSON命令；先检查上一条返回的小摘要，再进入依赖阶段。授权已经明确时由模型连续调度，不再让用户逐阶段确认。

```json
{"job":"login"}
{"job":"prepare_generation"}
{"job":"generate"}
{"job":"collect_approval"}
{"job":"approve"}
{"job":"logout"}
{"job":"exit"}
```

只审核时略过两个生成命令；只生成时略过两个审核命令。仅订单阶段已被用户明确指定时，此处完成后才按用户授权决定是否接续后续阶段；默认完整流程必须使用双分支控制器，不在此助手里另写浏览器或领料实现。

`prepare_generation` 只读查询/内存准备：

1. 订单每页1000条，检查原filters、主键/单号去重、分页连续及has_next_page=false后固定完整清单。
2. 默认每批50张订单（现有接口上限），完整订单清单连续覆盖全部订单，包括零候选批次；不逐张查询、不重查同阶段完整订单集合。`batch_size`仅可设1至50。
3. 每批先用完整订单范围发现候选。若选中候选，核对其真实来源订单；新版E10返回 `commit_guard_scope=selected_source_orders`、完整且状态有效的 `guarded_order_nos/order_versions` 时，直接复用该草稿。旧版缺少或无法核实该契约时，才丢弃未保存的发现草稿，并只按候选真实来源订单重准备；两次候选语义必须完全一致。最终版本检查与既有关联工单检查保护所有实际候选来源，无候选订单不进入提交保护集。零候选批次保留完整结果且不提交。不得跳过状态未知、草稿过期或候选变化的拒绝。

`generate` 同一会话依次提交有候选的已核对批次；保持MCP原有候选来源订单版本及既有关联工单集合的提交前校验。使用正式生成回执中实际新增工单与候选逐项比对，不再进行正常情况下的全量ERP查询。来源、数量或回执不符时停止后续写入。准备操作30分钟过期；助手在接近过期或会话变化时停止，不自动重建草稿。

`collect_approval` 先用e10_query_workorder_workflow（每页500）取得完整稳定集合，再按scope固定清单；不能边审核边翻未审核集合。`approve` 对冻结清单一次调用e10_prepare_workorder_confirmation_batch，再一次e10_commit_operation；主代理按整份清单放行，不逐单调用或轮询回执。批量准备及正式提交均核对MO_ID、DOC_NO、Owner_Emp、DOC_ID、状态及版本，审核人/日期继续由MCP当前登录人和实际日期提供。MCP批量入口内部复用已验证的单据级IConfirmService，逐单保留实际Y结果；不宣称E10原子集合事务。任何失败立即停后续项，并保留已完成、失败/unknown和未尝试对象。

## 速度、输出及异常

- 调度、输出节流、等待及通用异常按[执行约定](performance.md)。本页批次和分页数是接口范围，不作为线上加速承诺。
- `orders/complete.json`、`generation/scope.json`、`generation/completion.json`、`approval/pending.json`、`approval/completion.json`为各阶段凭据。`calls.jsonl`只记录调用次数、耗时及结果路径，可衡量调用减少，不把离线估算说成线上速度或token实测。
- 每次正式写入先独占创建started标记并保存state.in_flight；完成且结果检查通过后才清除in_flight。超时、异常、结果未知或进程重启后保留原标记和回执并停止依赖写入。查询e10_operation_status及受影响实际单据，确认实际状态、修复原因后只处理未完成对象；不能删除标记、换操作编号或新建目录绕过未知状态。助手不自动恢复未知写入。
- 已完成的阶段不可重放；重新启动不恢复服务端内存草稿。未解决失败不会被下次命令覆盖。需要恢复时先说明具体证据，使用现有MCP工具核实；不把单一历史超时案例固化成日常重复查询流程。
- 双分支中断准备的完整恢复输入和命令只在[控制器恢复说明](workorder-branches.md#哈希门与统一审核)维护。独立订单助手不接受控制器命令；不要把只有phase/recovery的片段发送给任一会话。

结束时使用logout/exit仅结束本助手会话。后续匹配复用原batch.py的本地prepare/submit-spec/report，在线读写按[钉钉表格MCP](dingtalk-mcp.md)执行，不导出整表，不增加正常写后读回或刷新复核。
