/* ===========================================================================
   背单词 · 背诵页。

   两个视图，各管一件事：
     卡片 —— 背。翻面前只有词和音标，逼你先自己检索（和阅读页的回忆模式同一个
             理由：光看不算记住）。
     词表 —— **对着书核顺序**。这是「和纸质版一致」唯一的验收手段，
             所以序号、页号都摆出来，而且顺序严格按后端给的来，前端一处都不排。

   翻面之后除了释义，还会告诉你这个词在生成的文章里读到过几次——背单词和读文章
   共用同一份学习状态，那是这个应用和一个普通背单词 App 的区别。
   =========================================================================== */

'use strict';

import { $, $$, on, html, escapeHtml, toast } from '../core.js';
import * as api from '../api.js';
import { WordPanel } from '../components/reader.js';

let entries = [];        // 整个单元，顺序 = 书上的顺序，**不排序**
let queue = [];          // 本轮还要背的（entry 引用）
let current = null;
let flipped = false;
let view = 'card';
let overdue = false;      // 是不是「不管到期，按书再过一遍」那种轮次
let pageFilter = '';
let unitId = null;
const seenCache = new Map();   // lemma -> 词库详情 | null
let inLibrary = new Set();     // 词库里有哪些词，开页时取一次（见 showSeen）

/* ------------------------------ 工具 ------------------------------ */

const visible = () => entries.filter((e) => !pageFilter || String(e.page) === pageFilter);

/** 本轮要背的。两道筛子，顺序有讲究：
 *
 *  1. 已掌握(98) 和忽略(99) 不进——它们出师了，再问一遍是浪费时间；
 *  2. **没到期的不进**（间隔重复）。
 *
 *  第 2 条和这一页唯一的承诺「顺序 == 纸质书」是有张力的，解法是：
 *  **只过滤，不重排**。entries 本来就是书序，filter 不动顺序，所以过一遍
 *  到期的词仍然是从书的前面往后走，只是跳过了今天不用看的。
 *  词表视图完全不受影响，那边永远是整本书的完整顺序。
 *
 *  ignoreDue 是留给用户的退路：「今天没到期，但我就是想再过一遍这一单元」。
 *  没有这条的话，间隔重复等于把「对着书从头背一遍」这个用法拿走了——
 *  而那正是他买那本书的理由。
 */
const buildQueue = (ignoreDue) =>
  visible().filter((e) => e.status < 98 && (ignoreDue || e.due !== false));

/** 还没背熟、但今天不用看的。空状态要用它把话说清楚。 */
const notDue = () => visible().filter((e) => e.status < 98 && e.due === false).length;

function renderProgress() {
  const rows = visible();
  const known = rows.filter((e) => e.status >= 98).length;
  const left = queue.length;
  $('#progress').textContent = rows.length
    ? `已掌握 ${known}/${rows.length}　${overdue ? '本轮还剩' : '今天还剩'} ${left}`
    : '这一页还没有词';
}

/* ------------------------------ 卡片 ------------------------------ */

function showCard() {
  const done = $('#done');
  const card = $('#card');
  if (!current) {
    card.hidden = true;
    done.hidden = false;
    const rows = visible();
    const later = notDue();
    const mastered = rows.filter((e) => e.status >= 98).length;
    //  「今天过完了」和「这一单元背完了」是两件完全不同的事，不能说成一句。
    //  说成一句的话，用户看到「过完了」会以为整本背完，而其实只是今天没到期的。
    $('#doneTitle').textContent = !rows.length ? '这一页还没有词'
      : later ? '今天该复习的过完了' : '这一单元过完了';
    $('#doneNote').textContent = !rows.length ? '先去「背单词」页把这一页录进来。'
      : later ? `还有 ${later} 个词没到复习时间——间隔重复会挑日子，隔几天再问一遍记得更牢。`
      : `${rows.length} 个词里，${mastered} 个已经掌握。`;
    //  退路。没有它，间隔重复等于把「对着书从头背一遍」这个用法拿走了。
    $('#again').hidden = !rows.length;
    $('#again').textContent = later ? `不管到期，按书再过一遍（${later + 0}）` : '再过一遍';
    return;
  }
  done.hidden = true;
  card.hidden = false;
  flipped = false;

  $('#word').textContent = current.headword;
  $('#phonetic').textContent = current.phonetic ? `[${current.phonetic}]` : '';
  $('#cardPage').textContent = current.page ? `P${current.page}` : '';
  $('#back').hidden = true;
  $('#judge').hidden = true;
  $('#flip').hidden = false;
  $('#seen').hidden = true;
}

function renderBack() {
  const e = current;
  $('#translation').textContent = e.translation || '（词典里没有这个词，释义空着）';

  const bits = [];
  if (e.inflections?.length) {
    bits.push('<div class="forms-row"><span class="forms-key">变形</span>'
      + e.inflections.map((i) =>
        `<span class="form"><i>${escapeHtml(i.label)}</i>${escapeHtml(i.word)}</span>`).join('')
      + '</div>');
  }
  if (e.derivatives?.length) {
    bits.push('<div class="forms-row"><span class="forms-key">派生</span>'
      + e.derivatives.map((d) => `<span class="form">${escapeHtml(d)}</span>`).join('')
      + '</div>');
  }
  html($('#forms'), bits.join(''));
  html($('#note'), e.note
    ? `<div class="drill-usernote">${escapeHtml(e.note)}</div>` : '');
}

