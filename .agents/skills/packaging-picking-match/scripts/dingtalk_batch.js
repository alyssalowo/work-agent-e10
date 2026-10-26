// Factory used by batch.py. Uses only the current DOM and normal copy/paste events.
(spec) => async (page) => {
  const canonical = value => { const u = new URL(value); return u.origin + u.pathname; };
  const equal = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const ids = value => [...new Set(value.split(/[\r\n/]+/).map(x => x.trim()).filter(Boolean))].sort();
  const parseTsv = text => {
    const rows = [], row = []; let value = '', quoted = false, atStart = true;
    for (let i = 0; i < text.length; i++) {
      const c = text[i];
      if (quoted) {
        if (c === '"' && text[i + 1] === '"') { value += '"'; i++; }
        else if (c === '"') quoted = false;
        else value += c;
      } else if (c === '"' && atStart) { quoted = true; atStart = false; }
      else if (c === '\t') { row.push(value); value = ''; atStart = true; }
      else if (c === '\n' || c === '\r') {
        if (c === '\r' && text[i + 1] === '\n') i++;
        row.push(value); rows.push(row.splice(0)); value = ''; atStart = true;
      } else { value += c; atStart = false; }
    }
    if (quoted) throw new Error('TSV 引号不完整');
    if (row.length || value || (text.length && !/[\r\n]$/.test(text))) { row.push(value); rows.push(row); }
    return rows.map(row => row.map(value => value.replace(/\r\n?/g, '\n')));
  };
  const quoteTsv = rows => rows.map(row => row.map(value => /[\t\r\n"]/.test(value)
    ? '"' + value.replace(/"/g, '""') + '"' : value).join('\t')).join('\r\n') + '\r\n';
  const column = n => { let s = ''; for (; n; n = Math.floor((n - 1) / 26)) s = String.fromCharCode(65 + (n - 1) % 26) + s; return s; };
  const cell = (r, c) => column(c + 1) + (r + 1);
  const frame = async () => {
    if (canonical(page.url()) !== canonical(spec.target_url)) throw new Error('当前文档 URL 与本次请求不同');
    await page.locator('#wiki-new-sheet-iframe').waitFor({state: 'attached', timeout: 10000});
    await page.frameLocator('#wiki-new-sheet-iframe').getByTestId('SelectionDisplayContainer')
      .waitFor({state: 'visible', timeout: 10000});
    const found = page.frames().filter(f => f.url().includes('/spreadsheetv2/'));
    if (found.length !== 1) throw new Error('无法唯一定位表格子框架');
    return found[0];
  };
  const active = async (f, sheet) => {
    if (canonical(page.url()) !== canonical(spec.target_url)) throw new Error('操作期间文档已改变');
    if (await f.locator('[data-testid="dingdocs-sheet-bar-SheetText"].active').innerText() !== sheet.name)
      throw new Error('活动工作表已改变');
  };
  const selectSheet = async (f, sheet) => {
    const escaped = sheet.name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const tab = f.getByTestId('dingdocs-sheet-bar-SheetText').filter({hasText: new RegExp('^' + escaped + '$')});
    if (await tab.count() !== 1) throw new Error('工作表标签不唯一');
    // This page version selects tabs on mousedown. Avoid zoom-sensitive coordinates.
    await tab.dispatchEvent('mousedown', {button: 0, buttons: 1});
    await tab.dispatchEvent('mouseup', {button: 0, buttons: 0});
    await tab.dispatchEvent('click', {button: 0});
    await f.waitForFunction(name => Array.from(document.querySelectorAll('[data-testid="dingdocs-sheet-bar-SheetText"].active'))
      .some(e => e.textContent === name), sheet.name, {timeout: 5000});
    await active(f, sheet);
    const id = await tab.getAttribute('id');
    if (!id?.startsWith('sheet-tab-')) throw new Error('当前工作表标识结构需要重新确认');
    return id.slice('sheet-tab-'.length);
  };
  const selectRange = async (f, sheet, range) => {
    await active(f, sheet);
    const input = f.getByTestId('SelectionEditContainer');
    if (!(await input.isVisible())) await f.getByTestId('SelectionDisplayContainer').evaluate(e => e.click());
    await input.fill(range); await input.press('Enter');
    await input.waitFor({state: 'hidden', timeout: 5000});
    await f.waitForFunction(start => document.querySelector('[data-testid="SelectionDisplayContainer"]')?.textContent.trim() === start,
      range.split(':')[0], {timeout: 5000});
    await active(f, sheet);
  };
  const readSheet = async (f, sheet) => {
    const sheetId = await selectSheet(f, sheet);
    if (sheet.sheet_id && sheet.sheet_id !== sheetId) throw new Error('工作表名称相同但标识已改变');
    if (sheet.discover) {
      await selectRange(f, sheet, 'A1:A1');
      await page.keyboard.press('Control+a');
      await page.keyboard.press('Control+a');
      await active(f, sheet);
    } else await selectRange(f, sheet, sheet.range);
    await f.evaluate(() => {
      const old = window.__packagingBatchCopy;
      if (old?.handler) window.removeEventListener('copy', old.handler);
      const state = {};
      state.handler = e => {
        state.data = {tsv: e.clipboardData?.getData('text/plain'), rich: e.clipboardData?.getData('application/flex-sheet-slice+json')};
      };
      window.__packagingBatchCopy = state;
      window.addEventListener('copy', state.handler, {once: true});
    });
    let data;
    try {
      await page.keyboard.press('Control+c');
      await f.waitForFunction(() => typeof window.__packagingBatchCopy?.data?.tsv === 'string', null, {timeout: 5000});
      data = await f.evaluate(() => window.__packagingBatchCopy.data);
    } finally {
      await f.evaluate(() => {
        const state = window.__packagingBatchCopy;
        if (state?.handler) window.removeEventListener('copy', state.handler);
        delete window.__packagingBatchCopy;
      });
    }
    await active(f, sheet);
    const from = JSON.parse(data.rich || '{}').from;
    const rows = parseTsv(data.tsv);
    if (sheet.discover) {
      if (!from || from.sheetId !== sheetId || from.row !== 0 || from.col !== 0
        || !rows.length || !rows[0].length || rows.some(row => row.length !== rows[0].length))
        throw new Error('整表复制范围不完整');
      const wholeSheetSentinel = from.rowCount === 0 && from.colCount === 0;
      if (!wholeSheetSentinel && (from.rowCount !== rows.length || from.colCount !== rows[0].length))
        throw new Error('整表元数据与实际行列数不符');
      const fields = spec.kind === 'workorder'
        ? ['单身来源单号', '销货明细_品号', '销货明细_特征码', '销货明细_业务数量', sheet.workorder_column || '工单单号']
        : [sheet.workorder_column || '包装工单', '机器领料单号', '物料领料单号', '是否加急'];
      const header = rows.slice(0, spec.header_search_rows || 20)
        .map(row => row.map(value => value.trim())).find(row => fields.every(field => row.includes(field)));
      if (!header || fields.some(field => header.filter(value => value === field).length !== 1))
        throw new Error('整表缺少唯一的完整匹配表头');
      let width = 0;
      for (const row of rows) for (let c = width; c < row.length; c++) if (row[c] !== '') width = c + 1;
      if (!width) throw new Error('整表为空');
      // Keep all rows and interior empty columns; only discard a proven empty suffix.
      const trimmed = rows.map(row => row.slice(0, width));
      return {name: sheet.name, range: 'A1:' + column(width) + rows.length, sheet_id: sheetId,
        from, copy_scope: 'whole_sheet', projection: {copied_rows: rows.length, copied_columns: rows[0].length,
          kept_columns: width, removed_trailing_blank_columns: rows[0].length - width},
        tsv: quoteTsv(trimmed), rows: trimmed};
    }
    const want = sheet.bounds;
    if (!from || from.sheetId !== sheetId || from.row !== want.row || from.col !== want.col
      || from.rowCount !== want.rows || from.colCount !== want.cols) throw new Error('复制范围或工作表不符，须重新确认实际范围');
    if (rows.length !== want.rows || rows.some(row => row.length !== want.cols)) throw new Error('复制 TSV 行列数不完整');
    return {name: sheet.name, range: sheet.range, sheet_id: sheetId, from, tsv: data.tsv, rows};
  };
  const checkBefore = (sheet, actual) => {
    const guard = sheet.guard, base = guard.before, differences = [];
    if (actual.length !== base.length || actual.some((row, r) => row.length !== base[r].length))
      return [{reason: 'range_changed'}];
    if (spec.kind === 'workorder') {
      for (let r = 0; r < base.length; r++) for (const c of guard.columns)
        if (actual[r][c] !== base[r][c]) differences.push({row: r + sheet.bounds.row + 1, column: c + sheet.bounds.col + 1, reason: 'matching_or_workorder_changed'});
    } else {
      if (!equal(actual[guard.header], base[guard.header])) return [{reason: 'header_changed'}];
      for (const item of guard.orders) {
        const found = ids(actual[item.r][guard.order_column]);
        if (found.length !== item.orders.length || item.orders.some(order => !found.includes(order)))
          differences.push({row: item.r + sheet.bounds.row + 1, reason: 'workorder_changed'});
      }
      for (const change of sheet.changes) {
        const value = actual[change.r][change.c];
        if (value !== change.before && value !== change.after) differences.push({cell: change.cell, reason: 'planned_cell_changed'});
      }
    }
    return differences;
  };
  const makeBatches = changes => {
    const byRow = new Map(), values = new Map();
    for (const c of changes) {
      if (values.has(c.row + ':' + c.col)) throw new Error('提交计划包含重复单元格');
      values.set(c.row + ':' + c.col, c.after);
      if (!byRow.has(c.row)) byRow.set(c.row, []);
      byRow.get(c.row).push(c.col);
    }
    const segments = [];
    for (const [r, columns] of byRow) {
      columns.sort((a, b) => a - b); let first = columns[0], last = first;
      for (const c of columns.slice(1)) {
        if (c === last + 1) last = c;
        else { segments.push({top: r, bottom: r, left: first, right: last}); first = last = c; }
      }
      segments.push({top: r, bottom: r, left: first, right: last});
    }
    segments.sort((a, b) => a.left - b.left || a.right - b.right || a.top - b.top);
    const groups = [];
    for (const segment of segments) {
      const last = groups[groups.length - 1];
      if (last && last.left === segment.left && last.right === segment.right && last.bottom + 1 === segment.top) last.bottom = segment.bottom;
      else groups.push({...segment});
    }
    return groups.sort((a, b) => a.top - b.top || a.left - b.left).map(g => {
      const rows = [], cells = [];
      for (let r = g.top; r <= g.bottom; r++) {
        const row = [];
        for (let c = g.left; c <= g.right; c++) {
          if (!values.has(r + ':' + c)) throw new Error('粘贴矩形包含计划以外的单元格');
          row.push(values.get(r + ':' + c)); cells.push(cell(r, c));
        }
        rows.push(row);
      }
      return {range: cell(g.top, g.left) + ':' + cell(g.bottom, g.right), cells, tsv: quoteTsv(rows)};
    });
  };
  const result = {schema_version: 1, batch_id: spec.batch_id, kind: spec.kind, target_url: spec.target_url,
    phase: spec.phase, ok: false, sheets: [], submitted: [], already_present: [], save_feedback: [], persistence_verified: false};
  result.skipped_history = spec.skipped_history || [];
  let current = null;
  try {
    await page.bringToFront();
    const f = await frame();
    for (const sheet of spec.sheets) {
      if (spec.phase === 'submit' && !sheet.changes.length) continue;
      current = {sheet: sheet.name, range: sheet.range, stage: 'read-before'};
      // No post-write copy or refresh: this is the only read of this sheet in this stage.
      const data = await readSheet(f, sheet);
      if (spec.phase === 'collect') {
        const {rows, ...snapshot} = data; result.sheets.push(snapshot); continue;
      }
      const differences = checkBefore(sheet, data.rows);
      if (differences.length) { result.failure = {...current, reason: 'check_before_failed', differences: differences.slice(0, 20)}; return result; }
      const pending = [];
      for (const c of sheet.changes) {
        if (data.rows[c.r][c.c] === c.after) result.already_present.push({sheet: sheet.name, cell: c.cell});
        else pending.push(c);
      }
      for (const batch of makeBatches(pending)) {
        current = {sheet: sheet.name, range: batch.range, cells: batch.cells, stage: 'select'};
        await selectRange(f, sheet, batch.range);
        current.stage = 'paste';
        const handled = await f.evaluate(tsv => {
          const active = document.activeElement;
          if (!active || active.tagName !== 'TEXTAREA') throw new Error('无表格编辑位置');
          const data = new DataTransfer(); data.setData('text/plain', tsv);
          const event = new ClipboardEvent('paste', {clipboardData: data, bubbles: true, cancelable: true});
          active.dispatchEvent(event); return event.defaultPrevented;
        }, batch.tsv);
        if (!handled) throw new Error('粘贴事件未被接受');
        result.submitted.push({sheet: sheet.name, range: batch.range, cells: batch.cells});
      }
      current = {sheet: sheet.name, stage: 'save-feedback'};
      await f.waitForFunction(() => !/保存中/.test(document.body.innerText), null, {timeout: 15000});
      const text = await f.locator('body').innerText();
      if (/保存失败|保存出错|无法保存/.test(text)) throw new Error('页面显示保存错误');
      await active(f, sheet);
      result.save_feedback.push({sheet: sheet.name, status: 'paste_accepted_no_save_error'});
    }
    result.ok = true;
  } catch (error) {
    result.failure = {...current, reason: String(error.message || error), uncertain: current?.stage === 'paste'};
  }
  return result;
}
