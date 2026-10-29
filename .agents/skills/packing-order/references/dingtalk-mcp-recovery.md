# 钉钉MCP异常恢复

仅初次不完整、关键范围/身份变化、失败/unknown或用户要求时读取。正常采集、投影与提交按[主契约](dingtalk-mcp.md)。不回退浏览器，不自动转换文档、改字段或权限。

## 普通表采集与提交

采集截断须分段证明无缺口，不手设complete。执行器失败保留.partial，不覆盖final/partial，不自动重试；先确认原范围和原始内容，再显式采集缺少的只读部分并重新验证。

正式写入已有started或非空结果目录阻止整批重放。最终receipt保存前中断，started中范围均可能已受影响；缺receipt不能推断未写入。保留请求和逐项结果，只补查相应操作及必要目标范围；影响重复分配或身份时重建该表完整计划。不得重放unknown追加/更新。遵守[停止与恢复](performance.md#停止与恢复)。

## AI表游标与更新

INVALID_CURSOR/CURSOR_SNAPSHOT_CHANGED仅重采同一只读查询；NON_RESUMABLE_ERROR_CURSOR停止，缺完整条件也停止。保留原页，不混合不同快照，不重放整轮业务。

update失败只查必要recordIds/fieldIds，不用create_records的get_record_write_result推断update结果。区分成功、失败、未知后才决定剩余范围；unknown停依赖写入。集合变化先处理，不靠重排或同值行掩盖增删。正常成功不为复核而重读整表。
