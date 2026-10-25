'use strict';

const $ = (sel) => document.querySelector(sel);

const state = {
  page: 1,
  size: 50,
  q: '',
  stateFilter: '',
  instance: '',
  sort: 'added',
  order: 'desc',
  items: [],
  total: 0,
  pages: 1,
  summary: null,
  selected: new Map(),       // key -> item
  timer: null,
  editingId: null,
  refreshing: false,
  instMap: {},
  errGroups: [],
  history: [],
  rules: [],
  limits: [],
  detailKey: '',
};

const STATE_LABEL = {
  downloading: '下载中', seeding: '做种', paused: '暂停', queued: '排队',
  checking: '校验', error: '错误', other: '其他',
};

/* ---------------- 工具 ---------------- */
function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function fmtBytes(n, digits = 1) {
  n = Number(n) || 0;
  if (n <= 0) return '0';
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return `${n.toFixed(i === 0 ? 0 : digits)} ${units[i]}`;
}
function fmtSpeed(n) { return `${fmtBytes(n, 1)}/s`; }
function fmtAgo(ts) {
  if (!ts) return '—';
  const d = Math.floor(Date.now() / 1000 - ts);
  if (d < 60) return `${d} 秒前`;
  if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
  if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
  return `${Math.floor(d / 86400)} 天前`;
}
let toastTimer = null;
function toast(msg, bad = false) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.toggle('bad', !!bad);
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 3200);
}
async function api(path, opts = {}) {
  const resp = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
  return data;
}

/* ---------------- 渲染 ---------------- */
function renderPills() {
  const wrap = $('#instance-pills');
  const insts = (state.summary && state.summary.instances) || [];
  state.instMap = {};
  insts.forEach((i) => { state.instMap[i.id] = i; });
  renderOpenMenu(insts);
  wrap.innerHTML = '';
  const all = document.createElement('button');
  all.className = 'inst' + (state.instance === '' ? ' active' : '');
  all.textContent = '所有实例';
  all.addEventListener('click', () => { state.instance = ''; state.page = 1; renderPills(); load(); });
  wrap.appendChild(all);
  insts.forEach((inst) => {
    const btn = document.createElement('button');
    btn.className = `inst ${inst.online ? 'online' : 'offline'}${String(state.instance) === String(inst.id) ? ' active' : ''}`;
    btn.innerHTML = `<span class="dot"></span>${esc(inst.name)}<span class="k">${esc(inst.kind)}</span><span class="n">${inst.count || 0}</span>`;
    btn.title = inst.online ? `${inst.kind.toUpperCase()} ${inst.version || ''} · ${inst.count} 个种子` : `离线：${inst.error || ''}`;
    btn.addEventListener('click', () => {
      state.instance = String(state.instance) === String(inst.id) ? '' : String(inst.id);
      state.page = 1;
      renderPills();
      load();
    });
    wrap.appendChild(btn);
  });
}

function renderOpenMenu(insts) {
  const menu = $('#open-menu');
  if (!menu) return;
  const list = (insts || []).filter((i) => i.webui);
  if (!list.length) {
    menu.innerHTML = '<button disabled>没有可用实例</button>';
    return;
  }
  menu.innerHTML = list.map((i) => `
    <a href="${esc(i.webui)}" target="_blank" rel="noreferrer noopener" class="${i.online ? '' : 'off'}">
      <span>${esc(i.name)}</span><span class="k">${esc(i.kind)}</span>
    </a>`).join('');
}

function jumpUrl(item) {
  const targets = item.instances || [];
  const qb = targets.find((t) => (state.instMap[t.instance_id] || {}).kind === 'qb');
  const pick = qb || targets[0];
  if (!pick) return '';
  const meta = state.instMap[pick.instance_id] || {};
  const base = meta.webui || '';
  if (!base) return '';
  if ((meta.kind === 'qb') && item.hash) {
    return `${base.replace(/\/$/, '')}#/details?hash=${encodeURIComponent(item.hash)}`;
  }
  return base;
}

function renderStats() {
  const s = state.summary;
  if (!s) return;
  const c = s.counts || {};
  const errCount = c.error || 0;
  const retryBtn = $('#btn-retry-errors');
  if (retryBtn) {
    retryBtn.hidden = errCount === 0;
    retryBtn.textContent = `重试全部错误（${errCount}）`;
  }
  $('#st-total').textContent = s.total ?? 0;
  $('#st-dl').textContent = c.downloading ?? 0;
  $('#st-seed').textContent = c.seeding ?? 0;
  $('#st-paused').textContent = c.paused ?? 0;
  $('#st-error').textContent = c.error ?? 0;
  $('#st-size').textContent = fmtBytes(s.size);
  const upd = $('#sp-updated');
  if (upd) upd.textContent = `更新 ${fmtAgo(Math.floor(s.at || 0))}`;
  $('#sp-dl').textContent = fmtSpeed(s.dl);
  $('#sp-ul').textContent = fmtSpeed(s.ul);
  renderDisks();
  drawSpark();
}

function renderDisks() {
  const wrap = $('#disks');
  const insts = (state.summary && state.summary.instances) || [];
  const seen = new Set();
  const rows = [];
  insts.forEach((i) => {
    if (i.free == null) return;
    const key = String(i.free);
    const dup = seen.has(key);
    seen.add(key);
    rows.push(`<span class="disk ${dup ? 'dup' : ''}" title="${esc(i.name)} · 剩余 ${fmtBytes(i.free)}${dup ? '（与同卷实例共享）' : ''}">
      <i>${esc(i.name)}</i><b>${fmtBytes(i.free, 0)}</b>${dup ? '<em>同卷</em>' : ''}</span>`);
  });
  wrap.innerHTML = (rows.length ? '<span class="disks-label">磁盘剩余</span>' : '') + (rows.join('') || '<span class="hint">磁盘空间读取中…</span>');
}

