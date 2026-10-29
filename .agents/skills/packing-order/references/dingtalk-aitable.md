# AI表专用快照

仅确认AI表后读取；共用身份、匹配及回执门槛按[主契约](dingtalk-mcp.md)。

## AI表采集与快照

1. get_base确定真实Base及指定Table；目录缺失时get_tables核实，不把视图ID当tableId。get_tables单次最多10表，选项/类型不明确时才get_fields（最多10字段）。字段名须唯一对应。
2. query_records按必需fieldIds完整读取，limit=100、每次最多100字段。保留全部原始页、筛选及recordId唯一性；默认全部指定表记录，用户限定子集才用明确条件。
3. 沿成功响应非空nextCursor续页，到空游标结束。缺少快照完整条件即停止；游标失效按[钉钉异常恢复](dingtalk-mcp-recovery.md#ai表游标与更新)处理。
4. 单元格按真实类型取语义值；null转空串，保留前导零和精确数值，不用对象字符串、截断显示或猜值。

AI快照：

```json
{
  "schema_version":1,"transport":"dingtalk_mcp","phase":"collect",
  "ok":true,"complete":true,"base_id":"真实Base","target_url":"本轮URL",
  "sheets":[{
    "name":"真实表名","sheet_id":"真实tableId","range":"A1:B2",
    "scope_complete":true,"next_cursor":null,"tsv":"字段甲\t字段乙\n值甲\t值乙\n",
    "mcp":{"base_id":"真实Base","table_id":"真实tableId",
      "record_ids":["真实记录ID"],
      "fields":[{"field_id":"真实字段甲","name":"字段甲","type":"text"},
                {"field_id":"真实字段乙","name":"字段乙","type":"text"}]}
  }]
}
```

示例不用于真实调用。首行真实表头，其余行与record_ids同序、列与fields同序；range精确包含表头及全部记录，零记录仍保留表头。不填浏览器from/copy_scope/projection。A1仅为本地矩阵，提交始终recordId/fieldId。手工来源spec保存真实record_id，不能用同值行移动掩盖身份改变。
