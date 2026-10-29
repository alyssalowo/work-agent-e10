'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { planRead } = require('../scripts/sheet_read_plan.cjs');

function request(overrides = {}) {
  return {
    schema_version: 1,
    workflow: 'complete',
    round_id: 'round-7',
    document: {
      node_id: 'node-1',
      target_url: 'https://sheet.example/i/nodes/node-1',
      sheets: [
        { sheet_id: 'sid-a', name: 'Target', nonEmptyRange: { range: 'A1:H20', lastRow: 20, lastColumn: 'H', lastCell: 'H20' }, doc_no_column: 'B' },
        { sheet_id: 'sid-b', name: 'Other', nonEmptyRange: { range: 'A1:F12', lastRow: 12, lastColumn: 'F', lastCell: 'F12' }, doc_no_column: 'D' },
      ],
    },
    target_sheet_id: 'sid-a',
    ...overrides,
  };
}

test('complete workflow reads target once in full and other sheets by doc number column', () => {
  const p = planRead(request());
  assert.equal(p.status, 'ready');
  assert.deepEqual(p.actions.map(a => [a.action, a.sheet_id, a.range]), [
    ['read_full', 'sid-a', 'A1:H20'],
    ['read_doc_no_column', 'sid-b', 'D1:D12'],
  ]);
});

test('independent fill defaults to doc number columns, including target', () => {
  const p = planRead(request({ workflow: 'independent_fill', target_sheet_id: 'sid-a' }));
  assert.deepEqual(p.actions.map(a => a.action), ['read_doc_no_column', 'read_doc_no_column']);
});

test('independent fill requests target full snapshot after confirmed additions or matching requirement', () => {
  for (const flag of ['confirmed_additions', 'match_required']) {
    const p = planRead(request({ workflow: 'independent_fill', [flag]: true }));
    assert.deepEqual(p.actions.map(a => a.action), ['read_full', 'read_doc_no_column']);
  }
});

test('independent match reads specified target sheets fully', () => {
  const p = planRead(request({ workflow: 'independent_match', target_sheet_ids: ['sid-a', 'sid-b'] }));
  assert.deepEqual(p.actions.map(a => a.action), ['read_full', 'read_full']);
});

test('raw same-round cache is revalidated and hash bound', () => {
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'sheet-read-'));
  try {
    const file=path.join(dir,'raw.json');
    const cols=['A','B','C','D','E','F','G','H'], rows=Array.from({length:20},(_,i)=>i+1);
    const metadata={success:true,id:'sid-a',name:'Target',nonEmptyRange:{range:'A1:H20',lastColumn:'H',lastRow:20,lastCell:'H20'}};
    const response={success:true,hasMore:false,truncationReasons:[],returnedRange:'A1:H20',colIndices:cols,rowIndices:rows,csv:[cols.join(','),...Array(19).fill(',,,,,,,')].join('\n')};
    const raw={target_url:request().document.target_url,node_id:'node-1',round_id:'round-7',collection_failures:[],sheets:[{name:'Target',sheet_id:'sid-a',metadata,response}]};
    fs.writeFileSync(file,JSON.stringify(raw));const ref={path:file,sha256:crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex')};
    assert.equal(planRead(request({same_round_cache:[ref]})).actions[0].action,'reuse_full_snapshot');
    raw.sheets[0].response.hasMore=true;fs.writeFileSync(file,JSON.stringify(raw));
    assert.equal(planRead(request({same_round_cache:[ref]})).actions[0].action,'read_full');
    ref.sha256=crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
    assert.equal(planRead(request({same_round_cache:[ref]})).actions[0].action,'read_full');
    assert.equal(planRead(request({same_round_cache:[{schema_version:1,ok:true,complete:true}]})).actions[0].action,'read_full');
  } finally {fs.rmSync(dir,{recursive:true,force:true});}
});

test('conflict returns only affected bounded range checks and blocks recovery', () => {
  const p = planRead(request({ conflicts: [{ sheet_id: 'sid-b', range: 'D4:D6', reason: 'unknown row mapping' }] }));
  assert.equal(p.status, 'recovery_blocked');
  assert.equal(p.recovery_blocked, true);
  assert.deepEqual(p.actions, [{ action: 'recheck_range', sheet_id: 'sid-b', sheet_name: 'Other', range: 'D4:D6', reason: 'unknown row mapping' }]);
});

test('rejects inconsistent host range metadata, unknown target, and out-of-range conflict', () => {
  assert.throws(() => planRead(request({ document: { ...request().document, sheets: [
    { sheet_id: 'sid-a', name: 'Target', nonEmptyRange: { range: 'A1:H20', lastRow: 19, lastColumn: 'H', lastCell: 'H20' }, doc_no_column: 'B' },
  ] } })), /bounds disagree/);
  assert.throws(() => planRead(request({ target_sheet_id: 'missing' })), /target sheet id/);
  assert.throws(() => planRead(request({ conflicts: [{ sheet_id: 'sid-b', range: 'G2:G3' }] })), /outside sheet bounds/);
});

test('plan explicitly does not claim cache atomicity or online conditional checking', () => {
  const p = planRead(request());
  assert.equal(p.snapshot_is_atomic, false);
  assert.equal(p.cache_is_online_prewrite_check, false);
});