function drawSpark() {
  const cv = $('#spark');
  if (!cv || !cv.getContext) return;
  const ctx = cv.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const w = 88, h = 26;
  if (cv.width !== w * dpr) { cv.width = w * dpr; cv.height = h * dpr; }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const data = state.history.map((x) => x.ul_speed || 0);
  if (data.length < 2) {
    // 还没攒够采样点：画一条虚线占位，提示曲线在采集
    ctx.setLineDash([2, 3]);
    ctx.strokeStyle = 'rgba(122,162,255,.38)';
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, h - 6); ctx.lineTo(w, h - 6); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = 'rgba(122,162,255,.55)';
    ctx.fillRect(w - 3, h - 9, 3, 6);
    return;
  }
  const max = Math.max(...data, 1);
  const step = w / (data.length - 1);
  const pts = data.map((v, i) => [i * step, h - 3 - (v / max) * (h - 8)]);
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, 'rgba(122,162,255,.45)');
  grad.addColorStop(1, 'rgba(122,162,255,0)');
  ctx.beginPath();
  ctx.moveTo(0, h);
  pts.forEach(([x, y]) => ctx.lineTo(x, y));
  ctx.lineTo(w, h);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();
  ctx.beginPath();
  pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
  ctx.strokeStyle = 'rgba(150,180,255,.9)';
  ctx.lineWidth = 1.4;
  ctx.stroke();
}

async function loadErrGroups() {
  const panel = $('#err-groups');
  if (!panel) return;
  if (state.stateFilter !== 'error') { panel.hidden = true; panel.innerHTML = ''; return; }
  try {
    const d = await api('/api/errors_by_tracker');
    state.errGroups = d.groups || [];
    panel.hidden = false;
    panel.innerHTML = `
      <div class="eg-head">
        <span>按 tracker 聚合：<b>${d.total || 0}</b> 个错误任务</span>
        <span class="hint">点「重试」只重试该站，避免一次性轰所有 tracker</span>
      </div>
      <div class="eg-list">${state.errGroups.map((g) => `
        <div class="eg">
          <div class="eg-main"><b>${esc(g.tracker)}</b><span>${g.count}</span></div>
          <div class="eg-sub">${Object.entries(g.instances).map(([k, v]) => `${esc(k)} ${v}`).join(' · ')}</div>
          <div class="eg-act">
            <button class="btn ghost mini" data-retry-tracker="${esc(g.tracker)}">重试此站</button>
          </div>
        </div>`).join('')}</div>`;
  } catch (err) {
    panel.hidden = true;
  }
}

function rowHtml(item) {
  const pct = Math.round((item.progress || 0) * 1000) / 10;
  const done = item.progress >= 0.999;
  const allTags = item.labels || [];
  const tags = allTags.slice(0, 3).map((t) => `<span class="t-tag" title="${esc(t)}">${esc(t)}</span>`).join('')
    + (allTags.length > 3 ? `<span class="t-tag more" title="${esc(allTags.join(' / '))}">+${allTags.length - 3}</span>` : '');
  const cat = item.category ? `<span class="t-tag cat">${esc(item.category)}</span>` : '';
  const insts = (item.instances || []).map((t) => `<span class="b" title="${esc(t.instance_name)}">${esc(t.instance_name)}</span>`).join('');
  const isPaused = item.state === 'paused';
  return `
    <td class="col-check"><input type="checkbox" data-key="${esc(item.key)}"></td>
    <td class="col-name">
      <div class="t-name">
        <span class="t-title" title="${esc(item.name)}" data-detail="${esc(item.key)}" role="button" tabindex="0">${esc(item.name)}</span>
        <span class="t-sub">${item.state === 'error' && item.error_msg ? `<span class="t-err" title="${esc(item.error_msg)}">${esc(item.error_msg)}</span>` : ''}${cat}${tags}</span>
      </div>
    </td>
    <td class="col-inst" data-label="实例"><div class="t-inst">${insts}</div></td>
    <td class="col-size" data-label="大小">${fmtBytes(item.size)}</td>
    <td class="col-prog" data-label="进度">
      <div class="prog-head">
        <div class="bar ${done ? 'done' : ''} ${item.state === 'downloading' ? 'live' : ''}"><i style="width:${Math.min(100, pct)}%"></i></div>
        <span class="pct ${done ? 'hide' : ''}">${pct.toFixed(0)}%</span>
      </div>
    </td>
    <td class="col-speed" data-label="速度">
      <div class="sp-row" style="color:#9fc0ff">↓ ${fmtSpeed(item.dl)}</div>
      <div class="sp-row" style="color:#8fe0bd">↑ ${fmtSpeed(item.ul)}</div>
    </td>
    <td class="col-ratio" data-label="分享率">${(item.ratio || 0).toFixed(2)}</td>
    <td class="col-state" data-label="状态"><span class="st ${esc(item.state)}" title="${esc(item.error_msg || '')}">${esc(STATE_LABEL[item.state] || item.state)}</span></td>
    <td class="col-act">
      <div class="acts">
        <button class="icon-btn act" data-act="${isPaused ? 'resume' : 'pause'}" data-key="${esc(item.key)}"
          title="${isPaused ? '恢复' : '暂停'}">${isPaused ? '▶' : '⏸'}</button>
        <button class="icon-btn jump" data-jump="${esc(item.key)}" title="在下载器里打开">↗</button>
        <button class="icon-btn act" data-act="recheck" data-key="${esc(item.key)}" title="重新校验">⟳</button>
        <button class="icon-btn del act" data-act="delete" data-key="${esc(item.key)}" title="删除种子">✕</button>
      </div>
    </td>`;
}

