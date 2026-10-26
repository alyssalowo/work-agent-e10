# 包装领料单匹配的本地批次

本页只维护请求和离线规划；表格类型识别、完整采集、快照、submit-spec/check-before、实际提交及report统一见[共用MCP契约](../../packing-order/references/dingtalk-mcp.md)。不运行collect-code、submit-code或旧浏览器桥接器；脚本保留这些命令不代表当前流程允许使用。

## 请求

写本轮UTF-8 request.json，替换所有示意值：

```json
{
  "target_url":"本次真实文档URL",
  "source":"本次源文件.xlsx",
  "state":"同文档实际提示历史.json",
  "sheets":[{"name":"本次表名","workorder_column":"包装工单"}]
}
```

source和state使用绝对路径或相对请求文件的路径。sheets必须覆盖本次全部授权表；workorder_column为核实后的真实列名，别名只在本地映射，不改线上表头。可选source_sheets选择源文件表，header_search_rows默认20。普通表可显式提供已核实range；省略时由完整MCP快照确定范围，不套用历史行列数。AI的range仅是本地投影。

state绑定同文档实际最新处理历史，不能把子代理副本或新空文件当成已有历史。用户明确只填空白时才加only_empty=true；已有号码的默认合并规则仍在SKILL.md。

## 一次规划

python使用load_workspace_dependencies定位的捆绑环境；以本技能脚本的实际绝对路径执行：

```text
python scripts/batch.py prepare --request 本轮request.json --snapshot 本轮完整snapshot.json --out-dir 本轮新计划目录
```

一次读取源文件并规划全部指定表，输出batch.json、各表plan、summary.json、changes.csv、exceptions.csv及原audit。缺完整快照或audit失败不提交。普通未匹配、候选不足、旧值冲突按[业务规则](../SKILL.md)保留，确定项才进入提交。

完整计划核对后按共用契约提交，实际回执交本技能batch.py report。report会登记实际成功提示submitted并保留marked/processed，必须由主代理运行。完整行级结果留文件，用户可据报告审阅，不为汇总重读或重新匹配。

单表plan/check-before等兼容离线命令见[脚本说明](../SKILL.md#离线助手)，正常不运行verify。
