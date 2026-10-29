/* Host-owned authorization boundary. Current desktop host has no verified
 * child tool isolation: only offline validation and explicit mock execution
 * are exported. Never pass live MCP callbacks to createMockEntry. */
'use strict';
const crypto = require('node:crypto');
const match = require('./sheet_mcp_submit.js');
const append = require('./shipment_sheet_submit.js');
const need = (ok, message) => { if (!ok) throw Error(message); };
const digest = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const clone = value => JSON.parse(JSON.stringify(value));
function value(raw) {
  need(raw && !raw.isError, 'Failed evidence response');
  if (raw.structuredContent) return raw.structuredContent;
  if (raw.content) return JSON.parse(raw.content.filter(x => x.type === 'text').map(x => x.text).join('\n'));
  return raw;
}
function createMockEntry(hostPolicy, mockIO) {
  // Policy is issued by Sol/host and must not be writable or supplied by Terra.
  const policy = clone(hostPolicy);
  need(policy.schema_version === 1 && policy.authorization_id && policy.mode === 'offline_mock', 'Explicit host mock authorization required');
  need(['packing_order','standalone'].includes(policy.workflow_kind), 'Explicit workflow scope required');
  need(policy.node_id && policy.target_url && Array.isArray(policy.sheets) && policy.sheets.length, 'Authorized document and sheets required');
  need(new Set(policy.sheets.map(x=>x.sheet_id)).size === policy.sheets.length, 'Duplicate sheet authorization');
  const load = (name, artifacts) => {
    const bytes = artifacts[name];
    need(typeof bytes === 'string' || Buffer.isBuffer(bytes), 'Missing bound artifact: ' + name);
    need(/^[0-9a-f]{64}$/.test(policy.artifact_hashes?.[name] || '') && digest(bytes) === policy.artifact_hashes[name], 'Artifact identity changed: ' + name);
    return JSON.parse(bytes.toString());
  };
  const scope = (node, url, sheets) => {
    need(node === policy.node_id && url === policy.target_url, 'Outside authorized document');
    for (const sheet of sheets) {
      const allowed = policy.sheets.find(x=>x.sheet_id === sheet.sheet_id && x.name === (sheet.name || sheet.sheet));
      need(allowed && Array.isArray(allowed.fields), 'Outside authorized sheet');
      need(Array.isArray(allowed.headers) && allowed.headers.length && new Set(allowed.headers).size===allowed.headers.length, 'Host-bound physical header mapping required');
      const fields = sheet.changes ? sheet.changes.map(x=>x.field) : sheet.headers;
      need(fields.every(x=>allowed.fields.includes(x)), 'Outside authorized fields');
      if (sheet.changes) for (const change of sheet.changes) {
        const letters = /^([A-Z]+)[1-9]\d*$/.exec(change.cell || '');
        let col=0; for(const c of letters?.[1] || '') col=col*26+c.charCodeAt(0)-64;
        need(col>0 && allowed.headers[col-1]===change.field, 'Physical cell differs from authorized field');
      }
      else need(JSON.stringify(sheet.headers)===JSON.stringify(allowed.headers), 'Append physical headers differ from host mapping');
    }
  };
  const gates = artifacts => {
    // Hash-bound raw evidence is required; a caller-provided boolean is insufficient.
    if (policy.workflow_kind !== 'packing_order') return;
    need(Array.isArray(policy.required_workorders) && policy.required_workorders.length && new Set(policy.required_workorders).size === policy.required_workorders.length, 'Exact workorder scope required');
    const packet = value(load('approval', artifacts));
    need(packet.ok === true && packet.read_only === true && packet.module === 'MO' && packet.start_row === 1 && packet.has_next_page === false && Array.isArray(packet.rows), 'Complete actual approval evidence required');
    const rows = packet.rows;
    for (const no of policy.required_workorders) {
      const found = rows.filter(x=>x.DOC_NO === no);
      need(found.length === 1 && found[0].ID && found[0].APPROVAL_STATUS === 'Y', 'Actual approval Y required: ' + no);
    }
    need(Array.isArray(policy.required_receipts) && policy.required_receipts.length, 'Prior stage receipts required');
    for (const name of policy.required_receipts) {
      const receipt = load(name, artifacts);
      need(receipt.ok === true && !receipt.failure && !['unknown','in_flight','partial'].includes(receipt.state), 'Prior stage incomplete: ' + name);
      if (receipt.phase === 'append' && receipt.operation_count === 0) {
        // A zero-new stage needs a host-bound, freshly rebuilt empty plan,
        // not a self-declared boolean. No submit callback is invoked for it.
        const empty = load(name+'/zero_plan', artifacts);
        need(JSON.stringify(load(name+'/zero_plan_revalidated',artifacts)) === JSON.stringify(empty), 'Zero-new source plan must be freshly rebuilt');
        need(empty.node_id===policy.node_id && empty.target_url===policy.target_url && empty.phase==='append' && Array.isArray(empty.requests) && empty.requests.length===0 && empty.source_evidence?.workflow_kind==='packing_order' && Array.isArray(empty.source_evidence.scope) && empty.source_evidence.scope.length===3 && ['sales','borrow','outbound_application'].every(x=>empty.source_evidence.scope.includes(x)), 'Complete zero-new source evidence required');
        need(receipt.completed_operations===0 && Array.isArray(receipt.operations) && receipt.operations.length===0 && Array.isArray(receipt.sheet_coverage) && receipt.sheet_coverage.length===0 && receipt.input_binding?.plan_sha256===policy.artifact_hashes[name+'/zero_plan'], 'Zero-new receipt differs from rebuilt empty plan');
        continue;
      }
      need(receipt.phase === 'append' && Number.isInteger(receipt.operation_count) && receipt.operation_count>0 && receipt.completed_operations===receipt.operation_count && Array.isArray(receipt.operations) && receipt.operations.length===receipt.operation_count && receipt.operations.every(x=>x.success===true) && Array.isArray(receipt.sheet_coverage) && receipt.sheet_coverage.length && receipt.sheet_coverage.every(x=>x.complete===true), 'Complete prior append receipt required');
      for(const op of receipt.operations) {
        const raw=value(load(name+'/'+op.file,artifacts));
        need(raw.index===op.index && raw.kind===op.kind && raw.sheet===op.sheet && raw.range===op.range && value(raw.response).success===true && !raw.error, 'Prior raw operation failed or missing');
      }
    }
  };
  const prepare = (command, artifacts) => {
    need(['match','append'].includes(command), 'Unsupported operation; ERP operations are unavailable');
    if (command === 'match') {
      gates(artifacts);
      const input = {batch:load('batch', artifacts), spec:load('spec', artifacts), check:load('check', artifacts)};
      need(input.batch.batch_id === policy.plan_id, 'Outside authorized plan');
      scope(input.batch.node_id, input.batch.target_url, input.batch.sheets);
      if (policy.workflow_kind === 'packing_order' && input.batch.kind === 'workorder') {
        const numbers = new Set(input.batch.sheets.flatMap(s=>s.changes.map(c=>c.after)));
        need(numbers.size === policy.required_workorders.length && policy.required_workorders.every(x=>numbers.has(x)), 'Approval scope differs from matched workorders');
      }
      const built = match.buildRequests(input);
      return {input, built};
    }
    const plan = load('append', artifacts);
    need(policy.plan_id === policy.artifact_hashes.append, 'Append identity must be its exact artifact hash');
    scope(plan.node_id, plan.target_url, plan.requests);
    need(plan.source_evidence?.workflow_kind === (policy.workflow_kind === 'packing_order' ? 'packing_order' : 'shipment_fill'), 'Append workflow evidence mismatch');
    need(Array.isArray(plan.source_evidence.scope) && (policy.workflow_kind !== 'packing_order' || ['sales','borrow','outbound_application'].every(x=>plan.source_evidence.scope.includes(x))), 'Three source evidence required');
    // Caller must include the freshly rebuilt append plan as a host-bound artifact.
    need(JSON.stringify(load('append_revalidated', artifacts)) === JSON.stringify(plan), 'Append must be rebuilt from raw sources immediately before execution');
    return {input:plan, built:append.buildRequests(plan)};
  };
  return Object.freeze({
    validate(command, artifacts) { return clone(prepare(command, artifacts).built); },
    async execute(command, artifacts) {
      const prepared = prepare(command, artifacts);
      need(mockIO && typeof mockIO.persistNew === 'function', 'Exclusive mock receipt persistence required');
      const io={...mockIO,persistBatch:undefined,persistNew:async(name,record)=>{
        const saved=await mockIO.persistNew(name,record);
        need(saved!==false && saved?.isError!==true && saved?.success!==false && saved?.structuredContent?.success!==false, 'Exclusive persistence failed: '+name);
        return saved;
      }};
      // Whole authorization has one durable start marker, even across new entry instances.
      await io.persistNew('authorization-started.json', {authorization_id:policy.authorization_id, plan_id:policy.plan_id, command});
      return command === 'match' ? match.run(prepared.input, io) : append.run(prepared.input, io);
    }
  });
}
function createLiveEntry() {
  throw Error('Live delegation closed: this host has no verified child tool allowlist or ERP isolation. Offline tests do not attest host isolation.');
}
module.exports = {createMockEntry, createLiveEntry, digest};
