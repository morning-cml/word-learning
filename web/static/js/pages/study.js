/* ===========================================================================
   背单词 · 总览页：建词书 / 建单元 / 一页一页录 / 核对顺序。

   这一页的全部难点在**录入这一步不能出错**：词的顺序就是用户敲进去的顺序，
   而他敲完不会逐个核对，所以要在加进去**之前**把「认出了几个、哪几个没认出来」
   摆在他眼前（和首页那个 plan-preview 一个用意）。
   =========================================================================== */

'use strict';

import { $, $$, on, html, escapeHtml, debounce, toast } from '../core.js';
import * as api from '../api.js';

let books = [];
let units = [];
let bookId = null;

/* 选中的词书 / 单元记在 localStorage：录完一页刷新回来还停在原处，
   不用每次重选一遍书和单元——这一页是要连着录十几遍的。 */
const LAST_BOOK = 'wl-study-book';
const LAST_UNIT = 'wl-study-unit';

const remember = (key, value) => {
  try { localStorage.setItem(key, String(value)); } catch (e) { /* 隐私模式，忽略 */ }
};
const recall = (key) => {
  try { return localStorage.getItem(key); } catch (e) { return null; }
};

/* ------------------------------ 进度条 ------------------------------ */

/** 三段：已掌握 / 学习中 / 还没背。分段而不是一个百分比——
 *  「90 个词里 12 个掌握、30 个在学」和「47%」不是一回事。 */
function bar(p) {
  const total = p.total || 0;
  if (!total) return '<span class="hint">还没有词</span>';
  const pct = (n) => (100 * n / total).toFixed(1) + '%';
  return `<span class="prog" title="共 ${total} 词 · 已掌握 ${p.known} · 学习中 ${p.learning} · 未学 ${p.new}">
      <span class="prog-track">
        <i class="prog-known" style="width:${pct(p.known)}"></i>
        <i class="prog-learning" style="width:${pct(p.learning)}"></i>
      </span>
      <b>${p.known}/${total}</b>
    </span>`;
}

/* ------------------------------ 渲染 ------------------------------ */

function renderBooks() {
  html($('#bookSel'), books.map((b) =>
    `<option value="${b.id}">${escapeHtml(b.name)}（${b.total} 词）</option>`).join('')
    || '<option value="">还没有词书</option>');
  if (bookId) $('#bookSel').value = String(bookId);
  $('#empty').hidden = books.length > 0;
}

function renderUnits() {
  $('#unitsCard').hidden = !bookId || !units.length;
  //  导出链接跟着当前这本书走。切了书没跟上的话，点下去导的是上一本——
  //  而两份 CSV 长得一模一样，用户拿去对着书核才会发现，那时已经在怀疑
  //  自己是不是录错了。
  $('#exportBook').href = bookId ? `/api/wordbook/books/${bookId}/export.csv` : '#';
  html($('#unitSel'), units.map((u) =>
    `<option value="${u.id}">${escapeHtml(u.label)}</option>`).join('')
    || '<option value="">还没有单元</option>');
  const saved = recall(LAST_UNIT);
  if (saved && units.some((u) => String(u.id) === saved)) $('#unitSel').value = saved;

  html($('#units'), units.map((u) => `
    <a class="unit" href="/study/${u.id}">
      <div class="unit-head">
        <span class="unit-label">${escapeHtml(u.label)}</span>
        ${u.page_range ? `<span class="unit-pages">${escapeHtml(u.page_range)}</span>` : ''}
      </div>
      <div class="unit-meta">${u.total} 词${
        u.due ? `　<b class="unit-due">今天 ${u.due}</b>` : ''}</div>
      ${bar(u)}
    </a>`).join('') || '<div class="empty">这本书还没有单元。新建一个，比如「List 01」。</div>');
}

/* ------------------------------ 录入预览 ------------------------------ */

