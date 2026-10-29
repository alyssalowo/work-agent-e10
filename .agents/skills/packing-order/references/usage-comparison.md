# 离线任务用量记录与比较

`scripts/task_usage.py` 只处理人工准备的 JSON，不连接额度服务、E10、钉钉或其他在线接口。它验证记录并输出确定性汇总，不能自行证明任务质量或额度数据真实；由填写者依据实际用量读数和阶段验收证据填写。

## 记录口径

每条任务记录包括任务身份和类型、模型及推理强度、实际额度、子代理调用数、MCP读取/写入次数、返工次数、`qualified` 布尔值及命名门槛结果。次数必须为非负整数。`qualified` 只能在所有列出的门槛均通过时为 `true`；通过所有门槛但仍有其他原因不合格时，允许为 `false`。

额度字段必须包含真实读数 `value`、`unit`、`basis_id` 和可定位实际读数的 `evidence_ref`。数值读数没有证据引用时记录无效。额度不可获得时把 `value` 和 `evidence_ref` 记为 JSON `null`，不估算、不用token数、提示词长度、模型档位或调用数量代替。`basis_id` 应标明同口径所需的额度类别、账号/窗口和读数范围版本；只有基准与任务的 `unit` 和 `basis_id` 完全相同，且两边均合格且额度数值可用时，才计算 `delta = baseline - actual`。delta 仅是该额度口径的差值，不外推为token节省或因果收益。

每个 `task_type` 最多提供一条基准。没有基准、基准不合格、基准额度缺失、当前额度缺失或口径不一致时，输出对应状态且 `delta` 保持 `null`；不得挑选近似口径或宣称节省。未合格任务不会参与比较。

## 输入 JSON

```json
{
  "baseline": [
    {
      "task_type": "workorder_matching",
      "actual_quota": {
        "value": 12.5,
        "unit": "weekly-used-percent",
        "basis_id": "account-A-weekly-window-read-v1",
        "evidence_ref": "quota-readings.json#/baseline/workorder_matching"
      },
      "qualified": true,
      "gates": {"all_pages_complete": true, "audit_passed": true}
    }
  ],
  "tasks": [
    {
      "task_id": "run-2026-10-29-a",
      "task_type": "workorder_matching",
      "model": "gpt-6.1-terra",
      "reasoning_effort": "high",
      "actual_quota": {
        "value": null,
        "unit": "weekly-used-percent",
        "basis_id": "account-A-weekly-window-read-v1",
        "evidence_ref": null
      },
      "subagent_calls": 1,
      "mcp_reads": 5,
      "mcp_writes": 1,
      "rework_count": 0,
      "qualified": true,
      "gates": {"all_pages_complete": true, "audit_passed": true}
    }
  ]
}
```

`baseline` 项按 `task_type` 提供基准额度和相同的门槛结论。`tasks` 项还须填写 `task_id`、`model`、`reasoning_effort` 及四个计数。真实额度 unavailable 时额度值和证据引用填 `null`，不能省略额度类别或口径标识。

## 运行

```text
python scripts/task_usage.py summarize --input task-usage.json --out task-usage-summary.json
```

命令返回状态、任务数、合格任务数、可比较任务数和输出路径。完整结果逐项列出比较状态、真实基准/当前额度及可计算时的差值。解析、schema或写文件错误以非零退出码报告；程序不会修补或推测输入数据。
