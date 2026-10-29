# 普通表执行器入口

仅普通表采集、提交或投影时读取；身份、完整性和回执门槛按[主契约](dingtalk-mcp.md)，异常按[恢复页](dingtalk-mcp-recovery.md)。

### 固定普通表执行器

每轮首次主代理加载packing-order的scripts/sheet_mcp_submit.js，随后复用。只注入已连接官方工具和本地文件工具；子代理不注入正式写入工具。在functions.exec中，skillFile为实际绝对路径：

```javascript
const r = await tools.mcp__node_repl__js({
  code: `var fs=await import('node:fs/promises');nodeRepl.write(JSON.stringify(await fs.readFile(${JSON.stringify(skillFile)},'utf8')));`,
  title: '加载钉钉执行器'
});
if (r.isError) throw Error('执行器加载失败');
store('packing-sheet-helper-source',
  JSON.parse(r.content.filter(x=>x.type==='text').map(x=>x.text).join('\n')));
```

采集指定表：

```javascript
const api = Function(load('packing-sheet-helper-source')+';return packingSheetSubmit;')();
text(await api.collectWithTools({tools,workDir,rawPath,targetUrl,nodeId,sheets}));
```

sheets为真实{name,sheet_id}列表。各表可并行读取，单表先元数据后完整CSV；全部完成才汇总。小结果一次落盘，大结果按完整工具代码UTF8不超过48KiB分块，顺序写独占.partial后独占发布；已有final/partial拒绝覆盖，失败保留partial且不自动重试；异常时读[落盘恢复](dingtalk-mcp-recovery.md#普通表采集与提交)。采集成功仍必须运行sheet_snapshot.py，snapshot_validation_required不表示验证通过。

现有格匹配提交：

```javascript
const api = Function(load('packing-sheet-helper-source')+';return packingSheetSubmit;')();
const r = await api.runWithTools({tools,workDir,batchPath,specPath,checkPath,outputDir,
  progress: v=>notify(v)});
text({ok:r.ok,submitted_cells:r.submitted.reduce((n,s)=>n+s.cells.length,0),
  failure:r.failure,receipt_file:outputDir+'/receipt.json'});
```

outputDir位于本轮工作目录且父目录已存在。执行器核对batch/spec/check/快照哈希和完整逐格规范；一次独占保存包含全部范围的started，再串行update_range，只组合连续真实变更格，单请求最多1000行，不用旧值填充矩形空隙。已为after的格凭同次check-before的already_present跳过。

正常完成或首错时批量落实际结果、耗时和总回执；已有started或非空结果目录阻止整批重放。若最终receipt保存前中断，started中的计划范围均属可能受影响范围，不能因缺receipt推断未写入。异常按[钉钉异常恢复](dingtalk-mcp-recovery.md)。

工单回填成功、需要继续领料时：

```text
python scripts/sheet_snapshot_project.py --snapshot 本轮当前快照.json --batch 本轮batch.json --spec 本轮submit-spec.json --receipt 本轮receipt.json --out 新派生快照.json
```

助手验证完整身份、计划与实际覆盖，只投影明确成功格，不覆盖原文件，不把部分成功/unknown视为全部成功。零变更沿用原快照。AI契约与追加行不交给该助手。
