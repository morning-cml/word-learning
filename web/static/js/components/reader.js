/* ===========================================================================
   双语阅读器 + 回忆模式 + 词条面板。

   三个设计前提：

   1. 中英对齐在生成阶段就定死了（每句一条 {en, zh}），这里只渲染不切句——
      英文句号切分必然在 Mr. / U.S. / 引号内句号上翻车。

   2. 覆盖切换最怕「页面往上跳」：中文比英文短，一换行数就变，读到一半位置就丢。
      两道保险：渲染后把每段 min-height 锁到中英两种排版里较高的一侧（从根上
      不产生高度变化）；外加切换时的滚动锚定兜底。

   3. 光看不算记住。回忆模式把目标词挖空，逼你先检索再验证——
      主动检索对留存率的提升远大于重复阅读。

   放在 components/ 是因为它是个可复用部件：给它一个容器和文档数据就能跑，
   不依赖任何页面结构。将来做「对照预览」之类的新页面可以直接拿来用。
   =========================================================================== */

'use strict';

import { escapeHtml, escapeRe, $$ } from '../core.js';

export const MODES = ['en', 'zh', 'both', 'cloze'];

export const MODE_HINTS = {
  en:    '点句子翻译该句 · 点生词看全部语境',
  zh:    '再点一次该句可切回英文',
  both:  '译文跟在原文后，原文不消失',
  cloze: '先自己回忆，点空格揭示答案 · 卡住可点句子看中文',
};

/* 找出目标词在句中的位置。按 surface 做词边界匹配自己算——
   不用模型给的字符偏移量，模型数字符位置经常错位。 */
function findRanges(text, targets) {
  const ranges = [];
  for (const t of targets || []) {
    const surface = (t.surface || '').trim();
    if (!surface) continue;
    const re = new RegExp(`\\b${escapeRe(surface)}\\b`, 'gi');
    let m;
    while ((m = re.exec(text)) !== null) {
      ranges.push({ start: m.index, end: m.index + m[0].length, lemma: t.lemma });
      if (m.index === re.lastIndex) re.lastIndex++;
    }
  }
  ranges.sort((a, b) => a.start - b.start || b.end - a.end);
  const merged = [];
  for (const r of ranges) {
    const last = merged[merged.length - 1];
    if (last && r.start < last.end) continue;
    merged.push(r);
  }
  return merged;
}

/* 每个目标词渲染成两层：明文 + 挖空。切模式时只换 CSS，不重新渲染，
   这样「已揭示」的状态在模式之间切换时不会丢。 */
function renderTarget(word, lemma) {
  const blank = '_'.repeat(Math.max(word.length, 3));
  return `<span class="tw" data-lemma="${escapeHtml(lemma || '')}" tabindex="0" role="button">`
       + `<span class="tw-word">${escapeHtml(word)}</span>`
       + `<span class="tw-blank" aria-hidden="true">${blank}</span>`
       + `</span>`;
}

function renderEnglish(text, targets) {
  const ranges = findRanges(text, targets);
  if (!ranges.length) return escapeHtml(text);
  let out = '', cursor = 0;
  for (const r of ranges) {
    out += escapeHtml(text.slice(cursor, r.start));
    out += renderTarget(text.slice(r.start, r.end), r.lemma);
    cursor = r.end;
  }
  return out + escapeHtml(text.slice(cursor));
}

