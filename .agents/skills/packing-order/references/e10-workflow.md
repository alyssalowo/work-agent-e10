# E10 领料接口

本页维护接口字段和已验证语义；流程范围和本地命令见[后续阶段](downstream-stages.md)，会话协议见[MCP会话](mcp-session.md)。工具schema以本轮缓存为准，缺能力不换入口。失败及未知结果按[停止与恢复](performance.md#停止与恢复)。

## 生成参数

唯一生成入口为“工单管理 → 工单发料 → 工单产生领料申请单”（MO_B012），通过 `e10_prepare_picking_batch` 准备、同会话 `e10_commit_operation` 提交；禁止直接新增领料申请单替代此入口。

分单必须同时勾选“源单不同”和“仓库不同”：显式传入布尔值 `SOURCE_DIFF=true`、`WAREHOUSE_DIFF=true`，不得省略或沿用接口默认值。提交前必须核对 prepare 返回的实际生效 `summary.options`，确认两项均为布尔值 `true`；任一为 `false`、缺失或无法确认时，停止 commit，纠正参数后重新 prepare 并核对。不得提交后再用审核或回填补救分单错误。

使用既有本机加密账号并确认已登录；`e10_lookup_master(kind="DOC",code=本次类型)` 只有unique=true才采用ID，常用类型5D01仍须实时核实。

`e10_resolve_picking_applicant(applicant_name=完整姓名)` 要求唯一员工。员工编码不一定等于登录名；从employee.EMPLOYEE_D核对IS_PRIMARY、START_DATE、END_DATE，取申请日有效唯一ADMIN_UNIT_ID，不用员工主档可能为空的Owner_Dept代替。

`e10_prepare_picking_batch` 参数：

```text
workorder_nos = 本轮生成清单的完整去重工单列表
options = {
  NEW_DOC_ID: 当前申请单据类型ID,
  EMPLOYEE_ID: 唯一申请人员ID,
  ADMIN_UNIT_ID: 申请日有效主部门ID,
  SOURCE_DIFF: true,
  WAREHOUSE_DIFF: true,
  APPLY_DATE: 本次申请日期yyyy-MM-dd,
  APPLY_TRANSACTION_DATE: 本次交易日期yyyy-MM-dd
}
```

每组1–200个不重复工单，同实际工厂和工作中心。未指定替代工作中心时，接口从实际来源设置WORK_CENTER_SUPPLIER_ID_NEW，不传全零ID。实际分组与缺字段补查见后续阶段，不按单号前缀猜工厂。

prepare返回operation_id、全部candidates和excluded_workorders；主代理核对完整计划后同会话commit。内存计划不跨进程保留，状态可用e10_operation_status查询。

生成前保留关联申请单/待审集合和运行时间。批量回执可能只给generated=true与选中工单，不能当成实际申请单数；用生成前后标准查询、实际关联工单和执行时间归因，区分本次确定生成与并发新出现的其他单据。

## 审核接口

`e10_query_picking_workflow` 单头查询使用本次applicant_name、approval_status="N"、include_detail=false、start_row=1、max_rows=1000。采齐全部分页并固定清单；查询结果须同时匹配APPLICANT_NAME、APPLICANT_ID及N。范围定义由后续阶段维护。

一次 `job=approve` 串行调度整个领料清单，内部逐张 `e10_prepare_document(module="ISSUE_RECEIPT_REQ",action="confirm",document_no=...)`，核对当前Owner_Emp、N和版本后commit。已审核no_changes记跳过。工单批量审核是另一入口，不能混用。

已验证单据级原生入口为IConfirmService.Execute(ConfirmContext)，构造必须带当前登录用户和实际日期：

```csharp
new ConfirmContext(plan.Oid, logOnService.CurrentUserId, DateTime.Now)
```

无日期构造会给公元1年而触发存货开放期间错误。审核人不等于申请员工；仍受实际开放期间限制，不伪造日期或调整期间。此说明用于核实安装实现，不授权MCP外直接调用。清单调度不等于原子集合事务，不以正式单据测试替代服务或并发。

## 明细查询

`e10_query_picking_workflow` 用workorder_nos精确列表：1–500个去重完整工单号，与旧单值workorder_no互斥；旧单值不能接收分号串。范围及source-request/source命令只在后续阶段维护。

实际日期、人员、detail_close按请求保留。DETAIL_CLOSE过滤明细CLOSE，不是单头CLOSE。include_detail=true；沿原filters和next_start_row采到has_next_page=false，单头按ID、明细按(ID,DETAIL_ID)去重，分页不可靠即停。

| MCP字段 | 匹配源表头 |
| --- | --- |
| WORKORDER_NO | 领料申请单明细_工单单号 |
| DOC_NO | 申请单号 |
| WAREHOUSE_NAME | 领料申请单明细_仓库名称 |
| ITEM_CODE | 领料申请单明细_材料品号 |

缺失字段保留空值并报告，不猜品号或仓库。审核状态参与规则由后续阶段的完整源校验维护；已有号码保留和仓库分类由领料匹配技能维护。
