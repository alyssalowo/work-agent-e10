#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const crypto = require('node:crypto');
const { validateCapturedSheet } = require('./sheet_mcp_submit.js');

function fail(message) {
  const error = new Error(message);
  error.name = 'PlanInputError';
  throw error;
}

function colNumber(col) {
  if (typeof col !== 'string' || !/^[A-Z]+$/.test(col)) return 0;
  let n = 0;
  for (const ch of col) n = n * 26 + ch.charCodeAt(0) - 64;
  return n;
}

function parseA1(value, allowCell = false) {
  if (typeof value !== 'string') return null;
  const re = allowCell
    ? /^([A-Z]+)([1-9]\d*)(?::([A-Z]+)([1-9]\d*))?$/
    : /^([A-Z]+)([1-9]\d*):([A-Z]+)([1-9]\d*)$/;
  const m = value.match(re);
  if (!m) return null;
  const c1 = colNumber(m[1]);
  const r1 = Number(m[2]);
  const c2 = m[3] ? colNumber(m[3]) : c1;
  const r2 = m[4] ? Number(m[4]) : r1;
  if (!c1 || !c2 || c1 > c2 || r1 > r2) return null;
  return { c1, r1, c2, r2 };
}

function validateSheet(sheet, index) {
  if (!sheet || typeof sheet !== 'object') fail(`document.sheets[${index}] must be an object`);
  const { sheet_id, name, nonEmptyRange, doc_no_column } = sheet;
  if (typeof sheet_id !== 'string' || !sheet_id.trim()) fail(`sheet ${index} missing actual sheet_id`);
  if (typeof name !== 'string' || !name.trim()) fail(`sheet ${sheet_id} missing actual name`);
  if (!nonEmptyRange || typeof nonEmptyRange !== 'object') fail(`sheet ${sheet_id} missing nonEmptyRange metadata`);
  const range = parseA1(nonEmptyRange.range);
  if (!range || range.r1 !== 1 || range.c1 !== 1) fail(`sheet ${sheet_id} nonEmptyRange.range must be an exact A1 rectangle from A1`);
  const lastCol = colNumber(nonEmptyRange.lastColumn);
  const lastRow = Number(nonEmptyRange.lastRow);
  if (!lastCol || !Number.isInteger(lastRow) || lastRow < 1 || range.c2 !== lastCol || range.r2 !== lastRow) {
    fail(`sheet ${sheet_id} nonEmptyRange bounds disagree with range`);
  }
  const lastCell = `${nonEmptyRange.lastColumn}${lastRow}`;
  if (nonEmptyRange.lastCell !== lastCell) fail(`sheet ${sheet_id} nonEmptyRange.lastCell disagrees with bounds`);
  const docCol = colNumber(doc_no_column);
  if (!docCol || docCol > lastCol) fail(`sheet ${sheet_id} doc_no_column must be a physical column within nonEmptyRange`);
  return { sheet_id, name, range: nonEmptyRange.range, bounds: range, doc_no_column };
}

function validateInput(input) {
  if (!input || input.schema_version !== 1) fail('schema_version must be 1');
  if (!['complete', 'independent_fill', 'independent_match'].includes(input.workflow)) fail('unsupported workflow');
  const doc = input.document;
  if (!doc || typeof doc.target_url !== 'string' || !doc.target_url || typeof doc.node_id !== 'string' || !doc.node_id) {
    fail('document.target_url and document.node_id are required');
  }
  let url; try { url = new URL(doc.target_url); } catch (_) { fail('invalid target_url'); }
  const nodeMatch = /^\/i\/nodes\/([^/]+)\/?$/.exec(url.pathname);
  if (!nodeMatch || decodeURIComponent(nodeMatch[1]) !== doc.node_id) fail('target_url node_id mismatch');
  if (!Array.isArray(doc.sheets) || doc.sheets.length === 0) fail('document.sheets must contain host metadata');
  const sheets = doc.sheets.map(validateSheet);
  if (new Set(sheets.map(s => s.sheet_id)).size !== sheets.length) fail('duplicate sheet_id');
  if (new Set(sheets.map(s => s.name)).size !== sheets.length) fail('duplicate sheet name');
  if (typeof input.round_id !== 'string' || !input.round_id.trim()) fail('round_id is required');
  let targets = [];
  if (input.workflow === 'complete') {
    targets = [input.target_sheet_id];
  } else if (input.workflow === 'independent_match') {
    targets = input.target_sheet_ids;
    if (!Array.isArray(targets) || targets.length === 0) fail('independent_match requires target_sheet_ids');
  } else {
    targets = (input.confirmed_additions === true || input.match_required === true) ? [input.target_sheet_id] : [];
  }
  if (targets.some(id => typeof id !== 'string' || !sheets.some(s => s.sheet_id === id))) fail('target sheet id is not present in host metadata');
  if (new Set(targets).size !== targets.length) fail('duplicate target sheet id');
  return { sheets, targets };
}