function renderTable() {
  const tbody = $('#torrents tbody');
  const empty = $('#empty');
  tbody.innerHTML = '';
  if (!state.items.length) {
    empty.classList.add('show');
    empty.textContent = state.summary ? '没有匹配的任务' : '正在读取下载器…';
  } else {
    empty.classList.remove('show');
    const frag = document.createDocumentFragment();
    state.items.forEach((item) => {
      const tr = document.createElement('tr');
      tr.dataset.key = item.key;
      if (item.state === 'error') tr.classList.add('has-error');
      if (state.selected.has(item.key)) tr.classList.add('sel');
      tr.innerHTML = rowHtml(item);
      tr.querySelector('input[type=checkbox]').addEventListener('change', (e) => {
        if (e.target.checked) state.selected.set(item.key, item); else state.selected.delete(item.key);
        tr.classList.toggle('sel', e.target.checked);
        renderSelBar();
      });
      frag.appendChild(tr);
    });
    tbody.appendChild(frag);
  }
  const label = `第 ${state.page} / ${state.pages} 页 · 共 ${state.total} 条`;
  $('#page-label').textContent = label;
  const label2 = $('#page-label2'); if (label2) label2.textContent = label;
  const info = $('#foot-info');
  if (info) info.textContent = `当前页 ${state.items.length} 条 · 共 ${state.total} 条`;
  ['#btn-prev', '#btn-prev2'].forEach((sel) => { const el = $(sel); if (el) el.disabled = state.page <= 1; });
  ['#btn-next', '#btn-next2'].forEach((sel) => { const el = $(sel); if (el) el.disabled = state.page >= state.pages; });
}

function renderSelBar() {
  const n = state.selected.size;
  $('#sel-count').textContent = n;
  $('#selbar').classList.toggle('show', n > 0);
}

/* ---------------- 数据 ---------------- */
async function load(showError = true) {
  if (state.refreshing) return;
  state.refreshing = true;
  try {
    const params = new URLSearchParams({
      page: state.page, size: state.size, q: state.q, state: state.stateFilter,
      instance: state.instance, sort: state.sort, order: state.order,
    });
    const [summary, list] = await Promise.all([
      api('/api/summary'),
      api(`/api/torrents?${params.toString()}`),
    ]);
    state.summary = summary;
    state.items = list.items;
    state.total = list.total;
    state.pages = list.pages;
    if (state.page > state.pages) { state.page = state.pages; state.refreshing = false; return load(showError); }
    renderPills();
    renderStats();
    renderTable();
    renderSelBar();
    loadErrGroups();
  } catch (err) {
    if (showError) toast(`读取失败：${err.message}`, true);
  } finally {
    state.refreshing = false;
  }
}

async function loadHistory() {
  try {
    const d = await api('/api/history?hours=24');
    state.history = d.items || [];
    drawSpark();
  } catch (err) { /* 忽略 */ }
}

function schedule() {
  if (state.timer) clearInterval(state.timer);
  state.timer = setInterval(() => {
    if (document.hidden) return;
    if (document.querySelector('#config-modal').classList.contains('show')) return;
    load(false);
  }, 5000);
}

/* ---------------- 操作 ---------------- */
function targetsOf(item) {
  return (item.instances || []).map((t) => ({ instance_id: t.instance_id, native_id: t.native_id }));
}

async function doAction(action, items, deleteFiles = false) {
  const targets = items.flatMap(targetsOf);
  if (!targets.length) return;
  try {
    const res = await api('/api/action', {
      method: 'POST',
      body: JSON.stringify({ action, targets, delete_files: deleteFiles }),
    });
    const failed = (res.results || []).filter((r) => !r.ok);
    if (failed.length) toast(`部分失败：${failed.map((f) => f.error).join('；').slice(0, 120)}`, true);
    else toast(`已执行 ${action}（${targets.length} 个实例任务）`);
    if (action === 'delete') {
      items.forEach((it) => state.selected.delete(it.key));
      renderSelBar();
    }
    setTimeout(() => load(false), 700);
  } catch (err) {
    toast(`操作失败：${err.message}`, true);
  }
}

function confirmDelete(items) {
  const files = $('#del-files').checked;
  const n = items.length;
  const msg = files
    ? `删除 ${n} 个种子，并删除已下载的文件？\n\n⚠ 文件会被永久删除，无法恢复。`
    : `删除 ${n} 个种子？（只删种子，保留文件）`;
  if (!window.confirm(msg)) return;
  if (files && !window.confirm(`再次确认：真的要删除 ${n} 个任务的已下载文件吗？`)) return;
  doAction('delete', items, files);
}

/* ---------------- 添加任务 ---------------- */
function openAddModal() {
  const sel = $('#add-instance');
  const insts = Object.values(state.instMap);
  if (!insts.length) { toast('先配置下载器实例', true); return; }
  sel.innerHTML = '<option value="0">自动选择（按规则）</option>'
    + insts.map((i) => `<option value="${i.id}">${esc(i.name)}（${esc(i.kind.toUpperCase())}）</option>`).join('');
  $('#add-urls').value = '';
  $('#add-file').value = '';
  $('#add-file-names').textContent = '未选择文件';
  $('#add-savepath').value = '';
  $('#add-labels').value = '';
  $('#add-paused').checked = false;
  $('#add-hint').textContent = '';
  $('#add-route-hint').textContent = state.rules.length
    ? `${state.rules.length} 条路由规则` : '未配置规则，自动时用第一个实例';
  $('#add-modal').classList.add('show');
  setTimeout(() => $('#add-urls').focus(), 60);
}