export class Reader {
  constructor(root, { onWord } = {}) {
    this.root = root;
    this.mode = 'en';
    this.onWord = onWord;
    this.doc = null;

    root.addEventListener('click', (e) => {
      const tw = e.target.closest('.tw');
      if (tw) {
        e.stopPropagation();
        if (this.mode === 'cloze' && !tw.classList.contains('revealed')) {
          this.reveal(tw);                    // 挖空状态下第一下只揭示，不开面板
        } else if (this.onWord) {
          this.onWord(tw.dataset.lemma, tw);
        }
        return;
      }
      const s = e.target.closest('.s');
      if (s && this.mode !== 'both') this.toggleSentence(s);
    });

    root.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      const tw = e.target.closest('.tw');
      if (!tw) return;
      e.preventDefault();
      // 必须连传播一起停掉。页面上还有一个「空格 = 中英互切」的全局监听器，
      // 光 preventDefault 挡不住冒泡：焦点落在挖空词上按一下空格，
      // 词是揭示了，同一下按键又把整个阅读器踢出了回忆模式。
      e.stopPropagation();
      if (this.mode === 'cloze' && !tw.classList.contains('revealed')) this.reveal(tw);
      else if (this.onWord) this.onWord(tw.dataset.lemma, tw);
    });

    let timer;
    window.addEventListener('resize', () => {
      clearTimeout(timer);
      timer = setTimeout(() => this.lockHeights(), 200);
    });
  }

  render(doc) {
    this.doc = doc;
    const paras = (doc.paragraphs || []).map((p) => {
      const sentences = (p.sentences || []).map((s) => {
        const en = renderEnglish(s.en || '', s.targets);
        const zh = escapeHtml(s.zh || '');
        return `<span class="s"><span class="en">${en}</span><span class="zh">${zh}</span></span> `;
      }).join('');
      return `<p class="para">${sentences}</p>`;
    }).join('');

    const title = doc.title_en
      ? `<h1>${escapeHtml(doc.title_en)}</h1><div class="doc-sub">${escapeHtml(doc.title_zh || '')}`
        + `${doc.genre ? ' · ' + escapeHtml(doc.genre) : ''}</div>`
      : '';

    this.root.innerHTML = title + paras;
    this.setMode('en');
    requestAnimationFrame(() => this.lockHeights());
  }

  reveal(el) {
    el.classList.add('revealed', 'just-revealed');
    setTimeout(() => el.classList.remove('just-revealed'), 700);
  }

  revealAll(on) {
    $$('.tw', this.root).forEach((el) => el.classList.toggle('revealed', on));
  }

  get clozeProgress() {
    return {
      total: $$('.tw', this.root).length,
      revealed: $$('.tw.revealed', this.root).length,
    };
  }

  /* 把每段高度锁到中英两种排版的较大值——切换时高度不变，页面就不会跳。 */
  lockHeights() {
    const paras = $$('.para', this.root);
    if (!paras.length) return;
    const sentences = $$('.s', this.root);
    const wasOn = sentences.map((s) => s.classList.contains('zh-on'));
    const cls = this.root.className;

    paras.forEach((p) => (p.style.minHeight = ''));
    this.root.className = 'doc mode-en';
    sentences.forEach((s) => s.classList.remove('zh-on'));
    const enH = paras.map((p) => p.offsetHeight);

    sentences.forEach((s) => s.classList.add('zh-on'));
    const zhH = paras.map((p) => p.offsetHeight);

    paras.forEach((p, i) => (p.style.minHeight = Math.max(enH[i], zhH[i]) + 'px'));

    sentences.forEach((s, i) => s.classList.toggle('zh-on', wasOn[i]));
    this.root.className = cls;
  }

  /* 滚动锚定：记住视口顶部那一段，切换后把它钉回原来的屏幕位置。 */
  withAnchor(fn) {
    const paras = $$('.para', this.root);
    const anchor = paras.find((p) => p.getBoundingClientRect().bottom > 80) || paras[0];
    const before = anchor ? anchor.getBoundingClientRect().top : 0;
    fn();
    if (anchor) {
      const after = anchor.getBoundingClientRect().top;
      if (Math.abs(after - before) > 1) window.scrollBy(0, after - before);
    }
  }

  setMode(mode) {
    if (!MODES.includes(mode)) return;
    this.withAnchor(() => {
      this.mode = mode;
      const marks = this.root.classList.contains('hide-marks') ? ' hide-marks' : '';
      this.root.className = 'doc mode-' + mode + marks;
      // 挖空模式下不该同时显示中文——那等于把答案摆在旁边
      const zhOn = mode === 'zh';
      $$('.s', this.root).forEach((s) => s.classList.toggle('zh-on', zhOn));
    });
  }

  /* 单句切换：真实的学习动作是「读到卡住的那一句才想看中文」。 */
  toggleSentence(el) {
    this.withAnchor(() => el.classList.toggle('zh-on'));
  }

  toggleMarks() {
    this.root.classList.toggle('hide-marks');
    return !this.root.classList.contains('hide-marks');
  }
}

/* ------------------------------ 词条面板 ------------------------------ */

/** CEFR 徽章。难度是这个应用的核心维度，不该和「见过 N 次」长得一样。 */
function lvBadge(cefr) {
  const v = (cefr || '').toLowerCase();
  return /^[abc][12]$/.test(v) ? `<span class="lv lv-${v}">${escapeHtml(cefr)}</span>` : '';
}

const STRENGTH = {
  strong: { label: '线索充分', cls: 'ok' },
  weak:   { label: '线索偏弱', cls: 'warn' },
  none:   { label: '无线索',   cls: 'bad' },
};

/* 掌握程度热键。借自 Lute：它把光标放到词上按 1-5 / W / I，
   省掉「点词 → 开面板 → 点 chip」这三步里的后两步。
   这里换成「面板开着 = 已经选中了这个词」当判定条件——不用追踪悬停在谁身上，
   也就不会出现「想切模式却改了某个词的掌握程度」这种误伤。
   数字键和阅读页的模式切换撞车，让路逻辑写在 pages/reader.js 里。 */
const HOTKEYS = { '1': 1, '2': 2, '3': 3, '4': 4, '5': 5, 'w': 98, 'i': 99 };

export const STATUSES = [
  [1, '刚认识'], [2, '有印象'], [3, '较熟'], [4, '很熟'], [5, '接近掌握'],
  [98, '已掌握'], [99, '忽略'],
];