/** 这个词在生成的文章里读到过没有。查不到就什么都不显示——
 *  没读到过是常态，为它留一行「0 次」只会让每张卡片都多一行噪声。
 *
 *  先查本地那份词库清单再决定要不要发请求：**没读到过才是常态**，
 *  直接发请求的话绝大多数翻面都收一个 404，控制台里堆一串红色的
 *  「Failed to load resource」——功能上被 catch 吞掉了，但那串报错会让
 *  以后排别的问题的人以为这里坏了。顺带也省掉每翻一张卡一个请求。 */
async function showSeen(entry) {
  const key = entry.lemma || entry.headword;
  if (!inLibrary.has(key.toLowerCase())) return;
  if (!seenCache.has(key)) {
    try { seenCache.set(key, await api.words.detail(key)); }
    catch (err) { seenCache.set(key, null); }
  }
  const w = seenCache.get(key);
  if (!w || !w.contexts?.length || current !== entry) return;
  const strong = w.contexts.filter((c) => c.clue_strength === 'strong').length;
  html($('#seen'),
    `<button type="button" class="seen-link" data-lemma="${escapeHtml(key)}">
       文章里读到过 ${w.contexts.length} 处${strong ? `，${strong} 处线索充分` : ''} →
     </button>`);
  $('#seen').hidden = false;
}

function flip() {
  if (!current || flipped) return;
  flipped = true;
  renderBack();
  $('#back').hidden = false;
  $('#flip').hidden = true;
  $('#judge').hidden = false;
  showSeen(current);
}

async function judge(known) {
  if (!current) return;
  const entry = current;
  //  卡片先翻过去，不等接口回来：这个动作一分钟要做几十次，每次等一个来回
  //  都会明显卡顿。**但掌握程度不在这里推**——档位规则只在后端一处
  //  （store.record_review），前端照着算一遍就是同一件事写两遍，以后换成
  //  遗忘曲线时这里会悄悄和它对不上（需要注意.md 第 20 条）。
  //  所以本地只动队列，档位等接口回来照抄。
  queue.shift();
  if (!known) queue.push(entry);       // 没答上来的，本轮末尾再见一次
  current = queue[0] || null;
  showCard();
  renderProgress();
  try {
    const got = await api.wordbook.review(entry.id, known, 'flip');
    Object.assign(entry, got);
    renderProgress();
    if (view === 'list') renderList();
  } catch (err) {
    //  没记上就得把它放回队列。
    //
    //  原来这里是 `Object.assign(entry, before)`，而 before 是发请求**之前**
    //  照着 entry 拍的快照——中间没有任何东西改过 entry（档位是后端推的），
    //  所以那句话是个空操作，回滚的是一份没变过的值。真正丢掉的是**队列里的
    //  位置**：答「认识」的那张已经被 shift 出去了，这一轮再也不会出现，
    //  于是这次判定既没落库、也不会重问，而进度条上它还算「学习中」。
    //  toast 会自己消失，两三秒后就没有任何痕迹说明这个词被跳过了。
    if (!queue.includes(entry)) queue.push(entry);
    if (!current) { current = queue[0]; showCard(); }
    renderProgress();
    toast('这一次没记上，本轮结束前会再问一遍：' + err.message, 'bad');
  }
}

/* ------------------------------ 词表 ------------------------------ */

function renderList() {
  html($('#entryTable tbody'), visible().map((e, i) => `
    <tr data-id="${e.id}" data-level="">
      <td class="num tight">${i + 1}</td>
      <td class="tight">${e.page ?? ''}</td>
      <td class="tight"><b class="entry-word">${escapeHtml(e.headword)}</b></td>
      <td class="tight hint">${escapeHtml(e.phonetic)}</td>
      <td>${escapeHtml(e.translation)
            || (e.extra?.no_dict
                ? '<span class="need-fill">词典里没有这个词，点「改」自己补</span>'
                : '<span class="hint">—</span>')}
        ${e.note ? `<div class="hint entry-note">${escapeHtml(e.note)}</div>` : ''}</td>
      <td class="tight"><span class="tag ${e.status >= 98 ? 'ok' : ''}">${escapeHtml(e.status_label)}</span></td>
      <td class="tight"><button class="btn-sm row-act edit" type="button">改</button></td>
    </tr>`).join('') || '<tr><td colspan="7" class="empty">这一页还没有词</td></tr>');
}

