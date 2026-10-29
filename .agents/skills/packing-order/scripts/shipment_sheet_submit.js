/* Pure request builder and serial executor for typed shipment-sheet appends.
 * No network, filesystem, credentials, MCP bridge, or retry behavior. */
var shipmentSheetSubmit = (() => {
  const need = (condition, message) => { if (!condition) throw new Error(message); };
  const DATE_HEADER = '预计发货日期';
  const QTY_HEADERS = ['销货明细_业务数量','借出明细_业务数量'];
  const MAX_CELLS = 30000, MAX_CSV_CHARS = 2_000_000, MAX_ROWS_PER_TYPED_CALL = 1000;
  const colNumber = s => [...s].reduce((n,c) => n * 26 + c.charCodeAt(0) - 64, 0);
  const letters = n => { let s=''; while(n){n--;s=String.fromCharCode(n%26+65)+s;n=Math.floor(n/26);} return s; };
  function parseRange(range) {
    const m = /^A([1-9]\d*):([A-Z]+)([1-9]\d*)$/.exec(range || '');
    need(m, 'Append range must be a full rectangular A1 range starting in column A');
    return {firstRow:Number(m[1]), lastCol:colNumber(m[2]), lastRow:Number(m[3]), lastColName:m[2]};
  }
  function csvField(value) {
    const s=String(value);
    return /[",\r\n]/.test(s) ? `"${s.replace(/"/g,'""')}"` : s;
  }
  function csv(rows) { return rows.map(row => row.map(csvField).join(',')).join('\r\n'); }
  function validateDate(s) {
    need(/^\d{4}-\d{2}-\d{2}$/.test(s), `Expected date must be YYYY-MM-DD: ${s}`);
    const [y,m,d]=s.split('-').map(Number), date=new Date(Date.UTC(y,m-1,d));
    need(date.getUTCFullYear()===y && date.getUTCMonth()===m-1 && date.getUTCDate()===d, `Invalid calendar date: ${s}`);
    need(date.getTime() >= Date.UTC(1900,2,1), `Date must be after 1900-02-28: ${s}`);
    return String((date.getTime()-Date.UTC(1899,11,30))/86400000);
  }
  function validateQty(s) {
    need(/^-?\d+(?:\.\d+)?$/.test(s), `Quantity must be an ordinary decimal: ${s}`);
    const digits=s.replace(/[-.]/g,'').replace(/^0+/, '');
    need((digits || '0').length <= 15, `Quantity exceeds 15 significant digits: ${s}`);
    const n=Number(s);
    need(Number.isFinite(n) && Math.abs(n)<=Number.MAX_SAFE_INTEGER, `Quantity outside safe numeric range: ${s}`);
    return s;
  }
  function typeHeaderMap(typeColumns, headers) {
    need(typeColumns && typeof typeColumns==='object' && !Array.isArray(typeColumns), 'type_columns must be an object');
    const keys=Object.keys(typeColumns).sort();
    const quantityHeaders=headers.filter(h=>QTY_HEADERS.includes(h));
    need(quantityHeaders.length===1, 'Exactly one real business quantity header required');
    need(JSON.stringify(keys)===JSON.stringify([DATE_HEADER,quantityHeaders[0]].sort()), 'type_columns must declare exactly the date and real quantity headers');
    const map={};
    for(const [header,definition] of Object.entries(typeColumns)) {
      need(headers.includes(header), `Typed header missing from request headers: ${header}`);
      need(definition && typeof definition==='object', `Missing explicit type declaration for ${header}`);
      if(header===DATE_HEADER) need(definition.type==='date' && definition.format==='yyyy-mm-dd', 'Expected shipment date must declare type=date and format=yyyy-mm-dd');
      else need(definition.type==='number' && definition.format==='General', 'Business quantity must declare type=number and format=General');
      map[header]=definition;
    }
    return map;
  }
  function buildRequests(plan) {
    need(plan && plan.schema_version===1 && plan.phase==='append', 'Unsupported shipment append plan');
    need(typeof plan.node_id==='string' && plan.node_id.length>0 && typeof plan.target_url==='string' && plan.target_url.length>0, 'Plan must bind node and target URL');
    const urlNode=/\/nodes\/([^/?#]+)/.exec(plan.target_url);
    need(urlNode && decodeURIComponent(urlNode[1])===plan.node_id, 'Target URL and node_id do not match');
    need(typeof plan.check_basis==='string' && plan.check_basis.length>0, 'Plan must retain check_basis');
    need((plan.initial_complete_snapshot && typeof plan.initial_complete_snapshot==='object') || (typeof plan.snapshot_path==='string' && plan.snapshot_path.length>0), 'Complete initial snapshot binding required');
    const snapshotComplete=plan.initial_complete_snapshot?.complete===true || plan.initial_complete_snapshot?.scope_complete===true || plan.requests?.length>0&&plan.requests.every(x=>x.before_scope_complete===true);
    need(snapshotComplete, 'Initial snapshot must be explicitly complete');
    const sourceHash=plan.source_sha256 || plan.source_plan_sha256 || plan.source?.sha256 || plan.initial_complete_snapshot?.source_sha256;
    const snapshotHash=plan.snapshot_sha256 || plan.initial_complete_snapshot.snapshot_sha256 || plan.initial_complete_snapshot.sha256;
    need(typeof sourceHash==='string' && sourceHash.length>0 && typeof snapshotHash==='string' && snapshotHash.length>0, 'Source and snapshot SHA strings must remain bound in the plan');
    need(Array.isArray(plan.requests) && plan.requests.length>0, 'At least one sheet request is required');
    const sheetNames=new Set(),sheetIds=new Set(),rangesBySheet=new Map(),operations=[];
    let cellCount=0;
    for(const item of plan.requests) {
      need(item && typeof item.sheet==='string' && item.sheet && typeof item.sheet_id==='string' && item.sheet_id, 'Sheet name and real sheet_id required');
      need(!sheetNames.has(item.sheet) && !sheetIds.has(item.sheet_id), 'Duplicate sheet or sheet_id');
      sheetNames.add(item.sheet);sheetIds.add(item.sheet_id);
      need(item.before_scope_complete===true && Number.isInteger(item.before_row_count) && item.before_row_count>=1, 'Append requires a complete before-scope and row count');
      need(Array.isArray(item.headers) && item.headers.length>0 && item.headers.every(h=>typeof h==='string' && h.length>0), 'Unique real headers required');
      need(new Set(item.headers).size===item.headers.length, 'Duplicate header names are not allowed');
      const typed=typeHeaderMap(item.type_columns,item.headers), range=parseRange(item.range);
      need(range.firstRow===item.before_row_count+1, 'Append range must start immediately after before_row_count');
      need(Array.isArray(item.values) && item.values.length>0 && range.lastRow-range.firstRow+1===item.values.length, 'Range row count must exactly match values');
      need(range.lastCol===item.headers.length, 'Range width must exactly match unique headers');
      need(item.values.every(row=>Array.isArray(row) && row.length===item.headers.length && row.every(v=>typeof v==='string')), 'Every value must be a string in a full rectangular row');
      cellCount+=item.values.length*item.headers.length;
      need(cellCount<=MAX_CELLS, 'Plan exceeds 30000 cells');
      let ranges=rangesBySheet.get(item.sheet)||[];
      need(!ranges.some(r=>!(range.lastRow<r.firstRow || range.firstRow>r.lastRow)), 'Overlapping ranges for the same sheet');
      ranges.push(range);rangesBySheet.set(item.sheet,ranges);
      for(const row of item.values) for(const value of row) need(!value.startsWith('='), 'Values beginning with = are refused');

      // Initial content is all text, chunked only at whole-row boundaries.
      let offset=0;
      while(offset<item.values.length) {
        const rows=[], firstRowCsv=csv([item.values[offset]]);
        let end=offset+1, text=firstRowCsv;
        need(text.length<=MAX_CSV_CHARS, 'One full row exceeds 2M CSV characters');
        while(end<item.values.length) {
          const next=csv([item.values[end]]), candidateLength=text.length+2+next.length;
          if(candidateLength>MAX_CSV_CHARS) break;
          text+='\r\n'+next;end++;
        }
        const first=range.firstRow+offset,last=range.firstRow+end-1;
        for(let i=offset;i<end;i++) rows.push(item.values[i]);
        operations.push({kind:'text', sheet:item.sheet, sheet_id:item.sheet_id, range:`A${first}:${range.lastColName}${last}`,
          headers:item.headers, values:rows, csv:text, autoConvert:false, allowOverwrite:false,
          nodeId:plan.node_id, targetUrl:plan.target_url});
        offset=end;
      }

      for(const header of Object.keys(typed)) {
        const col=item.headers.indexOf(header)+1, type=typed[header], indices=[];
        for(let i=0;i<item.values.length;i++) if(item.values[i][col-1]!=='' ) indices.push(i);
        for(const i of indices) {
          const value=item.values[i][col-1];
          if(header===DATE_HEADER) validateDate(value); else validateQty(value);
        }
        let p=0;
        while(p<indices.length) {
          let q=p+1;
          while(q<indices.length && indices[q]===indices[q-1]+1 && q-p<MAX_ROWS_PER_TYPED_CALL) q++;
          const firstIndex=indices[p],lastIndex=indices[q-1], firstRow=range.firstRow+firstIndex,lastRow=range.firstRow+lastIndex;
          const vals=[];
          for(let i=firstIndex;i<=lastIndex;i++) vals.push([header===DATE_HEADER?validateDate(item.values[i][col-1]):validateQty(item.values[i][col-1])]);
          const colName=letters(col);
          operations.push({kind:'typed_values',type:type.type,sheet:item.sheet,sheet_id:item.sheet_id,header,
            range:`${colName}${firstRow}:${colName}${lastRow}`,headers:[header],values:vals,csv:csv(vals),
            autoConvert:true,allowOverwrite:true,nodeId:plan.node_id,targetUrl:plan.target_url});
          p=q;
        }
        // Format only newly appended cells in this column, without values.
        let start=0;
        while(start<item.values.length) {
          const end=Math.min(item.values.length,start+MAX_ROWS_PER_TYPED_CALL),colName=letters(col);
          operations.push({kind:'format',type:type.type,sheet:item.sheet,sheet_id:item.sheet_id,header,
            range:`${colName}${range.firstRow+start}:${colName}${range.firstRow+end-1}`,
            numberFormat:type.format,nodeId:plan.node_id,targetUrl:plan.target_url});
          start=end;
        }
      }
    }
    // All input is validated above before any caller can write. Execute text for every sheet,
    // then date/quantity conversions, then number formats.
    const rank={text:0,typed_values:1,format:2};
    operations.sort((a,b)=>rank[a.kind]-rank[b.kind]);
    const coverage=plan.requests.map(item=>{
      const sheetOps=operations.filter(op=>op.sheet===item.sheet);
      return {sheet:item.sheet,sheet_id:item.sheet_id,range:item.range,rows:item.values.length,
        expected_operations:sheetOps.length,expected_text_operations:sheetOps.filter(op=>op.kind==='text').length,
        expected_typed_value_operations:sheetOps.filter(op=>op.kind==='typed_values').length,
        expected_format_operations:sheetOps.filter(op=>op.kind==='format').length,
        typed_cells:item.values.reduce((n,row)=>n+[DATE_HEADER,...QTY_HEADERS.filter(h=>item.headers.includes(h))].filter(h=>row[item.headers.indexOf(h)]!=='').length,0),
        successful_operations:0,successful_text_operations:0,successful_typed_value_operations:0,successful_format_operations:0,
        text_complete:false,typed_values_complete:false,formats_complete:false,complete:false};
    });
    return {operations,cell_count:cellCount,coverage};
  }
  function responseValue(raw) {
    if(raw?.isError) return {success:false,unknown:true,raw};
    if(raw?.structuredContent) return raw.structuredContent;
    if(raw?.success!==undefined) return raw;
    const text=raw?.content?.find(x=>x.type==='text')?.text;
    try{return JSON.parse(text);}catch(_){return {success:false,unknown:true,raw};}
  }
  async function run(plan,io) {
    need(io && typeof io.persistNew==='function' && typeof io.writeCsv==='function' && typeof io.updateFormat==='function', 'persistNew, writeCsv, and updateFormat callbacks required');
    const monotonicNow=()=>typeof globalThis.performance?.now==='function'?globalThis.performance.now():null;
    const elapsedTiming=start=>{
      if(start===null)return {elapsed_seconds:null,timing_clock:'unavailable'};
      const end=monotonicNow();
      return end===null?{elapsed_seconds:null,timing_clock:'unavailable'}:{elapsed_seconds:(end-start)/1000,timing_clock:'monotonic'};
    };
    const persistExclusive=async(name,value)=>{
      const saved=await io.persistNew(name,value);
      need(saved!==false && saved?.isError!==true && saved?.structuredContent?.success!==false && saved?.success!==false, `Exclusive persistence failed: ${name}`);
      return saved;
    };
    const built=buildRequests(plan), started={schema_version:1,phase:'started',created_utc:new Date().toISOString(),plan,
      input_binding:io.inputBinding||null,operations:built.operations};
    // Exclusive create is the replay guard; if it fails, no remote operation is attempted.
    await persistExclusive('started.json',started);
    const typedCells=built.coverage.reduce((n,x)=>n+x.typed_cells,0);
    const receipt={schema_version:1,phase:'append',ok:false,node_id:plan.node_id,target_url:plan.target_url,
      check_basis:plan.check_basis,input_binding:io.inputBinding||null,operation_count:built.operations.length,rows:plan.requests.reduce((n,x)=>n+x.values.length,0),
      typed_cells:typedCells,independent_save_verification:false,completed_operations:0,failure:null,
      operations:[],sheet_coverage:built.coverage.map(x=>({...x}))};
    const persistFailure=async()=>{receipt.failure_evidence_persisted=true;try{await persistExclusive('failure.json',{receipt});}catch(error){receipt.failure_evidence_persisted=false;receipt.failure_evidence_error=String(error?.message||error);}};
    for(let i=0;i<built.operations.length;i++) {
      const op=built.operations[i], index=i+1, file=`operation-${String(index).padStart(6,'0')}.json`; let raw,thrown=null;
      const startedUtc=new Date().toISOString(), tick=monotonicNow();
      try { raw=op.kind==='format'?await io.updateFormat(op):await io.writeCsv(op); }
      catch(error) { thrown=String(error?.message||error); }
      const returnedUtc=new Date().toISOString(), timing=elapsedTiming(tick);
      const operationRecord={operation:index,index,kind:op.kind,sheet:op.sheet,range:op.range,request:op,response:thrown===null?raw:null,
        started_utc:startedUtc,returned_utc:returnedUtc,...timing,...(thrown===null?{}:{error:thrown})};
      try { await persistExclusive(file,operationRecord); }
      catch(error) {
        receipt.failure={operation:index,kind:op.kind,sheet:op.sheet,range:op.range,state:'unknown',
          error:`Operation response could not be durably recorded: ${String(error?.message||error)}`,
          response_received:thrown===null,response_may_have_been_applied:true};
        await persistFailure();return receipt;
      }
      const ref={file,index,kind:op.kind,sheet:op.sheet,range:op.range,success:false};
      receipt.operations.push(ref);
      if(thrown!==null) {
        receipt.failure={operation:index,kind:op.kind,sheet:op.sheet,range:op.range,state:'unknown',error:thrown,response:null};
        await persistFailure();return receipt;
      }
      const result=responseValue(raw);
      if(result.success!==true) {
        receipt.failure={operation:index,kind:op.kind,sheet:op.sheet,range:op.range,
          state:result.unknown?'unknown':'failed',result};
        await persistFailure();return receipt;
      }
      ref.success=true;
      const coverage=receipt.sheet_coverage.find(x=>x.sheet===op.sheet);
      coverage.successful_operations++;
      if(op.kind==='text') coverage.successful_text_operations++;
      else if(op.kind==='typed_values') coverage.successful_typed_value_operations++;
      else if(op.kind==='format') coverage.successful_format_operations++;
      coverage.text_complete=coverage.successful_text_operations===coverage.expected_text_operations;
      coverage.typed_values_complete=coverage.successful_typed_value_operations===coverage.expected_typed_value_operations;
      coverage.formats_complete=coverage.successful_format_operations===coverage.expected_format_operations;
      coverage.complete=coverage.successful_operations===coverage.expected_operations &&
        coverage.text_complete&&coverage.typed_values_complete&&coverage.formats_complete;
      receipt.completed_operations++;
      if(io.progress) { try { await io.progress({completed:receipt.completed_operations,total:built.operations.length,kind:op.kind,sheet:op.sheet,range:op.range}); } catch(_){} }
    }
    receipt.ok=true;
    await persistExclusive('receipt.json',receipt);
    return receipt;
  }
  async function runWithTools({tools,workDir,planPath,outputDir,progress,persistRaw,pythonPath,verifierPath}) {
    need(tools && typeof tools.mcp__node_repl__js==='function' && typeof tools.mcp__dingtalk_sheet__set_range_from_csv==='function' && typeof tools.mcp__dingtalk_sheet__update_range==='function', 'Primary Node REPL and DingTalk sheet callbacks required');
    need(typeof workDir==='string'&&typeof planPath==='string'&&typeof outputDir==='string'&&typeof persistRaw==='function', 'workDir, planPath, outputDir, persistRaw required');
    need(typeof pythonPath==='string' && typeof verifierPath==='string', 'Host-configured Python and fixed source verifier required');
    const paths={workDir,planPath,outputDir,pythonPath,verifierPath};
    const loadCode=`(async()=>{var fs=await import('node:fs/promises'),path=await import('node:path'),crypto=await import('node:crypto');
      var p=${JSON.stringify(paths)},root=await fs.realpath(p.workDir),planFile=await fs.realpath(p.planPath),rel=path.relative(root,planFile);
      if(!rel||rel.startsWith('..')||path.isAbsolute(rel))throw Error('Plan is outside workDir');
      var plan=JSON.parse(await fs.readFile(planFile,'utf8'));
      if(typeof plan.source_plan_path!=='string'||typeof plan.source_plan_sha256!=='string'||typeof plan.snapshot_path!=='string'||typeof plan.snapshot_sha256!=='string')throw Error('Plan source/snapshot path and SHA bindings required');
      for(var pair of [[plan.source_plan_path,plan.source_plan_sha256],[plan.snapshot_path,plan.snapshot_sha256]]){var f=await fs.realpath(pair[0]),r=path.relative(root,f);if(!r||r.startsWith('..')||path.isAbsolute(r))throw Error('Bound source/snapshot outside workDir');var h=crypto.createHash('sha256').update(await fs.readFile(f)).digest('hex');if(h!==pair[1])throw Error('Bound source/snapshot SHA mismatch');}
      if(!path.isAbsolute(p.pythonPath)||!path.isAbsolute(p.verifierPath)||path.basename(p.verifierPath)!=='shipment_append_plan.py')throw Error('Absolute host verifier paths required');
      var child=await import('node:child_process'),util=await import('node:util');
      var verification=await util.promisify(child.execFile)(p.pythonPath,['-X','utf8',p.verifierPath,'--verify-plan',planFile],{windowsHide:true,timeout:60000,maxBuffer:1048576});
      var proof=JSON.parse(verification.stdout),planHash=crypto.createHash('sha256').update(await fs.readFile(planFile)).digest('hex');
      if(proof.ok!==true||proof.plan_sha256!==planHash||JSON.stringify(plan)!==JSON.stringify(JSON.parse(await fs.readFile(planFile,'utf8'))))throw Error('Fresh source verification failed');
      var out=path.resolve(p.outputDir),outParent=path.dirname(out),parentReal=await fs.realpath(outParent),outRel=path.relative(root,parentReal);
      if(outRel.startsWith('..')||path.isAbsolute(outRel))throw Error('Output outside workDir');
      try{await fs.lstat(out);throw Error('Output directory already exists; refuse replay');}catch(e){if(e.code!=='ENOENT')throw e;}
      nodeRepl.write(JSON.stringify({plan,outputDir:out,plan_sha256:crypto.createHash('sha256').update(await fs.readFile(planFile)).digest('hex')}));})()`;
    const loaded=await tools.mcp__node_repl__js({code:loadCode,title:'校验计划与输入哈希并独占创建输出目录'});
    need(!loaded.isError,'Local plan load/preflight failed; no sheet call was made');
    const outputText=(loaded.content||[]).filter(x=>x.type==='text').map(x=>x.text).join('\n');
    let payload;try{payload=JSON.parse(outputText);}catch(_){throw new Error('Could not parse local plan preflight result; no sheet call was made');}
    const plan=payload.plan, dir=payload.outputDir;
    buildRequests(plan); // finish all schema/value checks before creating output or making any sheet call
    const createCode=`(async()=>{var fs=await import('node:fs/promises'),path=await import('node:path'),root=await fs.realpath(${JSON.stringify(workDir)}),out=path.resolve(${JSON.stringify(dir)}),parent=await fs.realpath(path.dirname(out)),r=path.relative(root,parent);if(r.startsWith('..')||path.isAbsolute(r))throw Error('Output outside workDir');await fs.mkdir(out);var real=await fs.realpath(out),rr=path.relative(root,real);if(!rr||rr.startsWith('..')||path.isAbsolute(rr))throw Error('Output outside workDir');nodeRepl.write(real);})()`;
    const created=await tools.mcp__node_repl__js({code:createCode,title:'完整校验计划后独占创建回执目录'});
    need(!created.isError,'Could not exclusively create local output directory; no sheet call was made');
    const persistNew=async(relativeName,value)=>{
      need(/^[A-Za-z0-9._-]+\.json$/.test(relativeName),'Unsafe persistence filename');
      const saved=await persistRaw({tools,rawPath:`${dir}/${relativeName}`,data:JSON.stringify(value,null,2)});
      need(saved!==false && saved?.isError!==true && saved?.structuredContent?.success!==false && saved?.success!==false, `Exclusive persistence failed: ${relativeName}`);
    };
    const io={persistNew,inputBinding:{plan_path:planPath,plan_sha256:payload.plan_sha256,
        source_plan_path:plan.source_plan_path,source_plan_sha256:plan.source_plan_sha256,
        snapshot_path:plan.snapshot_path,snapshot_sha256:plan.snapshot_sha256},
      writeCsv:op=>tools.mcp__dingtalk_sheet__set_range_from_csv({nodeId:op.nodeId,sheetId:op.sheet_id,startCell:op.range.split(':')[0],csv:op.csv,autoConvert:op.autoConvert,allowOverwrite:op.allowOverwrite}),
      updateFormat:op=>tools.mcp__dingtalk_sheet__update_range({nodeId:op.nodeId,sheetId:op.sheet_id,rangeAddress:op.range,numberFormat:op.numberFormat}),progress};
    const receipt=await run(plan,io);
    return {receipt,plan_sha256:payload.plan_sha256,output_dir:dir};
  }
  return {buildRequests,run,runWithTools};
})();
if(typeof module==='object') module.exports=shipmentSheetSubmit;