export class WordPanel {
  /** @param {(lemma:string)=>Promise<object>} load 取词条详情
   *  @param {(lemma:string,status:number)=>Promise<any>} save 改掌握程度 */
  constructor(el, { load, save } = {}) {
    this.el = el;
    this.lemma = null;
    this.load = load;
    this.save = save;
    this.onChange = null;      // 改了掌握程度后通知外部刷新（词库页要用）

    el.addEventListener('click', (e) => {
      if (e.target.closest('.panel-close')) this.close();
      const btn = e.target.closest('button[data-status]');
      if (btn) this.setStatus(Number(btn.dataset.status));
    });
    document.addEventListener('keydown', (e) => {
      if (!this.el.classList.contains('open')) return;
      if (e.key === 'Escape') { this.close(); return; }
      if (!this.lemma || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.target.closest && e.target.closest('input, textarea, select')) return;
      const status = HOTKEYS[e.key.toLowerCase()];
      if (status === undefined) return;
      e.preventDefault();
      this.setStatus(status);
    });
  }

  close() {
    this.el.classList.remove('open');
    // 正文靠这个类让位。放在这里而不是调用方，是因为面板有三条关闭路径
    // （按钮、Esc、词库页切换），漏掉任何一条正文就永远偏在一边。
    document.body.classList.remove('panel-open');
    this.lemma = null;
  }

  async open(lemma) {
    if (!lemma) return;
    this.lemma = lemma;
    this.el.classList.add('open');
    document.body.classList.add('panel-open');
    this.el.innerHTML = '<div class="panel-inner"><div class="empty">加载中…</div></div>';
    try {
      this.draw(await this.load(lemma));
    } catch (err) {
      this.el.innerHTML = `<div class="panel-inner"><button class="panel-close">×</button>`
        + `<div class="empty">${escapeHtml(err.message || '读取失败')}</div></div>`;
    }
  }

  async setStatus(status) {
    if (!this.lemma) return;
    const lemma = this.lemma;
    try {
      await this.save(lemma, status);
      this.onChange?.(lemma, status);
      this.open(lemma);
    } catch (err) {
      this.el.querySelector('.status-row')?.insertAdjacentHTML(
        'afterend', `<div class="note bad" style="margin-top:8px">${escapeHtml(err.message)}</div>`);
    }
  }

  draw(w) {
    const ctxs = w.contexts || [];
    const strong = ctxs.filter((c) => c.clue_strength === 'strong').length;

    const contexts = ctxs.map((c) => {
      const st = STRENGTH[c.clue_strength] || null;
      const tone = st ? ' ctx-' + st.cls : '';
      const en = escapeHtml(c.en).replace(
        new RegExp(`\\b${escapeRe(c.surface || w.lemma)}\\b`, 'i'), (m) => `<b>${m}</b>`);
      return `<div class="ctx${tone}">
        <div class="ctx-head">
          <a href="/read/${c.article_id}">${escapeHtml(c.article_title || '未命名')}</a>
          ${st ? `<span class="tag ${st.cls}">${st.label}</span>` : ''}
        </div>
        <div class="ctx-en">${en}</div>
        <div class="ctx-zh">${escapeHtml(c.zh || '')}</div>
        ${c.clue ? `<div class="ctx-clue">线索：${escapeHtml(c.clue)}</div>` : ''}
      </div>`;
    }).join('') || '<div class="empty">还没有语境</div>';

    this.el.innerHTML = `<div class="panel-inner">
      <button class="panel-close" title="关闭 (Esc)">×</button>
      <div class="panel-word">
        <h2>${escapeHtml(w.lemma)}</h2>
        ${lvBadge(w.cefr)}
        <span class="tag">见过 ${Number(w.times_seen) || 0} 次</span>
        ${w.distinct_articles > 1
          ? `<span class="tag ok">${Number(w.distinct_articles)} 篇不同文章</span>` : ''}
      </div>
      ${w.gloss ? `<div class="panel-gloss">${escapeHtml(w.gloss)}</div>` : ''}
      ${w.forms?.length
        ? `<div class="panel-forms">文中出现过的形态：${w.forms.map(escapeHtml).join('、')}</div>` : ''}

      <div class="panel-label">掌握程度</div>
      <div class="status-row">
        ${STATUSES.map(([v, label]) =>
          `<button class="chip${w.status === v ? ' on' : ''}" data-status="${v}">${label}</button>`
        ).join('')}
      </div>
      <div class="panel-keys">
        <span>快捷键</span>
        ${STATUSES.map(([v]) => `<kbd>${v === 98 ? 'W' : v === 99 ? 'I' : v}</kbd>`).join('')}
      </div>

      <div class="panel-label">
        全部语境（${ctxs.length} 处${strong ? `，其中 ${strong} 处线索充分` : ''}）
        ${w.distinct_articles > 1
          ? '<span class="hint">同一个词在不同故事里反复出现，比在单词书上重复看十遍有效</span>' : ''}
      </div>
      ${contexts}
    </div>`;
  }
}
