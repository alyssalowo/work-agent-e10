/* Run in the primary agent's tool orchestrator with connected MCP callbacks.
 * No browser, network client, credentials, filesystem or automatic retries. */
var packingSheetSubmit = (() => {
  const need = (ok, message) => { if (!ok) throw new Error(message); };
  const colNumber = s => [...s].reduce((n, c) => n * 26 + c.charCodeAt(0) - 64, 0);
  const letters = n => { let s = ''; while (n) { n--; s = String.fromCharCode(n % 26 + 65) + s; n = Math.floor(n / 26); } return s; };
  const cell = s => { const m = /^([A-Z]+)([1-9]\d*)$/.exec(s); need(m, 'Invalid A1 cell'); return {col: colNumber(m[1]), row: Number(m[2])}; };
  const key = (sheet, address) => JSON.stringify([sheet, address]);
  const tuple = c => JSON.stringify([c.cell, c.field, c.before, c.after]);
  function buildRequests({batch, spec, check}, maxRows = 1000) {
    need(Number.isInteger(maxRows) && maxRows >= 1 && maxRows <= 1000, 'Invalid row limit');
    for (const item of [batch, spec, check]) {
      need(item && item.schema_version !== 0 && item.transport === 'dingtalk_sheet', 'Ordinary-sheet evidence required');
      need(item.batch_id === batch.batch_id && (item.kind === undefined || item.kind === batch.kind), 'Batch kind mismatch');
      need(item.batch_id === batch.batch_id && item.node_id === batch.node_id && item.target_url === batch.target_url, 'Document/batch mismatch');
    }
    need(batch.schema_version === 1 && spec.schema_version === 1 && ['workorder', 'picking'].includes(batch.kind), 'Unsupported plan');
    need(spec.phase === 'submit' && check.phase === 'check-before' && check.ok === true, 'Successful prewrite check required');
    need(batch.sheets.length === spec.sheets.length && check.sheets.length === batch.sheets.length, 'Sheet scope mismatch');
    const history = [...(spec.skipped_history || [])];
    for (const proof of check.sheets) for (const update of proof.accepted_updates || []) {
      if (update.reason === 'marker_history_preserved' && !history.some(x => x.sheet === proof.sheet && x.cell === update.cell))
        history.push({sheet:proof.sheet, cell:update.cell});
    }
    const skipped = new Set(history.map(x => key(x.sheet, x.cell)));
    need(skipped.size === history.length, 'Duplicate history skip');
    const requests = [], already = [], coverage = new Set(), known = new Set();
    for (let i = 0; i < batch.sheets.length; i++) {
      const original = batch.sheets[i], sheet = spec.sheets[i], proof = check.sheets[i];
      for (const entry of [sheet, proof]) {
        need((entry.name || entry.sheet) === original.name && entry.sheet_id === original.sheet_id && entry.range === original.range, 'Sheet identity changed');
        need(entry.node_id === batch.node_id, 'Sheet node mismatch');
      }
      need(proof.ok === true && proof.difference_count === 0, 'Protected fields changed');
      const range = /^A1:([A-Z]+)([1-9]\d*)$/.exec(sheet.range);
      need(range, 'Complete physical range required');
      const originals = new Map();
      for (const c of original.changes) {
        const k = key(sheet.name, c.cell);
        need(!known.has(k), 'Duplicate planned cell'); known.add(k);
        originals.set(c.cell, c);
        if (skipped.has(k)) need(batch.kind === 'picking' && c.field === '是否加急', 'Only marker history can skip changes');
      }
      const present = new Set(proof.already_present || []), rows = new Map(), seenSpec = new Set();
      need(present.size === (proof.already_present || []).length && (!present.size || batch.kind === 'picking'), 'Invalid already-present evidence');
      for (const c of sheet.changes) {
        const k = key(sheet.name, c.cell), pos = cell(c.cell);
        need(originals.has(c.cell) && tuple(c) === tuple(originals.get(c.cell)), 'Submission differs from audited plan');
        need(!seenSpec.has(k), 'Duplicate submitted change'); seenSpec.add(k);
        need(pos.row > 1 && pos.row <= Number(range[2]) && pos.col <= colNumber(range[1]), 'Cell outside business scope');
        need(typeof c.after === 'string' && c.after.length > 0 && c.after !== c.before && c.value === c.after, 'Only real nonempty string changes allowed');
        need(!(batch.exclusions?.rows || []).some(x => x.sheet === sheet.name && x.row === pos.row), 'Excluded row cannot be written');
        if (skipped.has(k)) continue;
        coverage.add(k);
        if (present.has(c.cell)) { already.push({sheet: sheet.name, cell: c.cell}); continue; }
        if (!rows.has(pos.row)) rows.set(pos.row, []);
        rows.get(pos.row).push({...c, ...pos});
      }
      for (const address of present) need(originals.has(address), 'Already-present cell is not an audited change');
      const open = new Map();
      for (const [r, changes] of [...rows].sort((a,b) => a[0]-b[0])) {
        changes.sort((a,b) => a.col-b.col);
        const runs = []; let run;
        for (const c of changes) {
          if (!run || run.last + 1 !== c.col) { run = {first:c.col, last:c.col, changes:[]}; runs.push(run); }
          run.last = c.col; run.changes.push(c);
        }
        for (const part of runs) {
          const id = part.first + ':' + part.last; let group = open.get(id);
          if (!group || group.lastRow + 1 !== r || group.request.values.length === maxRows) {
            group = {sheet:sheet.name, firstRow:r, lastRow:r, firstCol:part.first, lastCol:part.last,
              cells:[], request:{nodeId:batch.node_id, sheetId:sheet.sheet_id, rangeAddress:'', values:[]}};
            requests.push(group); open.set(id, group);
          }
          group.lastRow = r;
          group.cells.push(...part.changes.map(c => c.cell));
          group.request.values.push(part.changes.map(c => c.after));
          group.request.rangeAddress = `${letters(part.first)}${group.firstRow}:${letters(part.last)}${r}`;
        }
      }
    }
    for (const k of skipped) need(known.has(k) && !coverage.has(k), 'Invalid history skip');
    need(known.size === coverage.size + skipped.size, 'Incomplete audited change coverage');
    return {requests, already, skipped_history:history, planned_cells:known.size};
  }

  function responseValue(raw) {
    if (raw?.isError) return {success:false, tool_error:true};
    if (raw?.structuredContent) return raw.structuredContent;
    if (raw?.success !== undefined) return raw;
    const text = raw?.content?.find(x => x.type === 'text')?.text;
    try { return JSON.parse(text); } catch (_) { return {success:false, unknown:true}; }
  }

  const RAW_CODE_BUDGET=48*1024;
  // functions.exec is a bare V8 isolate: no Buffer or TextEncoder globals.
  const utf8Bytes=value=>{let bytes=0;for(const point of value){const n=point.codePointAt(0);bytes+=n<=0x7f?1:n<=0x7ff?2:n<=0xffff?3:4;}return bytes;};
  function rawAppendCode(partialPath,destinationPath,chunk,first,publish=false) {
    return `var cf=await import('node:fs/promises');var cp=await import('node:path');var partial=cp.resolve(${JSON.stringify(partialPath)}),dest=cp.resolve(${JSON.stringify(destinationPath)}),chunk=${JSON.stringify(chunk)};
      if(${first})await cf.writeFile(partial,chunk,{encoding:'utf8',flag:'wx'});else await cf.appendFile(partial,chunk,{encoding:'utf8'});${publish?'await cf.link(partial,dest);await cf.unlink(partial);':''}`;
  }
  function rawAppendChunks(data,partialPath,destinationPath,budget=RAW_CODE_BUDGET) {
    need(Number.isInteger(budget)&&budget>0&&budget<=RAW_CODE_BUDGET,'Invalid raw persistence code budget');
    const points=Array.from(data),chunks=[];let offset=0,first=true;
    while(offset<points.length) {
      let low=offset+1,high=points.length,best=offset;
      while(low<=high) {
        const mid=Math.floor((low+high)/2),candidate=points.slice(offset,mid).join('');
        if(utf8Bytes(rawAppendCode(partialPath,destinationPath,candidate,first))<=budget) {best=mid;low=mid+1;}
        else high=mid-1;
      }
      need(best>offset,'Raw value cannot fit persistence code budget');
      const chunk=points.slice(offset,best).join(''),code=rawAppendCode(partialPath,destinationPath,chunk,first);
      need(utf8Bytes(code)<=budget,'Raw persistence code exceeds byte budget');
      chunks.push(code);offset=best;first=false;
    }
    if(!chunks.length)chunks.push(rawAppendCode(partialPath,destinationPath,'',true));
    return chunks;
  }
  async function persistRawWithTools({tools,rawPath,data}) {
    const partialPath=rawPath+'.partial';
    const single=rawAppendCode(partialPath,rawPath,data,true,true);
    if(utf8Bytes(single)<=RAW_CODE_BUDGET) {
      const saved=await tools.mcp__node_repl__js({code:single,title:'保存并独占发布完整原始采集'});
      need(!saved.isError,'Raw persistence failed; retain the partial file and stop dependent work');
      return;
    }
    const chunks=rawAppendChunks(data,partialPath,rawPath);
    for(let i=0;i<chunks.length;i++) {
      const saved=await tools.mcp__node_repl__js({code:chunks[i],title:`落盘原始采集分块 ${i+1}/${chunks.length}`});
      need(!saved.isError,'Raw persistence failed; retain the partial file and stop dependent work');
    }
    const code=`var cf=await import('node:fs/promises');var cp=await import('node:path');var partial=cp.resolve(${JSON.stringify(partialPath)}),dest=cp.resolve(${JSON.stringify(rawPath)});await cf.link(partial,dest);await cf.unlink(partial);`;
    need(utf8Bytes(code)<=RAW_CODE_BUDGET,'Raw publish code exceeds byte budget');
    const published=await tools.mcp__node_repl__js({code,title:'独占发布完整原始采集文件'});
    need(!published.isError,'Raw publish failed; retain the complete partial file and stop dependent work');
  }

  async function run(input, io) {
    need(typeof io?.updateRange === 'function' && (typeof io?.persistNew === 'function' || typeof io?.persistBatch === 'function'), 'Primary-agent MCP and exclusive persistence callbacks required');
    const built = buildRequests(input), spec = input.spec;
    const receipt = {schema_version:1, transport:'dingtalk_sheet', phase:'submit', kind:spec.kind,
      batch_id:spec.batch_id, node_id:input.batch.node_id, target_url:spec.target_url, ok:false, submitted:[],
      check_basis:input.check.check_basis || 'legacy_snapshot_check',
      evidence:input.evidence || null,
      already_present:built.already, skipped_history:built.skipped_history,
      failure:null, persistence_verified:false};
    const startedUtc=new Date().toISOString();
    const started={schema_version:1,batch_id:spec.batch_id,phase:'started',created_utc:startedUtc,
      ranges:built.requests.map((g,i)=>({index:i+1,sheet:g.sheet,range:g.request.rangeAddress,request:g.request,cells:g.cells}))};
    if(io.persistBatch) await io.persistBatch({phase:'started',started});
    else await io.persistNew('batch-started.json',started);
    const results=[];
    const persistFinal=async()=>{
      if(io.persistBatch) return io.persistBatch({phase:'final',started,results,receipt});
      for(const row of results) await io.persistNew(`write-${String(row.index).padStart(4,'0')}-result.json`,row);
      await io.persistNew('receipt.json',receipt);
    };
    for (let i=0; i<built.requests.length; i++) {
      const group = built.requests[i], stem = `write-${String(i+1).padStart(4,'0')}`;
      const started = Date.now(); let raw;
      try { raw = await io.updateRange(group.request); }
      catch (error) {
        receipt.failure = {sheet:group.sheet, range:group.request.rangeAddress, state:'unknown',
          error:String(error.message || error), note:'Inspect only the affected range; do not replay.'};
        results.push({index:i+1,sheet:group.sheet,range:group.request.rangeAddress,state:'unknown',
          error:receipt.failure.error,started_utc:new Date(started).toISOString(),returned_utc:new Date().toISOString(),elapsed_seconds:(Date.now()-started)/1000});
        await persistFinal();
        return receipt;
      }
      const resultRow={index:i+1,sheet:group.sheet,range:group.request.rangeAddress,result:raw,started_utc:new Date(started).toISOString(),
        returned_utc:new Date().toISOString(),elapsed_seconds:(Date.now()-started)/1000};
      results.push(resultRow);
      const value = responseValue(raw);
      if (value.success !== true) {
        receipt.failure = {sheet:group.sheet, range:group.request.rangeAddress,
          state:value.unknown ? 'unknown' : 'failed', result:value};
        resultRow.state=receipt.failure.state;
        await persistFinal();
        return receipt;
      }
      let accepted = receipt.submitted.find(x => x.sheet === group.sheet);
      if (!accepted) { accepted={sheet:group.sheet, cells:[]}; receipt.submitted.push(accepted); }
      accepted.cells.push(...group.cells);
      if (io.progress) { try { await io.progress({phase:'sheet_submit', completed_ranges:i+1,
        total_ranges:built.requests.length, submitted_cells:receipt.submitted.reduce((n,x)=>n+x.cells.length,0)}); } catch (_) {} }
    }
    receipt.ok = true;
    await persistFinal();
    return receipt;
  }
  async function runWithTools({tools, workDir, batchPath, specPath, checkPath, outputDir, progress}) {
    // The model loads this trusted local helper once. All online calls stay in
    // the primary agent's connected tools; no direct HTTP or secondary bridge.
    const node = async (code, title) => {
      const raw = await tools.mcp__node_repl__js({code, title});
      need(!raw.isError, 'Local artifact operation failed; do not replay writes');
      return raw.content.filter(x => x.type === 'text').map(x => x.text).join('\n');
    };
    const paths = {workDir, batchPath, specPath, checkPath, outputDir};
    const input = JSON.parse(await node(`
      var pf = await import('node:fs/promises'); var pp = await import('node:path'); var pc = await import('node:crypto');
      var paths = ${JSON.stringify(paths)};
      var root = await pf.realpath(paths.workDir);
      for (var label of ['batchPath','specPath','checkPath']) {
        paths[label] = await pf.realpath(paths[label]);
        var rel = pp.relative(root,paths[label]);
        if (!rel || rel.startsWith('..') || pp.isAbsolute(rel)) throw Error('Evidence outside current work directory');
      }
      paths.outputDir=pp.resolve(paths.outputDir);
      var parent=await pf.realpath(pp.dirname(paths.outputDir)), parentRel=pp.relative(root,parent);
      if(parentRel.startsWith('..') || pp.isAbsolute(parentRel)) throw Error('Output parent outside current work directory');
      await pf.mkdir(paths.outputDir,{recursive:true}); paths.outputDir=await pf.realpath(paths.outputDir);
      var rel=pp.relative(root,paths.outputDir); if (!rel || rel.startsWith('..') || pp.isAbsolute(rel)) throw Error('Output outside current work directory');
      if((await pf.readdir(paths.outputDir)).length) throw Error('Submission evidence directory is not empty; refuse replay');
      var b=JSON.parse(await pf.readFile(paths.batchPath,'utf8')), s=JSON.parse(await pf.readFile(paths.specPath,'utf8')), c=JSON.parse(await pf.readFile(paths.checkPath,'utf8'));
      if (pc.createHash('sha256').update(await pf.readFile(b.source)).digest('hex')!==b.source_sha256) throw Error('Source changed');
      if(b.exclusions && pc.createHash('sha256').update(await pf.readFile(b.exclusions.path)).digest('hex')!==b.exclusions.sha256) throw Error('Exclusions changed');
      var evidence={};
      for(var label of ['batch','spec','check']) {
        var file=paths[label+'Path'];evidence[label+'_path']=file;
        evidence[label+'_sha256']=pc.createHash('sha256').update(await pf.readFile(file)).digest('hex');
      }
      evidence.snapshot_path=await pf.realpath(c.snapshot_file || b.snapshot_file);
      evidence.snapshot_sha256=pc.createHash('sha256').update(await pf.readFile(evidence.snapshot_path)).digest('hex');
      if(c.snapshot_sha256 && c.snapshot_sha256!==evidence.snapshot_sha256) throw Error('Checked snapshot changed');
      nodeRepl.write(JSON.stringify({outputDir:paths.outputDir,evidence,batch:{schema_version:b.schema_version,batch_id:b.batch_id,kind:b.kind,transport:b.transport,target_url:b.target_url,node_id:b.node_id,exclusions:b.exclusions,
        sheets:b.sheets.map(x=>({name:x.name,sheet_id:x.sheet_id,range:x.range,changes:x.changes}))},spec:s,check:c}));`, '加载完整计划与写前检查并核对源哈希'));
    const persistBatch = async payload => {
      const files=payload.phase==='started' ? {'batch-started.json':payload.started} : {
        ...Object.fromEntries(payload.results.map(row=>[`write-${String(row.index).padStart(4,'0')}-result.json`,row])),
        'receipt.json':payload.receipt
      };
      const code=`var pf=await import('node:fs/promises');var pp=await import('node:path');var dir=${JSON.stringify(input.outputDir)};
        var files=${JSON.stringify(files)};for(var [name,value] of Object.entries(files)){var dest=pp.join(dir,name);await pf.writeFile(dest,JSON.stringify(value,null,2),{encoding:'utf8',flag:'wx'});}`;
      await node(code,payload.phase==='started'?'独占创建完整批次started清单':'一次保存批次真实结果与回执');
    };
    return run(input, {updateRange: request => tools.mcp__dingtalk_sheet__update_range(request), persistBatch, progress});
  }
  async function collectWithTools({tools,workDir,rawPath,targetUrl,nodeId,sheets,maxChars=200000}) {
    need(Array.isArray(sheets) && sheets.length && new Set(sheets.map(s=>s.name)).size===sheets.length, 'Explicit unique sheet scope required');
    need(new Set(sheets.map(s=>s.sheet_id)).size===sheets.length && sheets.every(s=>s.name && s.sheet_id), 'Real unique sheet IDs required');
    const urlNode=/\/i\/nodes\/([^/?#]+)/.exec(targetUrl || '');
    need(urlNode && decodeURIComponent(urlNode[1])===nodeId, 'Collection URL/node identity mismatch');
    const outputCheck=await tools.mcp__node_repl__js({code:`var cf=await import('node:fs/promises');var cp=await import('node:path');
      var root=await cf.realpath(${JSON.stringify(workDir)}),dest=cp.resolve(${JSON.stringify(rawPath)});
      var rel=cp.relative(root,await cf.realpath(cp.dirname(dest)));
      if(rel.startsWith('..')||cp.isAbsolute(rel))throw Error('Raw output outside current work directory');
      try{await cf.lstat(dest);throw Error('Raw output already exists');}catch(e){if(e.code!=='ENOENT')throw e;}
      try{await cf.lstat(dest+'.partial');throw Error('Raw partial output already exists');}catch(e){if(e.code!=='ENOENT')throw e;}`,title:'采集前检查原始文件路径'});
    need(!outputCheck.isError,'Raw output preflight failed; no online read was started');
    const started=Date.now(), raw={target_url:targetUrl,captured_at:new Date().toISOString(),sheets:[]};
    const results=await Promise.allSettled(sheets.map(async s=>{
      const tick=Date.now();
      const metadata=responseValue(await tools.mcp__dingtalk_sheet__get_sheet({nodeId,sheetId:s.sheet_id}));
      need(metadata.success===true && metadata.name===s.name && metadata.id===s.sheet_id,'Sheet metadata identity mismatch');
      const range=metadata.nonEmptyRange?.range;
      need(/^A1:[A-Z]+[1-9]\d*$/.test(range||''),'Explicit nonempty A1 metadata range required');
      const response=responseValue(await tools.mcp__dingtalk_sheet__get_range_as_csv({nodeId,sheetId:s.sheet_id,range,annotateRowNumbers:false,maxChars}));
      return {name:s.name,sheet_id:s.sheet_id,metadata,response,elapsed_seconds:(Date.now()-tick)/1000};
    }));
    const failures=[];
    results.forEach((result,i)=>{
      if(result.status==='fulfilled') {
        const row=result.value;raw.sheets.push(row);
        if(row.response.success!==true || row.response.hasMore!==false || row.response.returnedRange!==row.metadata.nonEmptyRange.range)
          failures.push({sheet:row.name,reason:'CSV incomplete, failed or range changed'});
      } else failures.push({sheet:sheets[i].name,reason:String(result.reason?.message||result.reason)});
    });
    raw.collection_failures=failures;
    const data=JSON.stringify(raw);
    await persistRawWithTools({tools,rawPath,data});
    return {ok:failures.length===0,phase:'sheet_collect',raw_file:rawPath,
      elapsed_seconds:(Date.now()-started)/1000,failures,
      sheets:raw.sheets.map(s=>({name:s.name,sheet_id:s.sheet_id,range:s.metadata.nonEmptyRange.range})),
      snapshot_validation_required:true};
  }
  return {buildRequests, run, runWithTools, collectWithTools, persistRawWithTools};
})();
if (typeof module === 'object') module.exports = packingSheetSubmit;
