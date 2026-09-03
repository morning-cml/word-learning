/* ===========================================================================
   设置页：模型切换、Key 管理、四层检验、词表与备份状态。
   =========================================================================== */

'use strict';

import { $, html, escapeHtml, toast } from '../core.js';
import * as api from '../api.js';

let cfg = null;

const spec = (pid) => (cfg.providers || []).find((p) => p.id === pid);

const STEP_ICON = { ok: '✓', bad: '✕', idle: '·', run: '…' };

function drawSteps(steps) {
  // error / detail 里装的是模型服务端返回的错误 body，必须转义：
  // 一段带尖括号的报错原样塞进 innerHTML 会被当标签解析，把整块弄花
  html($('#steps'), steps.map((s) => {
    const state = s.state || (s.ok ? 'ok' : (s.error ? 'bad' : 'idle'));
    return `<div class="check-step">
      <div class="badge ${state}">${STEP_ICON[state]}</div>
      <div>
        <div class="check-title">${escapeHtml(s.label)}${
          s.ms ? `<span class="ms">${Number(s.ms)}ms</span>` : ''}</div>
        ${s.error ? `<div class="check-detail bad">${escapeHtml(s.error)}</div>` : ''}
        ${s.detail ? `<div class="check-detail">${escapeHtml(s.detail)}</div>` : ''}
      </div>
    </div>`;
  }).join(''));
}

function fillModels(list) {
  const p = spec($('#provider').value);
  if (!p) return;
  const models = list || p.models;
  html($('#model'), models.map((m) =>
    `<option value="${escapeHtml(m.id)}">${escapeHtml(m.id)}${
      m.label ? ' — ' + escapeHtml(m.label) : ''}</option>`).join(''));
  if (cfg.active_model && models.some((m) => m.id === cfg.active_model)) {
    $('#model').value = cfg.active_model;
  }
  const caps = p.capabilities;
  $('#modelNote').textContent =
    `${p.base_url} · JSON 模式${caps.json_object ? '支持' : '不支持'}`
    + ` · Structured Output ${caps.json_schema ? '支持' : '不支持'}`;
  showKey();
}

function showKey() {
  const p = spec($('#provider').value);
  $('#key').value = '';
  $('#key').placeholder = p.has_key
    ? `已保存 ${p.masked_key}（留空表示不改动，填空格再保存可清除）`
    : '粘贴 API Key';
  html($('#keyNote'), p.key_url
    ? 'Key 只存在本地 <code>config/settings.local.json</code>，不会进前端也不会入库。'
      + ` <a href="${escapeHtml(p.key_url)}" target="_blank" rel="noopener">去 ${escapeHtml(p.label)} 申请 Key</a>`
    : 'Key 只存在本地 <code>config/settings.local.json</code>。');
}

async function drawData() {
  const s = await api.status();
  const cefr = s.cefr || {};
  const b = s.backup || {};
  const parts = [];

  parts.push(cefr.real_data
    ? `<div class="note ok">CEFR-J 词表已就绪，${Number(cefr.size)} 词（A1–C2）。</div>`
    : `<div class="note warn">当前用的是内置兜底词表（${Number(cefr.size)} 词），难度判定较粗。
        跑一次 <code>scripts/fetch_cefr.py</code> 下载 CEFR-J 完整词表可显著提升准确度。</div>`);

  // 默默失败的备份比没有备份更糟：用户以为自己有一份，直到需要它那天才发现没有
  if (b.ok === false) {
    parts.push(`<div class="note bad" style="margin-top:10px">
      启动时备份失败：${escapeHtml(b.error)}<br>
      文章和学习状态目前没有副本，建议手动复制一份 <code>data/app.db</code>。</div>`);
  } else if (b.count) {
    parts.push(`<div class="note" style="margin-top:10px">
      已保留 ${Number(b.count)} 份数据库快照（最近：<code>${escapeHtml(b.latest)}</code>），
      放在 <code>data/backups/</code>，每次启动前自动留档。<br>
      <span class="hint">文章可以重新生成，掌握程度和累计语境不能——快照留的主要是后者。</span>
    </div>`);
  } else if (!b.protected) {
    parts.push('<div class="note" style="margin-top:10px">还没有数据可备份。生成第一篇文章后会自动留档。</div>');
  }

  // 删除前留的那条线单独说。例行快照的窗口是「最近 5 次有写入的启动」——
  // 误删一篇之后再生成 5 篇就把它挤掉了，而那正是唯一想找回来的一份。
  if (b.protected) {
    parts.push(`<div class="note ok" style="margin-top:10px">
      另有 ${Number(b.protected)} 份<b>删除前</b>快照，走单独的轮换线，
      例行快照再多也挤不掉它们。<br>
      <span class="hint">删一篇文章会连着它攒下的语境一起删，而语境是补不回来的。</span>
    </div>`);
  }
  html($('#dataInfo'), parts.join(''));
}

export async function init() {
  cfg = await api.settings.read();

  html($('#provider'), (cfg.providers || []).map((p) =>
    `<option value="${escapeHtml(p.id)}">${escapeHtml(p.label)}</option>`).join(''));
  $('#provider').value = cfg.active_provider || cfg.providers[0]?.id;
  fillModels();
  $('#provider').addEventListener('change', () => fillModels());

  $('#save').addEventListener('click', async () => {
    const pid = $('#provider').value;
    const patch = { active_provider: pid, active_model: $('#model').value, keys: {} };
    const raw = $('#key').value;
    if (raw.trim()) patch.keys[pid] = raw.trim();
    else if (raw.length) patch.keys[pid] = '';        // 全空格 = 清除
    try {
      cfg = await api.settings.save(patch);
      showKey();
      toast('已保存', 'ok');
    } catch (err) { toast(err.message, 'bad'); }
  });

  $('#refresh').addEventListener('click', async () => {
    const btn = $('#refresh');
    btn.disabled = true;
    try {
      const r = await api.settings.models($('#provider').value);
      fillModels(r.models);
      $('#modelNote').textContent += ' · ' + r.note;
    } catch (err) { toast(err.message, 'bad'); }
    btn.disabled = false;
  });

  $('#check').addEventListener('click', async () => {
    const btn = $('#check');
    $('#checkCard').hidden = false;
    btn.disabled = true;
    $('#checkSummary').textContent = '';
    drawSteps([
      { label: '连通性', state: 'run' },
      { label: 'JSON 输出', state: 'idle' },
      { label: '任务验收', state: 'idle' },
      { label: '线索审计校准', state: 'idle' },
    ]);
    const body = { provider: $('#provider').value, model: $('#model').value };
    const raw = $('#key').value.trim();
    if (raw) body.api_key = raw;
    try {
      const r = await api.settings.check(body);
      drawSteps(r.steps);
      const sum = $('#checkSummary');
      sum.style.color = r.ok ? 'var(--ok)' : 'var(--bad)';
      sum.textContent = r.ok
        ? `四层全过，这个模型可以正常干活，语境审计也判得准（本次检验用掉 ${r.tokens} tokens）。`
        : '有环节没过。上面标红的那一层就是它在你这条链路上真正的短板。';
    } catch (err) {
      $('#checkSummary').style.color = 'var(--bad)';
      $('#checkSummary').textContent = '检验请求失败：' + err.message;
    }
    btn.disabled = false;
  });

  await drawData();
}
