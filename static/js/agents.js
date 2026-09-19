// static/js/agents.js — Agents panel: persistent AI workers.
//
// The backend is routes/bots_routes.py (services/bots/*). This module is a
// view over that API and nothing more: it does not execute anything, and it
// never sends an owner/user id — identity is resolved server-side from the
// session cookie, so a crafted request from this file cannot claim another
// user's agent. Ownership failures come back as 404, exactly like a missing
// agent, and this UI renders them the same way.
//
// Wiring: the sidebar entry is #tool-agents-btn in index.html; app.js calls
// open() from there. Load this module BEFORE app.js (see the script tags at
// the bottom of index.html).

import uiModule from './ui.js';

const API = '/api/bots';
// Live-ish updates without a manual refresh. The bots API has no SSE stream
// yet, so the panel re-polls while it is open and stops the moment it closes.
const POLL_MS = 6000;

let modalEl = null;
let bodyEl = null;
let pollTimer = null;
let catalog = null;

// View state. `view` is either the list or one agent's detail page.
const state = {
  view: 'list',
  bots: [],
  loading: false,
  error: '',
  detail: null,          // bot dict for the open agent
  tab: 'overview',       // overview | runs | approvals | activity | memory
  tabData: {},           // tab name -> payload
  tabLoading: false,
  showCreate: false,
  createMsg: '',
  actionMsg: '',
  // Live copy of the create form's fields. renderCreate() builds fresh HTML, so
  // without this any re-render (a poll tick, a template click) would blank the
  // form the user is filling in.
  draft: {},
};

// Field ids restored after a re-render. Kept in one place so a new field only
// has to be added here to survive.
const DRAFT_FIELDS = [
  'ag-name', 'ag-desc', 'ag-instr', 'ag-model', 'ag-level',
  'ag-trigger', 'ag-time', 'ag-memory', 'ag-scope',
];

