const assert = require('node:assert/strict');
const shipment = require('../.agents/skills/packing-order/scripts/shipment_sheet_submit.js');
const packing = require('../.agents/skills/packing-order/scripts/sheet_mcp_submit.js');

function shipmentPlan() {
  return {schema_version:1,phase:'append',node_id:'n',target_url:'https://alidocs.dingtalk.com/i/nodes/n',
    check_basis:'initial_complete_snapshot',snapshot_path:'snapshot.json',source_plan_sha256:'a'.repeat(64),snapshot_sha256:'b'.repeat(64),
    requests:[{sheet:'s',sheet_id:'id',range:'A2:C2',before_row_count:1,before_scope_complete:true,
      headers:['预计发货日期','销货明细_业务数量','单号'],values:[['2026-10-31','1.25','0001']],
      type_columns:{'预计发货日期':{type:'date',format:'yyyy-mm-dd'},'销货明细_业务数量':{type:'number',format:'General'}}}]};
}
function assertTiming(record) {
  assert.equal(Number.isNaN(Date.parse(record.started_utc)),false);
  assert.equal(Number.isNaN(Date.parse(record.returned_utc)),false);
  assert.equal(typeof record.elapsed_seconds,'number');
  assert(record.elapsed_seconds>=0);
  assert.equal(record.timing_clock,'monotonic');
}
async function withoutPerformance(fn) {
  const descriptor=Object.getOwnPropertyDescriptor(globalThis,'performance');
  assert(descriptor?.configurable,'test runtime must allow temporarily hiding performance');
  try { Object.defineProperty(globalThis,'performance',{configurable:true,value:undefined}); return await fn(); }
  finally { Object.defineProperty(globalThis,'performance',descriptor); }
}

(async()=>{
  let cases=0;
  const files=new Map();
  const receipt=await shipment.run(shipmentPlan(),{persistNew:async(name,value)=>files.set(name,value),
    writeCsv:async()=>({success:true}),updateFormat:async()=>({success:true})});
  assert.equal(receipt.ok,true);
  for(const [name,record] of files) if(name.startsWith('operation-')) {assertTiming(record);cases++;}
  assert.equal(cases,5,'expected text, typed value and format operation receipts');

  for(const failKind of ['text','format']) {
    const failedFiles=new Map();
    const failed=await shipment.run(shipmentPlan(),{persistNew:async(name,value)=>failedFiles.set(name,value),
      writeCsv:async()=>{if(failKind==='text')throw Error('mock timeout');return {success:true};},
      updateFormat:async()=>{if(failKind==='format')throw Error('mock format timeout');return {success:true};}});
    assert.equal(failed.ok,false);
    const op=failedFiles.get('operation-000001.json');
    if(failKind==='text') {assert(op);assertTiming(op);}
    else {
      const formatEntry=[...failedFiles.entries()].find(([name,value])=>name.startsWith('operation-')&&value.kind==='format');
      assert(formatEntry);assertTiming(formatEntry[1]);
    }
    cases++;
  }

  const change={cell:'B2',field:'工单单号',before:'',after:'5101-1',value:'5101-1'};
  const batch={schema_version:1,transport:'dingtalk_sheet',batch_id:'b',kind:'workorder',node_id:'n',target_url:'https://alidocs.dingtalk.com/i/nodes/n',
    sheets:[{name:'s',sheet_id:'id',range:'A1:B3',changes:[change]}]};
  const common={schema_version:1,transport:'dingtalk_sheet',batch_id:'b',kind:'workorder',node_id:'n',target_url:batch.target_url};
  const spec={...common,phase:'submit',sheets:[{name:'s',node_id:'n',sheet_id:'id',range:'A1:B3',changes:[change]}]};
  const check={...common,phase:'check-before',ok:true,sheets:[{sheet:'s',node_id:'n',sheet_id:'id',range:'A1:B3',ok:true,difference_count:0}]};
  const persisted=[];
  const packReceipt=await packing.run({batch,spec,check},{persistBatch:async payload=>persisted.push(payload),updateRange:async()=>({success:true})});
  assert.equal(packReceipt.ok,true);assertTiming(persisted[1].results[0]);cases++;
  const unknown=[];
  const unknownReceipt=await packing.run({batch,spec,check},{persistBatch:async payload=>unknown.push(payload),updateRange:async()=>{throw Error('mock unknown');}});
  assert.equal(unknownReceipt.failure.state,'unknown');assertTiming(unknown[1].results[0]);cases++;

  await withoutPerformance(async()=>{
    const unavailableFiles=new Map();
    const unavailable=await shipment.run(shipmentPlan(),{persistNew:async(name,value)=>unavailableFiles.set(name,value),
      writeCsv:async()=>({success:true}),updateFormat:async()=>({success:true})});
    assert.equal(unavailable.ok,true);
    const records=[...unavailableFiles.entries()].filter(([name])=>name.startsWith('operation-')).map(([,value])=>value);
    assert.equal(records.length,5);
    for(const record of records) {assertTimingUnavailable(record);}
    const unavailablePack=[];
    const unavailablePackReceipt=await packing.run({batch,spec,check},{persistBatch:async payload=>unavailablePack.push(payload),updateRange:async()=>({success:true})});
    assert.equal(unavailablePackReceipt.ok,true);assertTimingUnavailable(unavailablePack[1].results[0]);
    const unavailableFailed=[];
    const unavailableFailureReceipt=await packing.run({batch,spec,check},{persistBatch:async payload=>unavailableFailed.push(payload),updateRange:async()=>{throw Error('mock unavailable-clock throw');}});
    assert.equal(unavailableFailureReceipt.failure.state,'unknown');assertTimingUnavailable(unavailableFailed[1].results[0]);
    cases++;
  });

  console.log(JSON.stringify({ok:true,offline_cases:cases,shipment_operation_records:5}));
})().catch(error=>{console.error(error);process.exitCode=1;});

function assertTimingUnavailable(record) {
  assert.equal(Number.isNaN(Date.parse(record.started_utc)),false);
  assert.equal(Number.isNaN(Date.parse(record.returned_utc)),false);
  assert.equal(record.elapsed_seconds,null);
  assert.equal(record.timing_clock,'unavailable');
}