async function submitAdd(dryRun) {
  const iid = parseInt($('#add-instance').value || '0', 10);
  const urls = $('#add-urls').value.split('\n').map((x) => x.trim()).filter(Boolean);
  const files = $('#add-file').files;
  const hint = $('#add-hint');
  if (Number.isNaN(iid)) { toast('选择下载器', true); return; }

  if (files && files.length) {
    hint.textContent = `正在上传 ${files.length} 个种子文件…`;
    let ok = 0, fail = [];
    for (const file of files) {
      const form = new FormData();
      form.append('instance_id', String(iid));
      form.append('savepath', $('#add-savepath').value.trim());
      form.append('labels', $('#add-labels').value.trim());
      form.append('paused', $('#add-paused').checked ? '1' : '0');
      form.append('file', file, file.name);
      try {
        const resp = await fetch('/api/add_file', { method: 'POST', body: form });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || resp.status);
        ok += 1;
      } catch (err) { fail.push(`${file.name}: ${err.message}`); }
    }
    hint.textContent = `上传完成：成功 ${ok}${fail.length ? `，失败 ${fail.length}（${fail[0]}）` : ''}`;
    if (ok) { toast(`已添加 ${ok} 个种子`); setTimeout(() => { $('#add-modal').classList.remove('show'); load(false); }, 900); }
    return;
  }

  if (!urls.length) { toast('粘贴磁力链接或选择种子文件', true); return; }
  hint.textContent = dryRun ? '校验中…' : '正在提交…';
  try {
    const res = await api('/api/add', {
      method: 'POST',
      body: JSON.stringify({
        instance_id: iid,
        urls,
        savepath: $('#add-savepath').value.trim(),
        labels: $('#add-labels').value.trim(),
        paused: $('#add-paused').checked,
        dry_run: dryRun,
      }),
    });
    if (dryRun) {
      hint.textContent = `校验通过：${res.count} 条 → ${res.instance}${res.rule ? `（命中规则 ${res.rule}）` : ''}${res.savepath ? `，路径 ${res.savepath}` : ''}`;
    } else {
      toast(`已提交 ${res.count} 条到 ${res.instance}`);
      $('#add-modal').classList.remove('show');
      setTimeout(() => load(false), 900);
    }
  } catch (err) {
    hint.textContent = `失败：${err.message}`;
  }
}

/* ---------------- 实例设置 ---------------- */
async function openConfig() {
  $('#config-modal').classList.add('show');
  loadLimits();
  loadRules();
  loadAudit();
  await renderInstList();
  await refreshPwdState();
}

async function refreshPwdState() {
  try {
    const cfg = await api('/api/config');
    const el = $('#pwd-state');
    el.textContent = cfg.access_password_set ? '已设置' : '未设置';
    el.classList.toggle('on', !!cfg.access_password_set);
  } catch (e) { /* 未登录时忽略 */ }
}

async function savePassword(clear = false) {
  const pwd = $('#f-pwd').value.trim();
  if (!clear && !pwd) { toast('先输入新密码', true); return; }
  if (clear && !window.confirm('清除访问密码？之后任何人打开面板都不需要密码。')) return;
  try {
    await api('/api/config', { method: 'POST', body: JSON.stringify(clear ? { clear_password: true } : { access_password: pwd }) });
    $('#f-pwd').value = '';
    toast(clear ? '已清除访问密码' : '密码已保存，下次打开需要输入');
    await refreshPwdState();
  } catch (err) {
    toast(`保存失败：${err.message}`, true);
  }
}

async function renderInstList() {
  const wrap = $('#inst-list');
  try {
    const data = await api('/api/instances');
    wrap.innerHTML = '';
    data.items.forEach((inst) => {
      const row = document.createElement('div');
      row.className = 'inst-row' + (state.editingId === inst.id ? ' editing' : '');
      row.innerHTML = `
        <span class="dot" style="width:6px;height:6px;border-radius:50%;background:${inst.online ? '#46d39a' : '#f16a6a'}"></span>
        <span class="n">${esc(inst.name)}</span>
        <span class="u">${esc(inst.kind.toUpperCase())} · ${esc(inst.url)}</span>
        <span class="sp">
          <button class="btn ghost mini" data-edit="${inst.id}">编辑</button>
          <button class="btn ghost mini danger" data-del="${inst.id}">删除</button>
        </span>`;
      row.querySelector('[data-edit]').addEventListener('click', () => {
        state.editingId = inst.id;
        document.querySelectorAll('.inst-row').forEach((r) => r.classList.remove('editing'));
        row.classList.add('editing');
        $('#f-name').value = inst.name;
        $('#f-kind').value = inst.kind;
        $('#f-url').value = inst.url;
        $('#f-webui').value = inst.webui || '';
        $('#f-user').value = inst.username || '';
        $('#f-pass').value = '';
        $('#f-pass').placeholder = inst.has_password ? '留空表示不修改' : '密码';
        $('#inst-hint').textContent = `编辑：${inst.name}`;
      });
      row.querySelector('[data-del]').addEventListener('click', async () => {
        if (!window.confirm(`删除实例「${inst.name}」？（不影响下载器本身）`)) return;
        await api(`/api/instances/${inst.id}`, { method: 'DELETE' });
        toast('实例已删除');
        await renderInstList();
        load(false);
      });
      wrap.appendChild(row);
    });
  } catch (err) {
    wrap.innerHTML = `<p class="hint">读取实例失败：${esc(err.message)}</p>`;
  }
}