function esc(s) {
  try { return uiModule.esc(String(s ?? '')); }
  catch (_) { return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
}

function toast(msg, kind) {
  try {
    if (kind === 'error' && uiModule.showError) return uiModule.showError(msg);
    if (uiModule.showToast) return uiModule.showToast(msg);
  } catch (_) { /* non-fatal */ }
  return undefined;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (_) { data = { error: text }; }
  if (!res.ok) {
    // The API's error envelope is {error, code} nested under `detail`.
    const detail = data && data.detail ? data.detail : data || {};
    const err = new Error(detail.error || detail.message || `Request failed (${res.status})`);
    err.code = detail.code || '';
    err.status = res.status;
    throw err;
  }
  return data;
}

/* ── presentation helpers ── */

const STATUS_TONE = {
  active: '#4caf50', running: '#4caf50', completed: '#4caf50',
  draft: '#9e9e9e', paused: '#ff9800', waiting_approval: '#ff9800',
  queued: '#2196f3', error: '#f44336', failed: '#f44336',
  disabled: '#757575', cancelled: '#757575', skipped: '#757575',
};

function badge(text, tone) {
  const color = tone || STATUS_TONE[text] || '#9e9e9e';
  return `<span style="font-size:10px;text-transform:uppercase;letter-spacing:0.04em;` +
    `padding:2px 6px;border-radius:999px;border:1px solid ${color};color:${color};` +
    `white-space:nowrap;">${esc(text || 'unknown')}</span>`;
}

function card(inner, extra = '') {
  return `<div class="admin-card" style="${extra}">${inner}</div>`;
}

function fmtDate(iso) {
  if (!iso) return 'never';
  try { return new Date(iso).toLocaleString(); } catch (_) { return String(iso); }
}

function fmtDuration(ms) {
  if (ms == null) return '—';
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.round(ms / 60000)} min`;
}

/* ── list view ── */

function renderList() {
  const rows = state.bots.map(bot => {
    const caps = (bot.capabilities || []).length;
    const skills = (bot.skills || []).length;
    const targets = bot.allowed_transitions || [];
    const canRun = ['active', 'draft', 'paused'].includes(bot.status) || bot.status === 'running';
    return `
      <div class="admin-card" style="margin-bottom:10px;">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
          <span style="font-size:16px;">${esc(bot.avatar || '🤖')}</span>
          <strong style="font-size:14px;">${esc(bot.name)}</strong>
          ${badge(bot.status)}
          ${bot.paused ? badge('paused') : ''}
          <span style="flex:1"></span>
          <button class="admin-btn-sm" data-ag-open="${esc(bot.id)}">Open</button>
          ${canRun ? `<button class="admin-btn-sm" data-ag-run="${esc(bot.id)}">Run</button>` : ''}
          ${targets.includes('paused') ? `<button class="admin-btn-sm" data-ag-pause="${esc(bot.id)}">Pause</button>` : ''}
          ${targets.includes('active') && bot.status !== 'active' ? `<button class="admin-btn-sm" data-ag-resume="${esc(bot.id)}">Resume</button>` : ''}
          <button class="admin-btn-sm admin-btn-delete" data-ag-del="${esc(bot.id)}" title="Delete this agent">Delete</button>
        </div>
        ${bot.description ? `<div class="admin-toggle-sub" style="margin-top:6px;">${esc(bot.description)}</div>` : ''}
        <div class="admin-toggle-sub" style="margin-top:6px;display:flex;gap:12px;flex-wrap:wrap;">
          <span>Model: <strong>${esc(bot.model || 'default')}</strong></span>
          <span>Tools: <strong>${caps}</strong></span>
          <span>Skills: <strong>${skills}</strong></span>
          <span>Autonomy: <strong>${esc(bot.autonomy_label || bot.autonomy_level)}</strong></span>
          <span>Runs: <strong>${bot.total_runs || 0}</strong> (${bot.failed_runs || 0} failed)</span>
          <span>Last run: <strong>${esc(fmtDate(bot.last_run_at))}</strong></span>
        </div>
        ${bot.last_error ? `<div class="admin-toggle-sub" style="margin-top:6px;color:#f44336;">Last error: ${esc(bot.last_error)}</div>` : ''}
      </div>`;
  }).join('');

  return `
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;">
      <h2 style="margin:0;padding:0;font-size:15px;">Agents</h2>
      <span style="flex:1"></span>
      <button class="admin-btn-add" id="ag-new-btn">+ Create Agent</button>
    </div>
    <p class="admin-toggle-sub" style="margin:0 0 10px;">
      Persistent AI workers that remember context, use the tools you grant them,
      run in the background, pause for your approval and keep an auditable history.
    </p>
    ${state.error ? `<div class="admin-card" style="border-color:#f44336;"><span style="color:#f44336;">${esc(state.error)}</span></div>` : ''}
    ${state.loading && !state.bots.length ? '<p class="admin-toggle-sub">Loading…</p>' : ''}
    ${!state.loading && !state.bots.length && !state.error ? `
      <div class="admin-card">
        <p class="admin-toggle-sub" style="margin:0;">
          No agents yet. Agents are owned by the account that created them, so this
          list only ever shows yours — create one to get started.
        </p>
      </div>` : ''}
    ${rows}
  `;
}

/* ── create form ── */

function renderCreate() {
  const caps = (catalog && catalog.capabilities) || {};
  const levels = (catalog && catalog.autonomy_levels) || { '1': 'assist' };
  const templates = (catalog && catalog.templates) || [];

  const capBoxes = Object.entries(caps).map(([key, meta]) => {
    const risk = (meta && meta.risk) || 'low';
    const color = risk === 'high' ? '#f44336' : risk === 'medium' ? '#ff9800' : '#4caf50';
    return `
      <label style="display:flex;align-items:center;gap:6px;font-size:12px;margin:2px 0;">
        <input type="checkbox" class="ag-cap" value="${esc(key)}"${['web.search', 'memory.read'].includes(key) ? ' checked' : ''}>
        <span>${esc((meta && meta.label) || key)}</span>
        <span style="font-size:10px;color:${color};">${esc(risk)}</span>
      </label>`;
  }).join('');

  const templateOpts = templates.map(t =>
    `<option value="${esc(t.id)}">${esc(t.icon || '')} ${esc(t.name)}</option>`).join('');

  const levelOpts = Object.entries(levels).map(([k, label]) =>
    `<option value="${esc(k)}"${String(k) === '1' ? ' selected' : ''}>${esc(k)} — ${esc(label)}</option>`).join('');

  return `
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;">
      <h2 style="margin:0;padding:0;font-size:15px;">Create Agent</h2>
      <span style="flex:1"></span>
      <button class="admin-btn-sm" id="ag-cancel-create">Cancel</button>
    </div>
    ${templates.length ? `
    <div class="admin-card">
      <div class="settings-row">
        <label class="settings-label">Start from a template</label>
        <select class="settings-select" id="ag-template">
          <option value="">— none —</option>
          ${templateOpts}
        </select>
      </div>
      <div class="admin-toggle-sub">Templates are pre-filled settings; they add no special behaviour.</div>
    </div>` : ''}
    <div class="admin-card">
      <div class="settings-col">
        <div class="settings-row">
          <label class="settings-label">Name</label>
          <input class="settings-select" id="ag-name" placeholder="Research Agent" maxlength="120">
        </div>
        <div class="settings-row">
          <label class="settings-label">Description</label>
          <input class="settings-select" id="ag-desc" placeholder="What this agent is for" maxlength="2000">
        </div>
        <div class="settings-row">
          <label class="settings-label">Role / instructions</label>
          <textarea class="settings-select" id="ag-instr" rows="4" placeholder="Standing instructions injected into every run."></textarea>
        </div>
        <div class="settings-row">
          <label class="settings-label">Model</label>
          <input class="settings-select" id="ag-model" placeholder="(default chat model)">
        </div>
        <div class="settings-row">
          <label class="settings-label">Autonomy</label>
          <select class="settings-select" id="ag-level">${levelOpts}</select>
        </div>
      </div>
      <div class="admin-toggle-sub" style="margin-top:6px;">
        Autonomy caps what the agent may hold: level 0 observe, 1 assist, 2 act, 3 autonomous.
        Higher levels are still subject to the tools you tick and to your approval policy.
      </div>
    </div>
    <div class="admin-card">
      <h2 style="font-size:13px;margin:0 0 6px 0;">Tools</h2>
      <div class="admin-toggle-sub" style="margin-bottom:6px;">
        Nothing dangerous is granted by default. Unticked tools are removed from the
        agent's run server-side, not just hidden here.
      </div>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:2px;">
        ${capBoxes}
      </div>
    </div>
    <div class="admin-card">
      <h2 style="font-size:13px;margin:0 0 6px 0;">Schedule</h2>
      <div class="settings-row">
        <label class="settings-label">Trigger</label>
        <select class="settings-select" id="ag-trigger">
          <option value="manual">Manual only</option>
          <option value="daily">Daily</option>
          <option value="weekdays">Weekdays</option>
          <option value="weekly">Weekly</option>
          <option value="interval">Every few hours</option>
        </select>
      </div>
      <div class="settings-row" id="ag-time-row" style="display:none;">
        <label class="settings-label">At</label>
        <input class="settings-select" id="ag-time" type="time" value="08:00">
      </div>
      <div class="settings-row">
        <label class="settings-label">Remember across runs</label>
        <label class="admin-switch"><input type="checkbox" id="ag-memory" checked><span class="admin-slider"></span></label>
      </div>
      <div class="settings-row">
        <label class="settings-label">Memory scope</label>
        <select class="settings-select" id="ag-scope">
          <option value="bot">This agent only</option>
          <option value="user">Shared with my account</option>
        </select>
      </div>
    </div>
    ${state.createMsg ? `<div class="admin-card"><span style="color:#f44336;">${esc(state.createMsg)}</span></div>` : ''}
    <div style="display:flex;gap:8px;">
      <button class="admin-btn-add" id="ag-save">Create Agent</button>
      <button class="admin-btn-sm" id="ag-cancel-create-2">Cancel</button>
    </div>
  `;
}

function readCreatePayload() {
  const val = id => {
    const el = document.getElementById(id);
    return el ? el.value : '';
  };
  const checked = id => {
    const el = document.getElementById(id);
    return !!(el && el.checked);
  };
  const capabilities = Array.from(document.querySelectorAll('.ag-cap'))
    .filter(cb => cb.checked).map(cb => cb.value);

  const triggerKind = val('ag-trigger') || 'manual';
  let triggers = [];
  if (triggerKind === 'manual') {
    triggers = [{ type: 'manual' }];
  } else if (triggerKind === 'interval') {
    triggers = [{ type: 'interval', hours: 6 }];
  } else {
    triggers = [{
      type: 'schedule',
      schedule: triggerKind,
      scheduled_time: val('ag-time') || '08:00',
    }];
  }

  return {
    name: val('ag-name').trim(),
    description: val('ag-desc').trim() || null,
    instructions: val('ag-instr').trim() || null,
    model: val('ag-model').trim() || null,
    autonomy_level: parseInt(val('ag-level') || '1', 10),
    capabilities,
    triggers,
    memory_enabled: checked('ag-memory'),
    memory_scope: val('ag-scope') || 'bot',
  };
}

function applyTemplate(templateId) {
  if (!catalog || !catalog.templates) return;
  const t = catalog.templates.find(x => x.id === templateId);
  if (!t) return;
  const set = (id, v) => { const el = document.getElementById(id); if (el && v != null) el.value = v; };
  set('ag-name', t.name || '');
  set('ag-desc', t.description || '');
  set('ag-instr', t.instructions || '');
  set('ag-level', String(t.autonomy_level ?? 1));
  // Capabilities come from the template, but they still pass the server's
  // autonomy ceiling — if the two disagree the API says so and we show it.
  document.querySelectorAll('.ag-cap').forEach(cb => {
    cb.checked = (t.capabilities || []).includes(cb.value);
  });
  const kind = (t.trigger && t.trigger.type) || 'manual';
  const triggerSel = document.getElementById('ag-trigger');
  if (triggerSel) {
    if (kind === 'interval') triggerSel.value = 'interval';
    else if (kind === 'schedule') triggerSel.value = (t.trigger && t.trigger.schedule) || 'daily';
    else triggerSel.value = 'manual';
  }
  const timeEl = document.getElementById('ag-time');
  if (timeEl && t.trigger && t.trigger.scheduled_time) timeEl.value = t.trigger.scheduled_time;
  syncScheduleRows();
}

function syncScheduleRows() {
  const sel = document.getElementById('ag-trigger');
  const row = document.getElementById('ag-time-row');
  if (!sel || !row) return;
  row.style.display = sel.value === 'schedule' || ['daily', 'weekdays', 'weekly'].includes(sel.value)
    ? '' : 'none';
}

/* ── detail view ── */

function renderDetail() {
  const bot = state.detail;
  if (!bot) return '<p class="admin-toggle-sub">Loading agent…</p>';
  const tabs = ['overview', 'runs', 'approvals', 'activity', 'memory'];
  const tabButtons = tabs.map(t =>
    `<button class="memory-tab${state.tab === t ? ' active' : ''}" data-ag-tab="${t}">${esc(t[0].toUpperCase() + t.slice(1))}</button>`
  ).join('');

  return `
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;flex-wrap:wrap;">
      <button class="admin-btn-sm" id="ag-back">← All agents</button>
      <span style="font-size:16px;">${esc(bot.avatar || '🤖')}</span>
      <strong style="font-size:15px;">${esc(bot.name)}</strong>
      ${badge(bot.status)}
      <span style="flex:1"></span>
      <button class="admin-btn-sm" data-ag-run="${esc(bot.id)}">Run now</button>
      ${(bot.allowed_transitions || []).includes('paused') ? `<button class="admin-btn-sm" data-ag-pause="${esc(bot.id)}">Pause</button>` : ''}
      ${(bot.allowed_transitions || []).includes('active') && bot.status !== 'active' ? `<button class="admin-btn-sm" data-ag-resume="${esc(bot.id)}">Resume</button>` : ''}
      <button class="admin-btn-sm admin-btn-delete" data-ag-del="${esc(bot.id)}">Delete</button>
    </div>
    ${state.actionMsg ? `<div class="admin-card"><span>${esc(state.actionMsg)}</span></div>` : ''}
    <div class="memory-tabs">${tabButtons}</div>
    <div class="memory-tab-panel" style="margin-top:10px;">${renderTab()}</div>
  `;
}

function renderTab() {
  const bot = state.detail || {};
  if (state.tabLoading) return '<p class="admin-toggle-sub">Loading…</p>';
  const data = state.tabData[state.tab];

  if (state.tab === 'overview') {
    return card(`
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:6px;font-size:12px;">
        <span>Model: <strong>${esc(bot.model || 'default')}</strong></span>
        <span>Autonomy: <strong>${esc(bot.autonomy_label || bot.autonomy_level)}</strong></span>
        <span>Memory: <strong>${bot.memory_enabled ? esc(bot.memory_scope) : 'off'}</strong></span>
        <span>Runs: <strong>${bot.total_runs || 0}</strong> (${bot.successful_runs || 0} ok, ${bot.failed_runs || 0} failed)</span>
        <span>Last run: <strong>${esc(fmtDate(bot.last_run_at))}</strong></span>
        <span>Workspace: <strong>${esc(bot.workspace_dir || 'created on activation')}</strong></span>
        <span>Max retries: <strong>${bot.max_retries || 0}</strong></span>
        <span>Concurrency: <strong>${bot.max_concurrent_runs || 1}</strong></span>
      </div>
      ${bot.instructions ? `<div class="admin-toggle-sub" style="margin-top:8px;white-space:pre-wrap;">${esc(bot.instructions)}</div>` : ''}
      <div style="margin-top:8px;">
        <div class="admin-toggle-sub" style="margin-bottom:4px;">Tools (${(bot.capabilities || []).length})</div>
        <div style="display:flex;gap:4px;flex-wrap:wrap;">
          ${(bot.capabilities || []).map(c => badge(c, '#2196f3')).join('') || '<span class="admin-toggle-sub">none granted</span>'}
        </div>
      </div>
      <div style="margin-top:8px;">
        <div class="admin-toggle-sub" style="margin-bottom:4px;">Triggers</div>
        <div style="display:flex;gap:4px;flex-wrap:wrap;">
          ${(bot.triggers || []).map(t => badge(t.type + (t.schedule ? ` · ${t.schedule}` : '') + (t.scheduled_time ? ` · ${t.scheduled_time}` : ''), '#9e9e9e')).join('') || '<span class="admin-toggle-sub">manual only</span>'}
        </div>
      </div>
    `);
  }

  if (state.tab === 'runs') {
    const runs = (data && data.runs) || [];
    if (!runs.length) return '<p class="admin-toggle-sub">No runs yet.</p>';
    return runs.map(r => card(`
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
        ${badge(r.status)}
        ${badge(r.trigger_type, '#9e9e9e')}
        <span class="admin-toggle-sub">${esc(fmtDate(r.started_at || r.created_at))}</span>
        <span class="admin-toggle-sub">${esc(fmtDuration(r.duration_ms))}</span>
        ${r.tokens_used ? `<span class="admin-toggle-sub">${esc(r.tokens_used)} tok</span>` : ''}
        <span style="flex:1"></span>
        ${['failed', 'cancelled'].includes(r.status) ? `<button class="admin-btn-sm" data-ag-retry="${esc(r.id)}">Retry</button>` : ''}
      </div>
      ${r.summary ? `<div class="admin-toggle-sub" style="margin-top:6px;">${esc(r.summary)}</div>` : ''}
      ${r.error ? `<div class="admin-toggle-sub" style="margin-top:6px;color:#f44336;">${esc(r.error)}</div>` : ''}
      ${r.result ? `<div style="margin-top:6px;white-space:pre-wrap;font-size:12px;max-height:220px;overflow:auto;">${esc(r.result)}</div>` : ''}
    `, 'margin-bottom:8px;')).join('');
  }

  if (state.tab === 'approvals') {
    const approvals = (data && data.approvals) || [];
    if (!approvals.length) return '<p class="admin-toggle-sub">Nothing waiting on you.</p>';
    return approvals.map(a => card(`
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
        ${badge(a.risk_level, a.risk_level === 'high' ? '#f44336' : '#ff9800')}
        <strong style="font-size:13px;">${esc(a.action)}</strong>
        <span style="flex:1"></span>
        <button class="admin-btn-add" data-ag-approve="${esc(a.id)}">Approve</button>
        <button class="admin-btn-sm" data-ag-approve-pattern="${esc(a.id)}" title="Approve and allow this pattern for this agent">Allow pattern</button>
        <button class="admin-btn-sm admin-btn-delete" data-ag-deny="${esc(a.id)}">Deny</button>
      </div>
      <div class="admin-toggle-sub" style="margin-top:6px;">${esc(a.description)}</div>
    `, 'margin-bottom:8px;')).join('');
  }

  if (state.tab === 'activity') {
    const events = (data && data.events) || [];
    if (!events.length) return '<p class="admin-toggle-sub">No activity recorded yet.</p>';
    return `<div style="display:flex;flex-direction:column;gap:6px;">` + events.map(e => `
      <div style="display:flex;gap:8px;align-items:baseline;font-size:12px;">
        <span class="admin-toggle-sub" style="min-width:132px;">${esc(fmtDate(e.created_at || e.at))}</span>
        ${badge(e.event_type || e.type || 'event', '#2196f3')}
        <span>${esc(e.message || '')}</span>
      </div>`).join('') + '</div>';
  }

  if (state.tab === 'memory') {
    const items = (data && (data.memories || data.memory)) || [];
    if (!items.length) return '<p class="admin-toggle-sub">Nothing remembered yet.</p>';
    return items.map(m => card(
      `<div style="font-size:12px;">${esc(m.text || m.content || m.summary || JSON.stringify(m))}</div>
       <div class="admin-toggle-sub" style="margin-top:4px;">${esc(m.category || '')} · ${esc(fmtDate(m.created_at))}</div>`,
      'margin-bottom:8px;')).join('');
  }

  return '';
}

/* ── data loading ── */

async function loadCatalog() {
  if (catalog) return;
  try { catalog = await api(`${API}/meta/catalog`); } catch (_) { catalog = null; }
}

async function loadList({ silent = false } = {}) {
  if (!silent) { state.loading = true; state.error = ''; render(); }
  try {
    const data = await api(API);
    state.bots = (data && data.bots) || [];
    state.error = '';
  } catch (err) {
    state.error = err.message || 'Could not load agents';
  } finally {
    state.loading = false;
    render();
  }
}

async function loadDetail(botId, { silent = false } = {}) {
  try {
    state.detail = await api(`${API}/${encodeURIComponent(botId)}`);
    state.actionMsg = '';
  } catch (err) {
    state.detail = null;
    state.view = 'list';
    state.error = err.status === 404
      ? 'That agent no longer exists.'
      : (err.message || 'Could not load agent');
    render();
    return;
  }
  await loadTab(silent);
}

async function loadTab(silent = false) {
  const bot = state.detail;
  if (!bot) return;
  const tab = state.tab;
  if (tab === 'overview') { state.tabLoading = false; if (!silent) render(); return; }
  if (!silent) { state.tabLoading = true; render(); }
  const paths = { runs: 'runs', approvals: 'approvals', activity: 'activity', memory: 'memory' };
  try {
    state.tabData[tab] = await api(`${API}/${encodeURIComponent(bot.id)}/${paths[tab]}`);
  } catch (err) {
    state.tabData[tab] = null;
    state.error = err.message || 'Could not load that tab';
  } finally {
    state.tabLoading = false;
    render();
  }
}

/* ── actions ── */

async function act(fn, okMsg) {
  try {
    await fn();
    if (okMsg) state.actionMsg = okMsg;
    toast(okMsg || 'Done');
    return true;
  } catch (err) {
    state.actionMsg = err.message || 'Action failed';
    toast(err.message || 'Action failed', 'error');
    return false;
  } finally {
    render();
  }
}

/* ── render + events ── */

function render() {
  if (!bodyEl) return;
  bodyEl.innerHTML = state.showCreate
    ? renderCreate()
    : (state.view === 'detail' ? renderDetail() : renderList());
  // Re-rendering replaces the form's DOM, so put the user's typing back.
  if (state.showCreate) restoreDraft();
}

/* ── create-form draft (survives re-renders) ── */

function captureDraft() {
  if (!state.showCreate) return;
  const d = state.draft;
  DRAFT_FIELDS.forEach(id => {
    const field = document.getElementById(id);
    if (!field) return;
    d[id] = field.type === 'checkbox' ? field.checked : field.value;
  });
  d.__caps = Array.from(document.querySelectorAll('.ag-cap'))
    .filter(cb => cb.checked).map(cb => cb.value);
}

function restoreDraft() {
  const d = state.draft;
  if (!d || !Object.keys(d).length) return;
  DRAFT_FIELDS.forEach(id => {
    const field = document.getElementById(id);
    if (!field || d[id] === undefined) return;
    if (field.type === 'checkbox') field.checked = !!d[id];
    else field.value = d[id];
  });
  if (Array.isArray(d.__caps)) {
    document.querySelectorAll('.ag-cap').forEach(cb => {
      cb.checked = d.__caps.includes(cb.value);
    });
  }
  syncScheduleRows();
}

function onBodyClick(event) {
  const t = event.target.closest('[data-ag-open],[data-ag-run],[data-ag-pause],[data-ag-resume],' +
    '[data-ag-del],[data-ag-retry],[data-ag-approve],[data-ag-approve-pattern],[data-ag-deny],' +
    '[data-ag-tab],#ag-new-btn,#ag-cancel-create,#ag-cancel-create-2,#ag-back,#ag-save');
  if (!t) return;
  const id = t.dataset;

  if (t.id === 'ag-new-btn') { state.showCreate = true; state.createMsg = ''; render(); return; }
  if (t.id === 'ag-cancel-create' || t.id === 'ag-cancel-create-2') { state.showCreate = false; render(); return; }
  if (t.id === 'ag-back') { state.view = 'list'; state.detail = null; state.tabData = {}; loadList(); return; }

  if (t.id === 'ag-save') { saveNewAgent(); return; }

  if (id.agTab) {
    state.tab = id.agTab;
    if (state.tab !== 'overview' && !state.tabData[state.tab]) loadTab();
    else render();
    return;
  }

  if (id.agOpen) {
    state.view = 'detail';
    state.tab = 'overview';
    state.tabData = {};
    state.detail = null;
    render();
    loadDetail(id.agOpen);
    return;
  }

  const botId = state.detail ? state.detail.id : null;
  if (id.agRun) {
    act(async () => {
      await api(`${API}/${encodeURIComponent(id.agRun)}/run`, {
        method: 'POST', body: JSON.stringify({ trigger_type: 'manual' }),
      });
    }, 'Run queued');
    return;
  }
  if (id.agPause) { act(() => api(`${API}/${encodeURIComponent(id.agPause)}/pause`, { method: 'POST' }), 'Agent paused'); return; }
  if (id.agResume) { act(() => api(`${API}/${encodeURIComponent(id.agResume)}/resume`, { method: 'POST' }), 'Agent resumed'); return; }
  if (id.agDel) {
    if (!window.confirm('Delete this agent? Runs and approvals recorded for it are removed; its scheduled tasks are paused.')) return;
    act(async () => {
      await api(`${API}/${encodeURIComponent(id.agDel)}`, { method: 'DELETE' });
      state.view = 'list'; state.detail = null;
      await loadList({ silent: true });
    }, 'Agent deleted');
    return;
  }
  if (id.agRetry && botId) {
    act(() => api(`${API}/${encodeURIComponent(botId)}/runs/${encodeURIComponent(id.agRetry)}/retry`, { method: 'POST' }), 'Retry queued');
    return;
  }
  if (id.agApprove || id.agApprovePattern || id.agDeny) {
    const approvalId = id.agApprove || id.agApprovePattern || id.agDeny;
    const approve = !id.agDeny;
    act(async () => {
      await api(`${API}/${encodeURIComponent(botId)}/approvals/${encodeURIComponent(approvalId)}/decide`, {
        method: 'POST', body: JSON.stringify({ approve, pattern: !!id.agApprovePattern }),
      });
      await loadTab(true);
    }, approve ? 'Approved' : 'Denied');
  }
}

function onBodyChange(event) {
  const t = event.target;
  if (!t) return;
  if (t.id === 'ag-template') { applyTemplate(t.value); captureDraft(); return; }
  if (t.id === 'ag-trigger') syncScheduleRows();
  captureDraft();
}

// 'change' only fires on blur/commit, so typing needs 'input' to be captured as
// it happens — that is what makes a stray re-render harmless.
function onBodyInput() {
  captureDraft();
}

async function saveNewAgent() {
  const payload = readCreatePayload();
  if (!payload.name) { state.createMsg = 'Give the agent a name.'; render(); return; }
  state.createMsg = '';
  try {
    await api(API, { method: 'POST', body: JSON.stringify(payload) });
    state.showCreate = false;
    state.draft = {};   // the form is done with; do not resurrect it
    toast('Agent created');
    await loadList({ silent: true });
  } catch (err) {
    // e.g. capability_above_level — the server refuses a grant the autonomy
    // level cannot hold, and says which ones. Show its wording verbatim.
    state.createMsg = err.message || 'Could not create agent';
    render();
  }
}

/* ── public API ── */

export function open() {
  if (!modalEl) {
    modalEl = document.getElementById('agents-modal');
    bodyEl = document.getElementById('agents-body');
    if (!modalEl || !bodyEl) return;
    bodyEl.addEventListener('click', onBodyClick);
    bodyEl.addEventListener('change', onBodyChange);
    bodyEl.addEventListener('input', onBodyInput);
    const closeBtn = modalEl.querySelector('.close-btn');
    if (closeBtn) closeBtn.addEventListener('click', close);
    // Close on a backdrop click only when the press STARTED on the backdrop too.
    // A text selection dragged out of an input fires a click whose target is
    // the backdrop, which used to close the dialog and discard a half-filled
    // form.
    let backdropPressed = false;
    modalEl.addEventListener('mousedown', ev => { backdropPressed = ev.target === modalEl; });
    modalEl.addEventListener('click', ev => {
      const shouldClose = ev.target === modalEl && backdropPressed;
      backdropPressed = false;
      if (shouldClose) close();
    });
    document.addEventListener('keydown', ev => {
      if (ev.key === 'Escape' && !modalEl.classList.contains('hidden')) close();
    });
  }
  modalEl.classList.remove('hidden');
  if (!state.bots.length) loadList();
  else render();
  loadCatalog().then(render);
  if (!pollTimer) {
    pollTimer = setInterval(() => {
      if (modalEl.classList.contains('hidden')) return;
      // Never refresh underneath an open create form. loadList/loadDetail both
      // end in render(), which rebuilds the form's DOM — that is what made a
      // half-filled form appear to erase itself mid-typing.
      if (state.showCreate) return;
      if (state.view === 'detail' && state.detail) loadDetail(state.detail.id, { silent: true });
      else loadList({ silent: true });
    }, POLL_MS);
  }
}

export function close() {
  if (modalEl) modalEl.classList.add('hidden');
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

export function isOpen() {
  return !!(modalEl && !modalEl.classList.contains('hidden'));
}

export function refresh() {
  if (state.view === 'detail' && state.detail) return loadDetail(state.detail.id);
  return loadList();
}

const agentsModule = { open, close, isOpen, refresh };

export default agentsModule;