function openEditor(tr, entry) {
  if (tr.nextElementSibling?.classList.contains('editor-row')) {
    tr.nextElementSibling.remove();
    return;
  }
  $$('.editor-row').forEach((r) => r.remove());
  const row = document.createElement('tr');
  row.className = 'editor-row';
  row.innerHTML = `<td colspan="7"><div class="entry-editor">
      <label class="field">释义（书上的说法和字典不一样时，以书为准）</label>
      <textarea class="ed-translation" rows="2">${escapeHtml(entry.translation)}</textarea>
      <label class="field">笔记 —— 书上的词根拆解、联想、例句写这里</label>
      <textarea class="ed-note" rows="3">${escapeHtml(entry.note)}</textarea>
      <div class="row">
        <div style="width:110px">
          <label class="field">页号</label>
          <input type="text" class="ed-page" value="${entry.page ?? ''}">
        </div>
        <span class="spacer"></span>
        <button class="btn-sm ed-cancel" type="button">取消</button>
        <button class="btn-sm btn-primary ed-save" type="button">保存</button>
      </div>
    </div></td>`;
  tr.after(row);
  row.querySelector('.ed-translation').focus();

  row.querySelector('.ed-cancel').addEventListener('click', () => row.remove());
  row.querySelector('.ed-save').addEventListener('click', async () => {
    try {
      const got = await api.wordbook.updateEntry(entry.id, {
        translation: row.querySelector('.ed-translation').value,
        note: row.querySelector('.ed-note').value,
        page: row.querySelector('.ed-page').value,
      });
      Object.assign(entry, got);
      row.remove();
      renderList();
      toast('已保存', 'ok');
    } catch (err) { toast(err.message, 'bad'); }
  });
}

/* ------------------------------ 视图切换 ------------------------------ */

function setView(next) {
  view = next;
  $$('#views button').forEach((b) => b.classList.toggle('on', b.dataset.view === next));
  $('#cardView').hidden = next !== 'card';
  $('#listView').hidden = next !== 'list';
  if (next === 'list') renderList();
}

function renderPageFilter() {
  const pages = [...new Set(entries.map((e) => e.page).filter((p) => p != null))].sort((a, b) => a - b);
  html($('#pageFilter'),
    `<option value="">全部 ${entries.length} 词</option>`
    + pages.map((p) => `<option value="${p}">P${p}</option>`).join(''));
  $('#pageFilter').hidden = pages.length < 2;
}

function restart(ignoreDue = false) {
  overdue = !!ignoreDue;
  queue = buildQueue(overdue);
  current = queue[0] || null;
  showCard();
  renderProgress();
}

/* ------------------------------ 页面 ------------------------------ */

export async function init(dataset) {
  unitId = Number(dataset.unitId);

  const panel = new WordPanel($('#panel'), {
    load: (lemma) => api.words.detail(lemma),
    save: (lemma, status) => api.words.setStatus(lemma, status),
  });

  try {
    entries = (await api.wordbook.entries(unitId)).entries;
  } catch (err) {
    html($('#cardView'), `<p class="empty">${escapeHtml(err.message)}</p>`);
    return;
  }

  //  词库清单取一次就够（见 showSeen）。拿不到也不影响背单词，
  //  只是卡片上不再显示「文章里读到过」那一行。
  try {
    const lib = await api.words.list();
    inLibrary = new Set((lib.words || []).map((w) => String(w.lemma).toLowerCase()));
  } catch (err) { inLibrary = new Set(); }

  //  面包屑：背到一半抬头要看得见「我在哪本书的哪个单元」
  $('#where').textContent = entries.length && entries[0].page
    ? `${entries.length} 词 · P${entries[0].page}起` : `${entries.length} 词`;
  document.title = '背诵 · Word Learning';

  renderPageFilter();
  restart();

  $('#flip').addEventListener('click', flip);
  $('#yes').addEventListener('click', () => judge(true));
  $('#no').addEventListener('click', () => judge(false));
  //  这个按钮现在明说自己干什么：绕开到期，按书再过一遍
  $('#again').addEventListener('click', () => restart(true));

  $('#views').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-view]');
    if (btn) setView(btn.dataset.view);
  });

  $('#pageFilter').addEventListener('change', (e) => {
    pageFilter = e.target.value;
    restart();
    if (view === 'list') renderList();
  });

  on($('#entryTable'), 'click', '.edit', (e, btn) => {
    const tr = btn.closest('tr');
    const entry = entries.find((x) => String(x.id) === tr.dataset.id);
    if (entry) openEditor(tr, entry);
  });

  on($('#seen'), 'click', '.seen-link', (e, btn) => panel.open(btn.dataset.lemma));

  //  键盘：空格翻面，左右手判。背单词是个高频重复动作，
  //  全程不碰鼠标才跟得上思路（阅读页的回忆模式也是这么做的）。
  document.addEventListener('keydown', (e) => {
    if (e.target.matches?.('input, textarea') || e.metaKey || e.ctrlKey) return;
    if ($('#panel').classList.contains('open')) return;
    if (view !== 'card') return;
    if (e.code === 'Space' || e.key === 'Enter') {
      e.preventDefault();
      if (!flipped) flip();
      else judge(true);
    } else if (e.key === 'ArrowRight') {
      e.preventDefault();
      if (flipped) judge(true);
    } else if (e.key === 'ArrowLeft') {
      e.preventDefault();
      if (flipped) judge(false);
    }
  });
}
