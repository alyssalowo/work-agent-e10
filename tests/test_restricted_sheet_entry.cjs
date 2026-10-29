'use strict';
const assert = require('node:assert/strict');
const {createMockEntry,createLiveEntry,digest}=require('../.agents/skills/packing-order/scripts/restricted_sheet_entry.cjs');
function fixture() {
  const change={cell:'B2',field:'工单单号',before:'',after:'5101-1',value:'5101-1'};
  const common={schema_version:1,transport:'dingtalk_sheet',batch_id:'b',kind:'workorder',node_id:'n',target_url:'https://alidocs.dingtalk.com/i/nodes/n'};
  const batch={...common,sheets:[{name:'s',sheet_id:'id',range:'A1:B3',changes:[change]}]};
  const spec={...common,phase:'submit',sheets:[{name:'s',node_id:'n',sheet_id:'id',range:'A1:B3',changes:[change]}]};
  const check={...common,phase:'check-before',ok:true,sheets:[{sheet:'s',node_id:'n',sheet_id:'id',range:'A1:B3',ok:true,difference_count:0}]};
  const op={index:1,kind:'text',sheet:'s',range:'A2:B2',file:'operation-000001.json',success:true};
  const data={batch,spec,check,approval:{ok:true,read_only:true,module:'MO',start_row:1,has_next_page:false,rows:[{ID:'mo1',DOC_NO:'5101-1',APPROVAL_STATUS:'Y'}]},prior:{ok:true,phase:'append',operation_count:1,completed_operations:1,operations:[op],sheet_coverage:[{complete:true}]},'prior/operation-000001.json':{...op,response:{success:true}}};
  const artifacts=Object.fromEntries(Object.entries(data).map(([k,v])=>[k,JSON.stringify(v)]));
  const policy={schema_version:1,authorization_id:'auth',mode:'offline_mock',workflow_kind:'packing_order',node_id:'n',target_url:'https://alidocs.dingtalk.com/i/nodes/n',plan_id:'b',sheets:[{name:'s',sheet_id:'id',fields:['工单单号'],headers:['单号','工单单号']}],required_workorders:['5101-1'],required_receipts:['prior'],artifact_hashes:Object.fromEntries(Object.entries(artifacts).map(([k,v])=>[k,digest(v)]))};
  return {artifacts,policy};
}
function ioFor(files,writes,respond=()=>({success:true})) {return {
  persistNew:async(k,v)=>{assert(!files.has(k),'refuse replay');files.set(k,v);},
  updateRange:async op=>{writes.push(op);return respond(op);}
};}
function replace(f,name,mutate) {const obj=JSON.parse(f.artifacts[name]);mutate(obj);f.artifacts[name]=JSON.stringify(obj);f.policy.artifact_hashes[name]=digest(f.artifacts[name]);}
(async()=>{
  let cases=0;
  assert.throws(createLiveEntry,/closed/);cases++;
  {const f=fixture(),e=createMockEntry(f.policy);assert.throws(()=>e.validate('e10_commit_operation',f.artifacts),/ERP/);cases++;}
  for(const alter of [f=>f.policy.node_id='other',f=>f.policy.sheets[0].sheet_id='other',f=>f.policy.sheets[0].fields=['其他'],f=>f.policy.plan_id='other',f=>f.artifacts.batch+=' ',f=>delete f.artifacts.approval,f=>delete f.artifacts.prior,
    f=>replace(f,'approval',x=>x.rows[0].APPROVAL_STATUS='N'),f=>replace(f,'prior',x=>x.state='unknown'),f=>replace(f,'check',x=>x.sheets[0].difference_count=1),
    f=>replace(f,'batch',x=>x.sheets[0].changes[0].cell='A2'),f=>delete f.policy.sheets[0].headers,f=>replace(f,'approval',x=>x.start_row=2),f=>delete f.artifacts['prior/operation-000001.json'],f=>replace(f,'prior/operation-000001.json',x=>x.response.success=false)]) {
    const f=fixture();alter(f);let writes=[];
    await assert.rejects(()=>createMockEntry(f.policy,ioFor(new Map(),writes)).execute('match',f.artifacts));assert.equal(writes.length,0);cases++;
  }
  {const f=fixture(),files=new Map(),writes=[],io=ioFor(files,writes);const receipt=await createMockEntry(f.policy,io).execute('match',f.artifacts);assert.equal(receipt.ok,true);assert.equal(writes.length,1);
    await assert.rejects(()=>createMockEntry(f.policy,io).execute('match',f.artifacts),/replay/);assert.equal(writes.length,1);cases++;}
  {const f=fixture(),empty={phase:'append',node_id:'n',target_url:f.policy.target_url,requests:[],source_evidence:{workflow_kind:'packing_order',scope:['sales','borrow','outbound_application']}};
    for(const name of ['prior/zero_plan','prior/zero_plan_revalidated']){f.artifacts[name]=JSON.stringify(empty);f.policy.artifact_hashes[name]=digest(f.artifacts[name]);}
    replace(f,'prior',x=>{x.operation_count=0;x.completed_operations=0;x.operations=[];x.sheet_coverage=[];x.input_binding={plan_sha256:f.policy.artifact_hashes['prior/zero_plan']};});
    assert.equal(createMockEntry(f.policy).validate('match',f.artifacts).requests.length,1);cases++;
    delete f.artifacts['prior/zero_plan_revalidated'];assert.throws(()=>createMockEntry(f.policy).validate('match',f.artifacts));cases++;
  }
  for(const response of [()=>{throw Error('timeout');},()=>({success:false}),()=>({})]) {
    const f=fixture(),files=new Map(),writes=[],io=ioFor(files,writes,response);
    const receipt=await createMockEntry(f.policy,io).execute('match',f.artifacts);assert.equal(receipt.ok,false);assert.equal(writes.length,1);
    await assert.rejects(()=>createMockEntry(f.policy,io).execute('match',f.artifacts));assert.equal(writes.length,1);cases++;
  }
  // A partial multi-range success must stop at first unknown and never replay.
  {const f=fixture();for(const name of ['batch','spec'])replace(f,name,x=>x.sheets[0].changes.push({cell:'B3',field:'工单单号',before:'',after:'5101-2',value:'5101-2'}));
    f.policy.required_workorders.push('5101-2');replace(f,'approval',x=>x.rows.push({ID:'mo2',DOC_NO:'5101-2',APPROVAL_STATUS:'Y'}));
    // Separate rows force two writes without filling an intervening cell.
    for(const name of ['batch','spec'])replace(f,name,x=>{x.sheets[0].range='A1:B4';x.sheets[0].changes[1].cell='B4';});replace(f,'check',x=>x.sheets[0].range='A1:B4');
    const files=new Map(),writes=[],io=ioFor(files,writes,()=>{if(writes.length===2)throw Error('timeout');return {success:true};});
    const receipt=await createMockEntry(f.policy,io).execute('match',f.artifacts);assert.equal(receipt.ok,false);assert.equal(receipt.submitted[0].cells.length,1);assert.equal(writes.length,2);
    await assert.rejects(()=>createMockEntry(f.policy,io).execute('match',f.artifacts));assert.equal(writes.length,2);cases++;
  }
  function appendFixture() {
    const p={schema_version:1,phase:'append',node_id:'n',target_url:'https://alidocs.dingtalk.com/i/nodes/n',check_basis:'initial_complete_snapshot',snapshot_path:'snapshot.json',source_plan_sha256:'a'.repeat(64),snapshot_sha256:'b'.repeat(64),source_evidence:{workflow_kind:'packing_order',scope:['sales','borrow','outbound_application']},requests:[{sheet:'s',sheet_id:'id',range:'A2:C2',before_row_count:1,before_scope_complete:true,headers:['预计发货日期','销货明细_业务数量','单号'],values:[['2026-10-31','1.25','2301-1']],type_columns:{'预计发货日期':{type:'date',format:'yyyy-mm-dd'},'销货明细_业务数量':{type:'number',format:'General'}}}]};
    const artifacts={append:JSON.stringify(p),append_revalidated:JSON.stringify(p)};
    const policy={schema_version:1,authorization_id:'append',mode:'offline_mock',workflow_kind:'packing_order',node_id:'n',target_url:'https://alidocs.dingtalk.com/i/nodes/n',plan_id:digest(artifacts.append),sheets:[{name:'s',sheet_id:'id',fields:p.requests[0].headers,headers:p.requests[0].headers}],artifact_hashes:Object.fromEntries(Object.entries(artifacts).map(([k,v])=>[k,digest(v)]))};
    return {artifacts,policy};
  }
  for(const alter of [f=>delete f.artifacts.append_revalidated,f=>f.policy.sheets[0].fields=['单号'],f=>{replace(f,'append',x=>x.source_evidence.scope=['sales']);f.policy.plan_id=digest(f.artifacts.append);}]) {
    const f=appendFixture();alter(f);assert.throws(()=>createMockEntry(f.policy).validate('append',f.artifacts));cases++;
  }
  {const f=appendFixture(),files=new Map(),writes=[];const io={persistNew:ioFor(files,writes).persistNew,writeCsv:async x=>{writes.push(x);return {success:true};},updateFormat:async x=>{writes.push(x);throw Error('timeout');}};
    const r=await createMockEntry(f.policy,io).execute('append',f.artifacts);assert.equal(r.ok,false);const count=writes.length;assert(count>1);
    await assert.rejects(()=>createMockEntry(f.policy,io).execute('append',f.artifacts));assert.equal(writes.length,count);cases++;
  }
  for(const response of [false,{isError:true},{success:false},{structuredContent:{success:false}}]) {
    const f=fixture(),writes=[];await assert.rejects(()=>createMockEntry(f.policy,{persistNew:async()=>response,updateRange:async x=>writes.push(x)}).execute('match',f.artifacts),/persistence failed/);assert.equal(writes.length,0);cases++;
  }
  console.log(JSON.stringify({ok:true,offline_cases:cases,host_isolation_verified:false}));
})().catch(e=>{console.error(e);process.exitCode=1;});
