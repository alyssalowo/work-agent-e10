const assert = require('node:assert/strict');
const {buildRequests, run} = require('../.agents/skills/packing-order/scripts/shipment_sheet_submit.js');
function plan(quantity='销货明细_业务数量') {
  return {schema_version:1,phase:'append',node_id:'n',target_url:'https://alidocs.dingtalk.com/i/nodes/n',
    check_basis:'initial_complete_snapshot',snapshot_path:'snapshot.json',source_plan_sha256:'a'.repeat(64),
    snapshot_sha256:'b'.repeat(64),requests:[{sheet:'s',sheet_id:'id',range:'A2:C2',
    before_row_count:1,before_scope_complete:true,headers:['预计发货日期',quantity,'单号'],
    values:[['2026-10-31','1.25','0001']],type_columns:{'预计发货日期':{type:'date',format:'yyyy-mm-dd'},
    [quantity]:{type:'number',format:'General'}}}]};
}
(async()=>{
  let cases=0;
  for (const qty of ['销货明细_业务数量','借出明细_业务数量']) {
    const p=plan(qty), built=buildRequests(p);
    assert.equal(built.coverage[0].typed_cells,2);
    assert.equal(built.operations.filter(x=>x.kind==='typed_values').length,2);
    assert.equal(built.operations.find(x=>x.header===qty).csv,'1.25');
    const writes=[], files=new Map();
    const receipt=await run(p,{persistNew:async(name,value)=>{assert(!files.has(name));files.set(name,value);},
      writeCsv:async op=>{writes.push(op);return {success:true};},
      updateFormat:async op=>{writes.push(op);return {success:true};}});
    assert.equal(receipt.ok,true);assert.equal(receipt.typed_cells,2);assert.equal(writes.length,5);
    cases++;
  }
  for (const alter of [
    p=>p.requests[0].values[0][0]='2026-02-30',
    p=>p.requests[0].values[0][1]='1234567890123456',
    p=>p.requests[0].values[0][2]='=1+1',
    p=>p.requests[0].headers.push('借出明细_业务数量')
  ]) {const p=plan();alter(p);assert.throws(()=>buildRequests(p));cases++;}
  const saved=[];
  const failed=await run(plan(),{persistNew:async(name,v)=>saved.push(name),
    writeCsv:async()=>({success:false}),updateFormat:async()=>{throw Error('must stop');}});
  assert.equal(failed.ok,false);assert(saved.includes('started.json'));cases++;
  let calls=0;
  await assert.rejects(()=>run(plan(),{persistNew:async()=>{throw Error('already started');},
    writeCsv:async()=>{calls++;},updateFormat:async()=>{calls++;}}));
  assert.equal(calls,0);cases++;
  console.log(JSON.stringify({ok:true,offline_cases:cases}));
})().catch(error=>{console.error(error);process.exitCode=1;});