const refreshPreview = debounce(async () => {
  const box = $('#pastePreview');
  const text = $('#paste').value.trim();
  if (!text) { box.textContent = ''; box.className = 'hint'; return; }
  let p;
  try {
    p = await api.wordbook.preview(text);
  } catch (err) {
    box.textContent = ''; box.className = 'hint'; return;
  }
  if (!p.count) { box.textContent = '没有识别到英文单词。'; box.className = 'hint warn'; return; }
  //  没认出来的词要**逐个列出来**，不能只报个数量：用户得知道是哪几个，
  //  才判断得出是自己敲错了、还是词典没收这个词。
  //  单个词要截断：解析失败时（分隔符不是换行、整段被当成一个 token）
  //  这里会是一整段几百字的文本，原样打出来会糊满整行，反而看不出是什么问题。
  const clip = (w) => escapeHtml(w.length > 24 ? w.slice(0, 24) + '…' : w);
  const miss = p.missing.length
    ? `　字典里没有 ${p.missing.length} 个：${p.missing.slice(0, 8).map(clip).join('、')}`
      + (p.missing.length > 8 ? ' 等' : '') + '（仍然会按顺序录进去，释义留空）'
    : '';
  //  书上印的是屈折形式、字典还原到了原形，也要说一声——卡片正面显示的
  //  仍然是书上那个词形，但释义来自原形，不说的话对不上会以为是配错了
  const fell = p.sample.filter((s) => s.fallback);
  const note = fell.length
    ? `　${fell.map((s) => escapeHtml(s.headword + '→' + s.lemma)).join('、')} 按原形取的释义`
    : '';
  box.className = 'hint' + (p.missing.length ? ' warn' : '');
  box.textContent = `识别到 ${p.count} 个词，配上释义 ${p.matched} 个。${miss}${note}`;
}, 250);

/* ------------------------------ 动作 ------------------------------ */

async function loadUnits() {
  units = bookId ? (await api.wordbook.units(bookId)).units : [];
  renderUnits();
}

async function loadBooks(keepId) {
  books = (await api.wordbook.books()).books;
  const saved = keepId || recall(LAST_BOOK);
  bookId = books.some((b) => String(b.id) === String(saved))
    ? Number(saved) : (books[0]?.id ?? null);
  if (bookId) remember(LAST_BOOK, bookId);
  renderBooks();
  await loadUnits();
}

/** 展开 / 收起一个内联新建行。用它而不是 prompt()：模态框会把整个页面卡住，
 *  而且这一页要连着录十几遍，每次弹一个框很烦。 */
function toggleNew(row, input, open) {
  $(row).hidden = !open;
  if (open) { $(input).value = ''; $(input).focus(); }
}

async function newBook() {
  const name = $('#newBookName').value.trim();
  if (!name) { $('#newBookName').focus(); return; }
  try {
    const b = await api.wordbook.createBook(name, '');
    toggleNew('#newBookRow', '#newBookName', false);
    await loadBooks(b.id);
    toast(`已建「${b.name}」`, 'ok');
  } catch (err) { toast(err.message, 'bad'); }
}

async function newUnit() {
  if (!bookId) { toast('先建一本词书', 'warn'); return; }
  const label = $('#newUnitLabel').value.trim();
  if (!label) { $('#newUnitLabel').focus(); return; }
  try {
    const u = await api.wordbook.createUnit(bookId, label);
    toggleNew('#newUnitRow', '#newUnitLabel', false);
    await loadUnits();
    $('#unitSel').value = String(u.id);
    remember(LAST_UNIT, u.id);
    toast(`已建「${u.label}」`, 'ok');
    $('#paste').focus();
  } catch (err) { toast(err.message, 'bad'); }
}

