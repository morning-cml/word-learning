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
      <div class="del-warn" hidden></div>
    </td>
    <td><div class="chips">${chips}</div></td>
    <td class="num tight">${a.word_count || 0} 词</td>
    <td class="tight">${clueBar(a.clue)}</td>
    <td class="hint tight">${escapeHtml(a.model)}</td>
    <td class="tight"><button class="btn-sm btn-danger del row-act" type="button">删除</button></td>
  </tr>`;
}

/* ------------------------------ 删除的二次确认 ------------------------------

   删一篇文章会**连着它攒下的语境一起删**（Encounter 跟着 Sentence 级联走），
   而 CLAUDE.md 里那句「文章能重新生成，掌握程度和累计语境不能」说的就是它。
   原来这里是一次点击就没了，按钮平时还是隐形的（.row-act 平时 opacity: 0）——
   整个应用里最不可逆的那个动作，阻力最小，而且从没说过它会带走什么。

   所以确认这一步的重点不是「你确定吗」，是**把代价摆出来**：
   这一篇有多少处语境、其中哪几个词只在这一篇里出现过（删完就等于从没学过）。
   确认框放在标题那一格里而不是操作格：操作格是 .tight（宽度贴着内容），
   往里塞两个按钮会把整列撑宽，一行进入确认态、整张表跟着跳。 */

let armed = null;                    // 正在等确认的那一行

function disarm() {
  if (!armed) return;
  const box = armed.querySelector('.del-warn');
  if (box) { box.hidden = true; box.innerHTML = ''; }
  armed.classList.remove('arming');
  armed = null;
}

function cost(impact) {
  if (!impact) return '删掉就找不回来了（这一篇攒了多少语境没算出来）。';
  if (!impact.contexts) return '这一篇还没有攒下语境，删掉只丢文章本身。';
  const orphans = impact.orphaned || [];
  let text = `会连同 <b>${impact.contexts}</b> 处语境一起删掉`;
  if (orphans.length) {
    const shown = orphans.slice(0, 6).map(escapeHtml).join('、');
    text += `，其中 <b>${orphans.length}</b> 个词只在这一篇里出现过`
          + `（${shown}${orphans.length > 6 ? ' 等' : ''}）`;
  }
  return text + '。文章能重新生成，累计语境不能。';
}

async function load() {
  disarm();                            // 重渲染会把确认框连同它的行一起换掉
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

  //  第一步：只是问一下，顺便把这一篇到底攒了什么算出来给人看
  on($('#table'), 'click', '.del', async (e, btn) => {
    const tr = btn.closest('tr');
    if (armed === tr) return;
    disarm();
    armed = tr;
    tr.classList.add('arming');
    const box = tr.querySelector('.del-warn');
    box.hidden = false;
    box.innerHTML = '<span class="del-cost">正在算这一篇攒了多少语境…</span>';

    let impact = null;
    try {
      impact = await api.article.impact(tr.dataset.id);
    } catch (err) {
      // 算不出代价也得能删，只是说不清丢的是什么——cost() 会如实这么说
    }
    if (armed !== tr) return;          // 等接口的这会儿用户已经点了别处
    box.innerHTML = `<span class="del-cost">${cost(impact)}</span>`
      + '<button class="btn-sm btn-danger confirm-del" type="button">确认删除</button>'
      + '<button class="btn-sm cancel-del" type="button">取消</button>';
  });

  on($('#table'), 'click', '.cancel-del', () => disarm());

  //  第二步：真删
  on($('#table'), 'click', '.confirm-del', async (e, btn) => {
    const tr = btn.closest('tr');
    btn.disabled = true;
    try {
      const r = await api.article.remove(tr.dataset.id);
      disarm();
      toast(r?.backup?.made ? '已删除，删除前的库已留档' : '已删除', 'ok');
      // 备份悄悄失败比没有备份更糟：用户会以为自己还有退路
      if (r?.backup?.error) toast(`没能留下删除前的快照：${r.backup.error}`, 'bad');
      await load();
    } catch (err) {
      btn.disabled = false;
      toast(err.message, 'bad');
    }
  });

  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') disarm(); });

  await load();
}
