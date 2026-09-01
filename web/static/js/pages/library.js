'use strict';

import { $, on, html, escapeHtml, fmtTime, toast } from '../core.js';
import * as api from '../api.js';

function row(a) {
  const words = (a.target_words || []);
  const chips = words.slice(0, 6).map((w) => `<span class="chip">${escapeHtml(w)}</span>`).join('')
    + (words.length > 6 ? `<span class="chip">+${words.length - 6}</span>` : '');
  return `<tr data-id="${a.id}">
    <td>
      <a href="/read/${a.id}">${escapeHtml(a.title_en || '（无标题）')}</a>
      <div class="hint">${escapeHtml(a.title_zh || '')}${a.genre ? ' · ' + escapeHtml(a.genre) : ''}</div>
      <div class="hint" style="font-size:12px">${escapeHtml(fmtTime(a.created_at))}</div>
    </td>
    <td><div class="chips">${chips}</div></td>
    <td class="num tight">${a.word_count || 0} 词<div class="hint">${escapeHtml(a.level)}</div></td>
    <td class="num tight">${a.clue || '—'}</td>
    <td class="hint tight">${escapeHtml(a.model)}</td>
    <td class="tight"><button class="btn-sm btn-danger del" type="button">删除</button></td>
  </tr>`;
}

async function load() {
  const { articles } = await api.article.list();
  $('#empty').hidden = articles.length > 0;
  $('#table').hidden = articles.length === 0;
  html($('#table tbody'), articles.map(row).join(''));

  const { stats } = await api.words.list();
  if (stats.total) {
    $('#summaryCard').hidden = false;
    html($('#summary'), `
      <div class="stat"><b>${articles.length}</b><span>篇文章</span></div>
      <div class="stat"><b>${stats.total}</b><span>累计词条</span></div>
      <div class="stat ok"><b>${stats.seen_multi}</b><span>在多个语境中见过</span></div>
      <div class="stat"><b>${stats.seen_once}</b><span>只见过一次</span></div>`);
  }
}

export async function init() {
  // 双击整行打开，和点标题等价——桌面版是双击，这里保持一致
  on($('#table'), 'dblclick', 'tr[data-id]', (e, tr) => {
    if (!e.target.closest('button, a')) location.href = `/read/${tr.dataset.id}`;
  });

  on($('#table'), 'click', '.del', async (e, btn) => {
    const tr = btn.closest('tr');
    btn.disabled = true;
    try {
      await api.article.remove(tr.dataset.id);
      toast('已删除', 'ok');
      await load();
    } catch (err) {
      btn.disabled = false;
      toast(err.message, 'bad');
    }
  });
  await load();
}
