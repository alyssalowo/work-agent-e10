// Evaluate this trusted skill file in functions.exec; pass the current Node REPL tool callback.
async (toolResult, resultPath, runNode) => {
  if (!resultPath || typeof resultPath !== 'string' || !/^(?:[A-Za-z]:[\\/]|\/)/.test(resultPath))
    throw new Error('结果文件须为本轮工作目录的绝对路径');
  if (toolResult?.isError) throw new Error('浏览器工具失败，未保存业务结果');
  let packet = toolResult;
  if (Array.isArray(toolResult?.content)) {
    const raw = toolResult.content.filter(c=>c.type==='text').map(c=>c.text).join('\n');
    const match = raw.match(/### Result\n([\s\S]*?)(?=\n### |$)/);
    packet = JSON.parse(match ? match[1] : raw);
  }
  const compressed = packet?.transport === 'gzip-base64';
  if (compressed) {
    if (packet.packet_version !== 1 || typeof packet.payload !== 'string' || !packet.payload.length
      || packet.payload.length % 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(packet.payload)
      || !Number.isSafeInteger(packet.utf8_bytes) || packet.utf8_bytes < 1) throw new Error('压缩结果包不完整');
  } else if (packet?.schema_version !== 1 || !['collect','submit'].includes(packet.phase) || typeof packet.ok !== 'boolean')
    throw new Error('结果不是已支持的业务回执');
  const summary = compressed ? packet.summary : packet;
  if (!summary || !['collect','submit'].includes(summary.phase) || typeof summary.ok !== 'boolean')
    throw new Error('业务结果摘要不完整');
  const temp = resultPath + '.transfer-' + Date.now() + '-' + Math.random().toString(16).slice(2);
  const body = compressed ? packet.payload : JSON.stringify(packet);
  let chunks = 0;
  for (let i=0; i<body.length;) {
    let end = Math.min(i + (compressed ? 40000 : 8000), body.length);
    if (!compressed && end<body.length && /[\uD800-\uDBFF]/.test(body[end-1])) end--;
    const value = JSON.stringify(body.slice(i,end));
    const code = "var packagingTransferFs=await import('node:fs/promises'); await packagingTransferFs." +
      (i===0 ? 'writeFile(' : 'appendFile(') + JSON.stringify(temp) + ', ' +
      (compressed ? "Buffer.from("+value+",'base64')" : value+", 'utf8'") +
      '); nodeRepl.write({partSaved:true});';
    const result = await runNode({code,title:'保存本轮读取结果'});
    if (result?.isError) throw new Error('结果分块传递失败；保留原结果文件，未继续规划或重放提交');
    chunks++; i=end;
  }
  const code = "var packagingTransferFs=await import('node:fs/promises'); var packagingTransferData=await packagingTransferFs.readFile("+
    JSON.stringify(temp)+");" + (compressed ? "var packagingTransferZlib=await import('node:zlib'); packagingTransferData=packagingTransferZlib.gunzipSync(packagingTransferData);" : '') +
    (compressed ? 'if(packagingTransferData.length!=='+packet.utf8_bytes+")throw Error('解压长度不符');" : '') +
    "var packagingTransferValue=JSON.parse(packagingTransferData.toString('utf8'));" +
    'if(packagingTransferValue.schema_version!==1 || packagingTransferValue.phase!=='+JSON.stringify(summary.phase)+
    ' || packagingTransferValue.batch_id!=='+JSON.stringify(summary.batch_id)+
    ' || packagingTransferValue.target_url!=='+JSON.stringify(summary.target_url)+
    ' || packagingTransferValue.ok!=='+JSON.stringify(summary.ok)+")throw Error('业务结果与摘要不符');" +
    "await packagingTransferFs.writeFile("+JSON.stringify(temp+'.json')+",packagingTransferData); await packagingTransferFs.rename("+
    JSON.stringify(temp+'.json')+','+JSON.stringify(resultPath)+"); await packagingTransferFs.unlink("+JSON.stringify(temp)+"); nodeRepl.write({saved:true,phase:packagingTransferValue.phase,ok:packagingTransferValue.ok});";
  const saved = await runNode({code,title:'校验并保存完整业务结果'});
  if (saved?.isError) throw new Error('完整结果校验或保存失败；保留原结果文件');
  return {saved:true,result_file:resultPath,chunks,compressed,phase:summary.phase,ok:summary.ok,
    batch_id:summary.batch_id,sheets:summary.sheets?.map(s=>({name:s.name,range:s.range})),failure:summary.failure};
}