async function addEntries() {
  const unitId = $('#unitSel').value;
  if (!unitId || unitId === '__new') { toast('先选一个单元', 'warn'); return; }
  const text = $('#paste').value.trim();
  if (!text) { $('#paste').focus(); return; }

  const btn = $('#addEntries');
  btn.disabled = true;
  try {
    const r = await api.wordbook.addEntries(unitId, {
      text,
      page: $('#pageNo').value.trim(),
      replace_page: $('#replacePage').checked,
    });
    //  录进去了多少、丢了什么，一次说清楚。丢的那些不能只弹个 toast
    //  ——toast 会自己消失，而「哪几个词没录进去」是他之后要回来补的。
    let msg = `已录入 ${r.added} 个词`;
    if (r.restored) msg += `（${r.restored} 个的进度接回来了）`;
    toast(msg, 'ok');
    if (r.missing.length) {
      toast(`${r.missing.length} 个词字典里没有，已按顺序录入但释义为空：`
            + r.missing.slice(0, 6).join('、'), 'warn');
    }
    //  超过一次粘贴的上限时后端只收前 500 个、**剩下的直接丢掉**，
    //  而它唯一说这件事的办法就是这个字段。不读它的话，用户看到的是
    //  「已录入 500 个词」，一个字都没提还有几百个没进去
    //  （需要注意.md 第 12b 条：算出来了但没在界面上编码，等于没算）。
    if (r.truncated) {
      toast('一次最多录 500 个词，超出的部分没有录进去——把剩下的再贴一次', 'bad');
    }
    $('#paste').value = '';
    $('#pastePreview').textContent = '';
    $('#replacePage').checked = false;
    //  页号自动 +1：录完这一页，下一步几乎一定是下一页
    const page = Number($('#pageNo').value.trim());
    if (Number.isFinite(page) && page > 0) $('#pageNo').value = String(page + 1);
    await loadUnits();
    await loadBooks(bookId);
  } catch (err) {
    toast(err.message, 'bad');
  }
  btn.disabled = false;
  $('#paste').focus();
}

/* ------------------------------ 词典状态 ------------------------------ */

async function drawDict() {
  let s;
  try { s = await api.wordbook.status(); } catch (err) { return; }
  const d = s.dictionary || {};
  $('#dictNote').textContent = d.ready ? `词典 ${Number(d.size).toLocaleString()} 词` : '';
  html($('#dictInfo'), d.ready
    ? `<div class="note ok">参考词典已就绪，${Number(d.size).toLocaleString()} 词
        （音标 / 释义 / 屈折 / 派生词）。<br>
        <span class="hint">来源 ${escapeHtml(d.source)}。书上的词根拆解和联想不在里面——
        那是你那本书的内容，录进来之后可以写进每个词的笔记。</span></div>`
    : `<div class="note bad">参考词典没找到，现在录进去的词只有词形、没有释义。<br>
        跑一次 <code>scripts/build_wordbook_dict.py</code> 生成
        <code>data/wordbook/dict.csv</code>。</div>`);
}

/* ------------------------------ 页面 ------------------------------ */

export async function init() {
  $('#bookSel').addEventListener('change', async (e) => {
    if (!e.target.value) return;
    bookId = Number(e.target.value);
    remember(LAST_BOOK, bookId);
    await loadUnits();
  });

  $('#unitSel').addEventListener('change', (e) => {
    if (e.target.value) remember(LAST_UNIT, e.target.value);
  });

  $('#newBook').addEventListener('click',
    () => toggleNew('#newBookRow', '#newBookName', $('#newBookRow').hidden));
  $('#newBookCancel').addEventListener('click',
    () => toggleNew('#newBookRow', '#newBookName', false));
  $('#newBookOk').addEventListener('click', newBook);
  $('#newBookName').addEventListener('keydown',
    (e) => { if (e.key === 'Enter') newBook(); });

  $('#newUnit').addEventListener('click',
    () => toggleNew('#newUnitRow', '#newUnitLabel', $('#newUnitRow').hidden));
  $('#newUnitCancel').addEventListener('click',
    () => toggleNew('#newUnitRow', '#newUnitLabel', false));
  $('#newUnitOk').addEventListener('click', newUnit);
  $('#newUnitLabel').addEventListener('keydown',
    (e) => { if (e.key === 'Enter') newUnit(); });

  $('#addEntries').addEventListener('click', addEntries);
  $('#paste').addEventListener('input', refreshPreview);

  //  Ctrl+Enter 提交：录入是连着做十几遍的动作，每次都去摸鼠标很烦
  $('#paste').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); addEntries(); }
  });

  on($('#units'), 'click', 'a.unit', () => { /* 让浏览器正常跳转 */ });

  drawDict();
  try {
    await loadBooks();
  } catch (err) {
    toast(err.message, 'bad');
  }
  if (!books.length) $('#empty').hidden = false;
}