function resetInstForm() {
  state.editingId = null;
  ['#f-name', '#f-url', '#f-user', '#f-pass', '#f-webui'].forEach((s) => { $(s).value = ''; });
  $('#f-kind').value = 'qb';
  $('#inst-hint').textContent = '新增实例：填地址和账号后点「保存实例」';
}

/* ---------------- 事件绑定 ---------------- */
/* ---------------- 限速 ---------------- */
async function loadLimits() {
  const box = $('#limit-list');
  if (!box) return;
  box.innerHTML = '<p class="hint">读取中…</p>';
  try {
    const d = await api('/api/limits');
    state.limits = d.items || [];
    box.innerHTML = state.limits.map((i) => `
      <div class="lim-row" data-iid="${i.instance_id}">
        <div class="lim-name"><b>${esc(i.name)}</b><em>${esc(i.kind.toUpperCase())}</em>
          <span class="hint">${i.free != null ? `剩余 ${fmtBytes(i.free)}` : '剩余未知'}</span></div>
        <label>下载<input class="lim-dl" type="number" min="0" step="128" value="${i.dl || 0}"> KB/s</label>
        <label>上传<input class="lim-ul" type="number" min="0" step="128" value="${i.ul || 0}"> KB/s</label>
        <button class="btn ghost mini lim-save">保存</button>
      </div>`).join('');
    box.querySelectorAll('.lim-save').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const row = btn.closest('.lim-row');
        const iid = parseInt(row.dataset.iid, 10);
        const dl = parseInt(row.querySelector('.lim-dl').value || '0', 10);
        const ul = parseInt(row.querySelector('.lim-ul').value || '0', 10);
        btn.disabled = true;
        try {
          await api('/api/limits', { method: 'POST', body: JSON.stringify({ instance_id: iid, dl, ul }) });
          toast(`限速已更新：↓${dl || '不限'} / ↑${ul || '不限'} KB/s`);
        } catch (err) { toast(`失败：${err.message}`, true); }
        btn.disabled = false;
      });
    });
  } catch (err) {
    box.innerHTML = `<p class="hint">读取失败：${esc(err.message)}</p>`;
  }
}

/* ---------------- 规则 ---------------- */
async function loadRules() {
  const box = $('#rules-list');
  if (!box) return;
  try {
    const cfg = await api('/api/config');
    state.rules = cfg.add_rules || [];
    const insts = Object.values(state.instMap);
    const sel = $('#rule-inst');
    if (sel) sel.innerHTML = insts.map((i) => `<option value="${i.id}">${esc(i.name)}</option>`).join('');
    box.innerHTML = state.rules.length ? state.rules.map((r) => `
      <div class="rule-row">
        <code>${esc(r.match)}</code>
        <span class="arrow">→</span>
        <b>${esc((state.instMap[r.instance_id] || {}).name || `实例 ${r.instance_id}`)}</b>
        ${r.savepath ? `<span class="hint">${esc(r.savepath)}</span>` : ''}
        ${r.category ? `<span class="t-tag cat">${esc(r.category)}</span>` : ''}
        <button class="icon-btn del mini" data-rm-rule="${esc(r.match)}" title="删除规则">✕</button>
      </div>`).join('') : '<p class="hint">还没有规则，添加任务时会使用第一个实例。</p>';
    box.querySelectorAll('[data-rm-rule]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const next = state.rules.filter((r) => r.match !== btn.dataset.rmRule);
        try {
          await api('/api/config', { method: 'POST', body: JSON.stringify({ add_rules: next }) });
          toast('规则已删除');
          loadRules();
        } catch (err) { toast(`失败：${err.message}`, true); }
      });
    });
  } catch (err) {
    box.innerHTML = `<p class="hint">读取失败：${esc(err.message)}</p>`;
  }
}

async function addRule() {
  const match = $('#rule-match').value.trim();
  if (!match) { toast('填写匹配规则', true); return; }
  const rule = {
    match,
    instance_id: parseInt($('#rule-inst').value || '0', 10),
    savepath: $('#rule-path').value.trim(),
    category: $('#rule-cat').value.trim(),
  };
  try {
    const d = await api('/api/config', { method: 'POST', body: JSON.stringify({ add_rules: [...state.rules, rule] }) });
    state.rules = d.add_rules || [];
    $('#rule-match').value = ''; $('#rule-path').value = ''; $('#rule-cat').value = '';
    toast('规则已添加');
    loadRules();
  } catch (err) { toast(`失败：${err.message}`, true); }
}

/* ---------------- 审计日志 ---------------- */
const ACTION_LABEL = {
  pause: '暂停', resume: '恢复', recheck: '校验', delete: '删除', reannounce: '重新汇报',
  retry: '重试', retry_errors: '批量重试', add: '添加任务', add_file: '上传种子',
  limit: '限速', torrent_limit: '单任务限速', bulk: '批量修改', rules: '规则修改',
};
async function loadAudit() {
  const box = $('#log-list');
  if (!box) return;
  box.innerHTML = '<p class="hint">读取中…</p>';
  try {
    const d = await api('/api/audit?limit=100');
    const items = d.items || [];
    $('#log-hint').textContent = items.length ? `最近 ${items.length} 条操作` : '还没有操作记录';
    box.innerHTML = items.length ? items.map((r) => `
      <div class="log-row ${r.ok ? '' : 'bad'}">
        <time>${esc((r.ts || '').slice(5))}</time>
        <span class="act">${esc(ACTION_LABEL[r.action] || r.action)}</span>
        <span class="tg" title="${esc(r.target)}">${esc(r.target || '')}</span>
        <span class="dt" title="${esc(r.detail)}">${esc(r.detail || '')}</span>
      </div>`).join('') : '<p class="hint">还没有操作记录。</p>';
  } catch (err) {
    box.innerHTML = `<p class="hint">读取失败：${esc(err.message)}</p>`;
  }
}