function cacheEligibility(input, sheet) {
  for (const ref of (Array.isArray(input.same_round_cache) ? input.same_round_cache : [])) {
    try {
      if (!ref || typeof ref.path !== 'string' || typeof ref.sha256 !== 'string') continue;
      const bytes = fs.readFileSync(ref.path);
      if (crypto.createHash('sha256').update(bytes).digest('hex') !== ref.sha256) continue;
      const raw = JSON.parse(bytes.toString('utf8'));
      if (raw.recovery_blocked || raw.round_id !== input.round_id || raw.target_url !== input.document.target_url || raw.node_id !== input.document.node_id || !Array.isArray(raw.collection_failures) || raw.collection_failures.length || !Array.isArray(raw.sheets)) continue;
      const matches = raw.sheets.filter(s => s.sheet_id === sheet.sheet_id);
      if (matches.length !== 1) continue;
      validateCapturedSheet(matches[0], {name:sheet.name,sheet_id:sheet.sheet_id,range:sheet.range,full_range:sheet.range});
      return {ok:true,path:ref.path,sha256:ref.sha256};
    } catch (_) { continue; }
  }
  return {ok:false,reason:(input.same_round_cache || []).length ? 'raw_cache_invalid' : 'no_full_snapshot_for_sheet_id'};
}

function recoveryActions(input, sheets) {
  if (!Array.isArray(input.conflicts) || input.conflicts.length === 0) return null;
  const byId = new Map(sheets.map(s => [s.sheet_id, s]));
  const actions = input.conflicts.map((item, i) => {
    if (!item || typeof item.sheet_id !== 'string' || typeof item.range !== 'string') fail(`conflicts[${i}] requires sheet_id and exact range`);
    const sheet = byId.get(item.sheet_id);
    if (!sheet) fail(`conflicts[${i}] references unknown sheet_id`);
    const r = parseA1(item.range, true);
    if (!r || r.c1 < 1 || r.r1 < 1 || r.c2 > sheet.bounds.c2 || r.r2 > sheet.bounds.r2) fail(`conflicts[${i}] range is invalid or outside sheet bounds`);
    return { action: 'recheck_range', sheet_id: sheet.sheet_id, sheet_name: sheet.name, range: item.range, reason: String(item.reason || 'conflict_or_unknown') };
  });
  return actions;
}

function planRead(input) {
  const { sheets, targets } = validateInput(input);
  const conflicts = recoveryActions(input, sheets);
  const base = {
    schema_version: 1,
    consumer: 'sheet_snapshot_collector',
    workflow: input.workflow,
    round_id: input.round_id,
    document: { node_id: input.document.node_id, target_url: input.document.target_url },
    snapshot_is_atomic: false,
    cache_is_online_prewrite_check: false,
  };
  if (conflicts) return { ...base, status: 'recovery_blocked', recovery_blocked: true, actions: conflicts };

  const actions = [];
  const cache_rejections = [];
  for (const sheet of sheets) {
    const full = targets.includes(sheet.sheet_id);
    if (full) {
      const cache = cacheEligibility(input, sheet);
      if (cache.ok) {
        actions.push({ action: 'reuse_full_snapshot', sheet_id: sheet.sheet_id, sheet_name: sheet.name, range: sheet.range, cache: {path:cache.path,sha256:cache.sha256} });
      } else {
        if (cache.reason !== 'no_full_snapshot_for_sheet_id') cache_rejections.push({ sheet_id: sheet.sheet_id, reason: cache.reason });
        actions.push({ action: 'read_full', sheet_id: sheet.sheet_id, sheet_name: sheet.name, range: sheet.range });
      }
    } else {
      const end = sheet.bounds.r2;
      actions.push({ action: 'read_doc_no_column', sheet_id: sheet.sheet_id, sheet_name: sheet.name, range: `${sheet.doc_no_column}1:${sheet.doc_no_column}${end}`, doc_no_column: sheet.doc_no_column });
    }
  }
  return { ...base, status: 'ready', recovery_blocked: false, actions, cache_rejections };
}

function main(argv) {
  const arg = name => { const i = argv.indexOf(name); return i >= 0 ? argv[i + 1] : null; };
  const inputPath = arg('--input');
  const outputPath = arg('--out');
  if (!inputPath) fail('usage: node sheet_read_plan.cjs --input request.json [--out plan.json]');
  const input = JSON.parse(fs.readFileSync(inputPath, 'utf8'));
  const output = planRead(input);
  const json = `${JSON.stringify(output, null, 2)}\n`;
  if (outputPath) fs.writeFileSync(outputPath, json, {encoding:'utf8',flag:'wx'});
  else process.stdout.write(json);
  if (output.recovery_blocked) process.exitCode = 2;
}

if (require.main === module) {
  try { main(process.argv.slice(2)); }
  catch (error) { process.stderr.write(`${error.name || 'Error'}: ${error.message}\n`); process.exitCode = 1; }
}

module.exports = { planRead, parseA1 };
