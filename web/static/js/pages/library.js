'use strict';

import { $, on, html, escapeHtml, fmtTime, toast } from '../core.js';
import * as api from '../api.js';

function lvBadge(level) {
  const v = (level || '').toLowerCase();
  return /^[abc][12]$/.test(v) ? `<span class="lv lv-${v}">${escapeHtml(level)}</span>` : '';
}

/** 线索比画成分段条。
 *
 * 「N 个目标词里有几个语境线索充分」是这个产品唯一的价值指标，
 * 原先它是一个裸的 "3/5"，和旁边的「178 词」看起来一样重，
 * 扫一列根本分不出哪几篇是白读的。
 *
 * 老文章没存过这个字段，要和「测过、但一格都不达标」区分开——
 * 画成全空会读成后者，而那是两件完全不同的事。 */
function clueBar(clue) {
  const m = /^(\d+)\/(\d+)$/.exec(clue || '');
  if (!m) return '<span class="clue-none" title="这篇生成时还没有线索审计">—</span>';
  const strong = +m[1], total = +m[2];
  const segs = Array.from({ length: total },
    (_, i) => `<i class="${i < strong ? 'on' : 'miss'}"></i>`).join('');
  return `<span class="clue-bar${strong === total ? ' full' : ''}"`
       + ` title="${total} 个目标词里 ${strong} 个语境线索充分">`
       + `<span class="clue-seg">${segs}</span><b>${strong}/${total}</b></span>`;
}

function row(a) {
  const words = (a.target_words || []);
  const chips = words.slice(0, 6).map((w) => `<span class="chip">${escapeHtml(w)}</span>`).join('')
    + (words.length > 6 ? `<span class="chip">+${words.length - 6}</span>` : '');
  return `<tr data-id="${a.id}">
    <td>
      <a href="/read/${a.id}">${escapeHtml(a.title_en || '（无标题）')}</a>
      <div class="hint">${escapeHtml(a.title_zh || '')}${a.genre ? ' · ' + escapeHtml(a.genre) : ''}</div>
      <div class="row-meta">${lvBadge(a.level)}<span class="row-time">${escapeHtml(fmtTime(a.created_at))}</span></div>
    </td>
    <td><div class="chips">${chips}</div></td>
    <td class="num tight">${a.word_count || 0} 词</td>
    <td class="tight">${clueBar(a.clue)}</td>
    <td class="hint tight">${escapeHtml(a.model)}</td>
    <td class="tight"><button class="btn-sm btn-danger del row-act" type="button">删除</button></td>
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