/* ---------------- 详情抽屉 ---------------- */
function closeDrawer() {
  $('#drawer').classList.remove('show');
  $('#drawer-mask').classList.remove('show');
  state.detailKey = '';
}

async function openDetail(key) {
  state.detailKey = key;
  $('#drawer').classList.add('show');
  $('#drawer-mask').classList.add('show');
  $('#dr-name').textContent = '读取中…';
  $('#dr-body').innerHTML = '<p class="hint">正在从下载器读取详情…</p>';
  try {
    const d = await api(`/api/detail?key=${encodeURIComponent(key)}`);
    $('#dr-name').textContent = d.name;
    const head = `<div class="dr-meta">
      <span>${fmtBytes(d.size)}</span><span>${esc(STATE_LABEL[d.state] || d.state)}</span>
      ${d.hash ? `<span class="mono">${esc(d.hash.slice(0, 16))}…</span>` : ''}
    </div>`;
    const blocks = (d.instances || []).map((b) => {
      const trk = b.trackers.length ? b.trackers.map((t) => `
        <div class="trk ${t.status}">
          <span class="dot"></span>
          <span class="url" title="${esc(t.url)}">${esc(t.url.replace(/^https?:\/\//, '').split('/')[0])}</span>
          <span class="msg">${esc(t.msg || '')}</span>
          <span class="num">${t.peers || 0}</span>
        </div>`).join('') : '<p class="hint">没有 tracker 信息</p>';
      const files = b.files.length ? b.files.slice(0, 30).map((f) => `
        <div class="file-row">
          <span class="fn" title="${esc(f.name)}">${esc(f.name)}</span>
          <span class="fs">${fmtBytes(f.size)}</span>
          <span class="fp">${Math.round((f.progress || 0) * 100)}%</span>
        </div>`).join('') : '<p class="hint">没有文件信息</p>';
      return `
        <section class="dr-block">
          <div class="dr-block-head">
            <h4>${esc(b.instance_name)} <em>${esc(b.kind.toUpperCase())}</em></h4>
            ${b.online ? '' : `<span class="bad">离线：${esc(b.error)}</span>`}
          </div>
          ${b.online ? `
          <div class="dr-kv">
            <div><i>保存路径</i><span title="${esc(b.savepath)}">${esc(b.savepath || '—')}</span></div>
            <div><i>添加时间</i><span>${b.added ? new Date(b.added * 1000).toLocaleString('zh-CN') : '—'}</span></div>
            <div><i>连接</i><span>${b.peers} peers</span></div>
            <div><i>单任务限速</i><span>↓${b.limits.dl || '不限'} / ↑${b.limits.ul || '不限'} KB/s</span></div>
          </div>
          <div class="dr-limits">
            <label>↓<input id="dl-${b.instance_id}" type="number" min="0" step="16" value="${b.limits.dl || 0}"></label>
            <label>↑<input id="ul-${b.instance_id}" type="number" min="0" step="16" value="${b.limits.ul || 0}"></label>
            <button class="btn ghost mini" data-set-limit="${b.instance_id}" data-native="${esc(b.native_id)}">应用限速</button>
          </div>
          <h5>Tracker</h5>
          <div class="trk-list">${trk}</div>
          <h5>文件 ${b.files_total ? `（显示前 ${Math.min(30, b.files_total)} / ${b.files_total}）` : ''}</h5>
          <div class="file-list">${files}</div>` : ''}
        </section>`;
    }).join('');
    $('#dr-body').innerHTML = head + blocks;
    $('#dr-body').querySelectorAll('[data-set-limit]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const iid = Number(btn.dataset.setLimit);
        const dl = parseInt($(`#dl-${iid}`).value || '0', 10);
        const ul = parseInt($(`#ul-${iid}`).value || '0', 10);
        try {
          await api('/api/torrent_limits', {
            method: 'POST',
            body: JSON.stringify({ targets: [{ instance_id: iid, native_id: isNaN(Number(btn.dataset.native)) ? btn.dataset.native : Number(btn.dataset.native) }], dl, ul }),
          });
          toast('单任务限速已更新');
        } catch (err) { toast(`失败：${err.message}`, true); }
      });
    });
  } catch (err) {
    $('#dr-body').innerHTML = `<p class="hint">读取失败：${esc(err.message)}</p>`;
  }
}

/* ---------------- 批量分类 / 标签 ---------------- */
function openBulkMeta() {
  if (!state.selected.size) return;
  $('#bulk-category').value = '';
  $('#bulk-add-labels').value = '';
  $('#bulk-del-labels').value = '';
  $('#bulk-hint').textContent = '';
  $('#bulk-modal').classList.add('show');
}

async function applyBulkMeta() {
  const targets = [...state.selected.values()].flatMap(targetsOf);
  const category = $('#bulk-category').value.trim();
  const add = $('#bulk-add-labels').value.split(/[,，]/).map((x) => x.trim()).filter(Boolean);
  const remove = $('#bulk-del-labels').value.split(/[,，]/).map((x) => x.trim()).filter(Boolean);
  if (!category && !add.length && !remove.length) { toast('没有要修改的内容', true); return; }
  try {
    const res = await api('/api/bulk', {
      method: 'POST',
      body: JSON.stringify({ targets, category: category || null, add_labels: add, remove_labels: remove }),
    });
    $('#bulk-hint').textContent = `已修改 ${res.count} 个任务`;
    toast(`已修改 ${res.count} 个任务`);
    setTimeout(() => { $('#bulk-modal').classList.remove('show'); load(false); }, 700);
  } catch (err) {
    $('#bulk-hint').textContent = `失败：${err.message}`;
  }
}

