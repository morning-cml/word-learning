/* ===========================================================================
   词库页。

   这页是拿来验证「加一个页面要花多少功夫」的：
   后端 /api/words 早就存在、一直没有界面用它；这里没有加任何后端代码，
   前端也只写了这一个文件 —— 导航、主题、脚本装载都由 pages.py 和 main.js
   自动接上了。词条面板直接复用阅读页那个部件。
   =========================================================================== */

'use strict';

import { $, $$, on, html, escapeHtml, debounce } from '../core.js';
import * as api from '../api.js';
import { WordPanel } from '../components/reader.js';

const STATUS_TONE = { 98: 'ok', 99: '' };

let all = [];
let filter = 'all';
let query = '';

/** 掌握程度画成 5 格，比数字直观，也比进度条省地方。 */
function meter(status) {
  if (status === 98) return '<span class="tag ok">已掌握</span>';
  if (status === 99) return '<span class="tag">忽略</span>';
  const dots = [1, 2, 3, 4, 5]
    .map((i) => `<i class="${i <= status ? 'on' : ''}"></i>`).join('');
  return `<span class="meter" title="掌握程度 ${status}/5">${dots}</span>`;
}

/** CEFR 徽章。难度是这个应用的核心维度，不该和别的标签长得一样。 */
function level(cefr) {
  const v = (cefr || '').toLowerCase();
  return /^[abc][12]$/.test(v)
    ? `<span class="lv lv-${v}">${escapeHtml(cefr)}</span>`
    : '<span class="lv" title="不在 CEFR 词表里">—</span>';
}

/** 见过几次。多语境重复正是这个应用声称有效的机制，值得有个形状。 */
function seen(n) {
  const c = n || 0;
  if (c <= 1) return `<span class="count single"><b>${c}</b></span>`;
  const dots = Array.from({ length: Math.min(c, 5) },
    (_, i) => `<i class="${i < 3 ? '' : 'more'}"></i>`).join('');
  return `<span class="count" title="在 ${c} 处语境里见过"><b>${c}</b>`
       + `<span class="dots">${dots}</span></span>`;
}

function matches(w) {
  if (query) {
    const hay = (w.lemma + ' ' + (w.gloss || '')).toLowerCase();
    if (!hay.includes(query)) return false;
  }
  switch (filter) {
    case 'learning': return w.status >= 1 && w.status <= 5;
    case 'multi':    return (w.times_seen || 0) > 1;
    case 'once':     return (w.times_seen || 0) === 1;
    case 'known':    return w.status === 98;
    default:         return true;
  }
}

function render() {
  const rows = all.filter(matches);
  $('#table').hidden = rows.length === 0;
  $('#empty').hidden = rows.length > 0;
  $('#empty').textContent = all.length
    ? '没有符合条件的词。'
    : '还没有词条。去生成第一篇文章。';

  html($('#table tbody'), rows.map((w) => `
    <tr class="word-row" data-lemma="${escapeHtml(w.lemma)}">
      <td class="word-cell"><div class="lemma">${escapeHtml(w.lemma)}</div></td>
      <td class="gloss-cell">${w.gloss ? escapeHtml(w.gloss) : '<span class="hint">还没有释义</span>'}</td>
      <td class="tight">${level(w.cefr)}</td>
      <td class="tight">${seen(w.times_seen)}</td>
      <td class="tight">${meter(w.status)}</td>
      <td class="hint tight">${(w.forms || []).map(escapeHtml).join('、') || '<span class="faint">—</span>'}</td>
    </tr>`).join(''));
}

async function load() {
  const data = await api.words.list();
  all = data.words || [];
  const s = data.stats || {};
  if (s.total) {
    $('#summaryCard').hidden = false;
    html($('#summary'), `
      <div class="stat"><b>${s.total}</b><span>累计词条</span></div>
      <div class="stat ok"><b>${s.seen_multi}</b><span>在多个语境中见过</span></div>
      <div class="stat"><b>${s.seen_once}</b><span>只见过一次</span></div>`);
  }
  render();
}

export async function init() {
  const panel = new WordPanel($('#panel'), {
    load: (lemma) => api.words.detail(lemma),
    save: (lemma, status) => api.words.setStatus(lemma, status),
  });
  // 面板里改了掌握程度，列表要跟着变，否则得手动刷新才看得到
  panel.onChange = (lemma, status) => {
    const hit = all.find((w) => w.lemma === lemma);
    if (hit) { hit.status = status; render(); }
  };

  on($('#table'), 'click', 'tr[data-lemma]', (e, tr) => panel.open(tr.dataset.lemma));

  on($('#filters'), 'click', 'button[data-filter]', (e, btn) => {
    filter = btn.dataset.filter;
    $$('#filters button').forEach((b) => b.classList.toggle('on', b === btn));
    render();
  });

  $('#search').addEventListener('input', debounce((e) => {
    query = e.target.value.trim().toLowerCase();
    render();
  }, 150));

  await load();
}