function bind() {
  $('#q').addEventListener('input', (e) => {
    state.q = e.target.value.trim();
    state.page = 1;
    clearTimeout(state._qt);
    state._qt = setTimeout(() => load(), 250);
  });
  $('#sort').addEventListener('change', (e) => {
    const [sort, order] = e.target.value.split(':');
    state.sort = sort;
    state.order = order;
    state.page = 1;
    load();
  });
  $('#state-chips').addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    document.querySelectorAll('#state-chips .chip').forEach((c) => c.classList.toggle('active', c === chip));
    state.stateFilter = chip.dataset.state || '';
    document.querySelectorAll('.stat.clickable').forEach((x) => x.classList.toggle('on', x.dataset.filter === state.stateFilter));
    state.page = 1;
    load();
  });
  ['#btn-prev', '#btn-prev2'].forEach((sel) => $(sel)?.addEventListener('click', () => {
    if (state.page > 1) { state.page -= 1; load(); }
  }));
  ['#btn-next', '#btn-next2'].forEach((sel) => $(sel)?.addEventListener('click', () => {
    if (state.page < state.pages) { state.page += 1; load(); }
  }));
  $('#btn-first')?.addEventListener('click', () => { state.page = 1; load(); });
  $('#btn-last')?.addEventListener('click', () => { state.page = state.pages; load(); });
  $('#jump-page')?.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    const n = Math.min(state.pages, Math.max(1, parseInt(e.target.value || '1', 10)));
    state.page = n;
    load();
  });
  $('#page-size')?.addEventListener('change', (e) => {
    state.size = parseInt(e.target.value || '50', 10);
    state.page = 1;
    load();
  });

  // 统计卡当筛选器用
  document.querySelectorAll('.stat.clickable').forEach((el) => {
    el.addEventListener('click', () => {
      const want = el.dataset.filter || '';
      state.stateFilter = state.stateFilter === want ? '' : want;
      document.querySelectorAll('.stat.clickable').forEach((x) => x.classList.toggle('on', x.dataset.filter === state.stateFilter));
      document.querySelectorAll('#state-chips .chip').forEach((c) => c.classList.toggle('active', (c.dataset.state || '') === state.stateFilter));
      state.page = 1;
      load();
    });
  });
  $('#btn-refresh').addEventListener('click', () => load());

  // 打开下载器下拉
  $('#btn-open').addEventListener('click', (e) => {
    e.stopPropagation();
    $('#open-menu').classList.toggle('show');
  });
  document.addEventListener('click', () => $('#open-menu')?.classList.remove('show'));

  // 添加任务
  $('#btn-add').addEventListener('click', openAddModal);
  $('#btn-x-add').addEventListener('click', () => $('#add-modal').classList.remove('show'));
  $('#add-modal').addEventListener('click', (e) => {
    if (e.target.id === 'add-modal') $('#add-modal').classList.remove('show');
  });
  $('#add-file').addEventListener('change', (e) => {
    const files = [...e.target.files];
    $('#add-file-names').textContent = files.length
      ? files.map((f) => f.name).join('、').slice(0, 60) + (files.length > 1 ? ` 等 ${files.length} 个` : '')
      : '未选择文件';
  });
  $('#btn-add-dry').addEventListener('click', () => submitAdd(true));
  $('#btn-add-submit').addEventListener('click', () => submitAdd(false));
  $('#btn-retry-errors').addEventListener('click', async () => {
    const n = (state.summary && state.summary.counts && state.summary.counts.error) || 0;
    if (!n) return;
    if (!window.confirm(`对 ${n} 个错误任务执行「重新开始 + 汇报 tracker」？\n这不会删除任何文件。`)) return;
    const btn = $('#btn-retry-errors');
    btn.disabled = true; btn.textContent = '正在重试…';
    try {
      const res = await api('/api/retry_errors', { method: 'POST' });
      const detail = Object.entries(res.by_instance || {}).map(([k, v]) => `${k} ${v}`).join('，');
      toast(`已重试 ${res.retried} 个任务${detail ? `（${detail}）` : ''}`);
      setTimeout(() => load(false), 1200);
    } catch (err) {
      toast(`重试失败：${err.message}`, true);
    } finally {
      btn.disabled = false;
      load(false);
    }
  });

  // 行内操作
  $('#torrents tbody').addEventListener('click', (e) => {
    const title = e.target.closest('[data-detail]');
    if (title) { openDetail(title.dataset.detail); return; }
    const jump = e.target.closest('[data-jump]');
    if (jump) {
      const item = state.items.find((i) => i.key === jump.dataset.jump);
      const url = item ? jumpUrl(item) : '';
      if (url) window.open(url, '_blank', 'noreferrer');
      else toast('这个实例没有配置原生界面地址', true);
      return;
    }
    const btn = e.target.closest('.act');
    if (!btn) return;
    const item = state.items.find((i) => i.key === btn.dataset.key);
    if (!item) return;
    const act = btn.dataset.act;
    if (act === 'delete') confirmDelete([item]);
    else doAction(act, [item]);
  });

  // 全选
  $('#check-all').addEventListener('change', (e) => {
    state.items.forEach((item) => {
      if (e.target.checked) state.selected.set(item.key, item); else state.selected.delete(item.key);
    });
    document.querySelectorAll('#torrents tbody tr').forEach((tr) => {
      tr.classList.toggle('sel', e.target.checked);
      const cb = tr.querySelector('input[type=checkbox]');
      if (cb) cb.checked = e.target.checked;
    });
    renderSelBar();
  });

  $('#bulk-clear').addEventListener('click', () => {
    state.selected.clear();
    $('#check-all').checked = false;
    document.querySelectorAll('#torrents tbody input[type=checkbox]').forEach((cb) => { cb.checked = false; });
    document.querySelectorAll('#torrents tbody tr').forEach((tr) => tr.classList.remove('sel'));
    renderSelBar();
  });
  $('#bulk-pause').addEventListener('click', () => doAction('pause', [...state.selected.values()]));
  $('#bulk-resume').addEventListener('click', () => doAction('resume', [...state.selected.values()]));
  $('#bulk-recheck').addEventListener('click', () => doAction('recheck', [...state.selected.values()]));
  $('#bulk-delete').addEventListener('click', () => confirmDelete([...state.selected.values()]));

  // 设置弹窗
  $('#btn-config').addEventListener('click', openConfig);
  $('#btn-x-config').addEventListener('click', () => $('#config-modal').classList.remove('show'));
  $('#config-modal').addEventListener('click', (e) => {
    if (e.target.id === 'config-modal') $('#config-modal').classList.remove('show');
  });
  $('#btn-save-inst').addEventListener('click', async () => {
    const payload = {
      id: state.editingId,
      name: $('#f-name').value.trim(),
      kind: $('#f-kind').value,
      url: $('#f-url').value.trim(),
      username: $('#f-user').value.trim(),
      password: $('#f-pass').value,
      webui: $('#f-webui').value.trim(),
      enabled: true,
    };
    if (!payload.url) { toast('地址不能为空', true); return; }
    try {
      await api('/api/instances', { method: 'POST', body: JSON.stringify(payload) });
      toast('实例已保存');
      resetInstForm();
      await renderInstList();
      load(false);
    } catch (err) {
      toast(`保存失败：${err.message}`, true);
    }
  });
  // 设置标签页
  $('#cfg-tabs').addEventListener('click', (e) => {
    const tab = e.target.closest('.tab');
    if (!tab) return;
    document.querySelectorAll('#cfg-tabs .tab').forEach((t) => t.classList.toggle('active', t === tab));
    document.querySelectorAll('#config-modal .tabpane').forEach((pane) => {
      pane.classList.toggle('active', pane.dataset.pane === tab.dataset.tab);
    });
    if (tab.dataset.tab === 'log') loadAudit();
  });

  // 详情抽屉
  $('#btn-x-drawer').addEventListener('click', closeDrawer);
  $('#drawer-mask').addEventListener('click', closeDrawer);

  // 批量分类 / 标签
  $('#bulk-meta').addEventListener('click', openBulkMeta);
  ['#btn-x-bulk', '#btn-x-bulk2'].forEach((sel) => $(sel).addEventListener('click', () => $('#bulk-modal').classList.remove('show')));
  $('#bulk-modal').addEventListener('click', (e) => { if (e.target.id === 'bulk-modal') $('#bulk-modal').classList.remove('show'); });
  $('#btn-bulk-apply').addEventListener('click', applyBulkMeta);

  // 规则
  $('#btn-rule-add').addEventListener('click', addRule);
  $('#btn-log-refresh').addEventListener('click', loadAudit);

  // 按 tracker 重试 / 复制
  $('#err-groups').addEventListener('click', async (e) => {
    const retry = e.target.closest('[data-retry-tracker]');
    if (retry) {
      const host = retry.dataset.retryTracker;
      if (!window.confirm(`只重试 ${host} 的错误任务？`)) return;
      retry.disabled = true;
      try {
        const d = await api(`/api/retry_errors?tracker=${encodeURIComponent(host)}`, { method: 'POST' });
        toast(`已重试 ${d.retried} 个任务`);
        setTimeout(() => load(false), 900);
      } catch (err) { toast(`失败：${err.message}`, true); }
      retry.disabled = false;
      return;
    }
  });

  $('#btn-save-pwd').addEventListener('click', () => savePassword(false));
  $('#btn-clear-pwd').addEventListener('click', () => savePassword(true));
  $('#btn-test').addEventListener('click', async () => {
    const url = $('#f-url').value.trim();
    if (!url) { toast('先填地址', true); return; }
    $('#inst-hint').textContent = '正在测试连接…';
    try {
      const res = await api(`/api/instances/${state.editingId || 0}/test`, {
        method: 'POST',
        body: JSON.stringify({
          id: state.editingId, name: $('#f-name').value, kind: $('#f-kind').value,
          url, username: $('#f-user').value.trim(), password: $('#f-pass').value,
        }),
      });
      $('#inst-hint').textContent = res.ok
        ? `连接成功：${res.version}，${res.count} 个种子（${res.took_ms}ms）`
        : `连接失败：${res.error}`;
    } catch (err) {
      $('#inst-hint').textContent = `连接失败：${err.message}`;
    }
  });

  // 快捷键
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && e.target.matches?.('[data-detail]')) {
      e.preventDefault();
      openDetail(e.target.dataset.detail);
      return;
    }
    if (e.key === 'Escape') {
      closeDrawer();
      document.querySelectorAll('.modal.show').forEach((m) => m.classList.remove('show'));
    }
    if (e.key === 'Escape') $('#config-modal').classList.remove('show');
    if (e.key === '/' && document.activeElement.tagName !== 'INPUT') {
      e.preventDefault();
      $('#q').focus();
    }
  });
}

(async function init() {
  bind();
  resetInstForm();
  await load();
  schedule();
})();
