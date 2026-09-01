/* ===========================================================================
   生成页。

   这页的难点全在「生成过程要看得见」上：一篇 400 词的文章要跑 4-9 次模型调用、
   一两分钟，中间还可能触发修复和补线索。转圈圈的话用户不知道是在干活还是卡死了。

   管线 yield 出来的事件有 8 类（phase/plan/paragraph/retry/error/done/saved 及
   phase 下的 5 个子阶段）。这里把它们摊成一条时间线：正在跑的那步高亮 + 转圈，
   跑完收成一行并带上结论（比如某段的线索审计结果）。
   加新事件类型时只要在 EVENT_HANDLERS 里加一条，不用动其它地方。
   =========================================================================== */

'use strict';

import { $, escapeHtml, debounce, toast } from '../core.js';
import * as api from '../api.js';
import { renderStats } from '../components/stats.js';

/* ------------------------------ 时间线 ------------------------------ */

class Timeline {
  constructor(el, metaEl) {
    this.el = el;
    this.metaEl = metaEl;
    this.current = null;
  }

  reset() {
    this.el.innerHTML = '';
    this.metaEl.textContent = '';
    this.current = null;
  }

  /** 开一个新步骤，前一个自动收尾。返回它的节点，方便后续补充说明。 */
  begin(text, sub = '') {
    this.settle();
    const row = document.createElement('div');
    row.className = 'step active';
    row.innerHTML = `<span class="dot"><span class="spinner"></span></span>`
      + `<span class="body">${escapeHtml(text)}`
      + (sub ? `<span class="sub">${escapeHtml(sub)}</span>` : '')
      + `</span>`;
    this.el.appendChild(row);
    this.current = row;
    return row;
  }

  /** 把当前步骤标成完成。tone 传 'bad' 表示这一步是坏消息。 */
  settle(tone = '') {
    if (!this.current) return;
    this.current.classList.remove('active');
    this.current.classList.add(tone || 'done');
    $('.dot', this.current).textContent = tone === 'bad' ? '✕' : '✓';
    this.current = null;
  }

  /** 一次性的一行，不进入「进行中」状态。 */
  line(text, sub = '', tone = '') {
    this.settle();
    const row = document.createElement('div');
    row.className = 'step' + (tone ? ' ' + tone : '');
    row.innerHTML = `<span class="dot">${tone === 'bad' ? '!' : '·'}</span>`
      + `<span class="body">${escapeHtml(text)}`
      + (sub ? `<span class="sub">${escapeHtml(sub)}</span>` : '')
      + `</span>`;
    this.el.appendChild(row);
    return row;
  }

  meta(text) { this.metaEl.textContent = text; }
}

/* ------------------------------ 事件分发 ------------------------------
   一类事件一个处理函数。管线以后加事件类型，这里加一条就行。 */

const EVENT_HANDLERS = {
  phase: (ev, ui) => {
    // 修复和补线索是「本来不该发生」的事，单独标出来，别混在正常步骤里
    const tone = (ev.phase === 'repair' || ev.phase === 'clue_fix') ? 'warn' : '';
    if (tone) ui.tl.line(ev.message, '', '');
    else ui.tl.begin(ev.message);
  },

  plan: (ev, ui) => {
    ui.tl.settle();
    ui.tl.line(`选题《${ev.title_en || '未命名'}》${ev.title_zh ? ' / ' + ev.title_zh : ''}`,
               [ev.genre, ev.reason].filter(Boolean).join(' · '));
  },

  paragraph: (ev, ui) => {
    const audits = ev.paragraph?.audits || [];
    const detail = audits.map((a) => `${a.lemma}=${a.strength}`).join('，');
    const weak = audits.some((a) => a.strength !== 'strong');
    ui.tl.settle();
    ui.tl.line(`第 ${ev.index} 段完成`, detail, weak ? '' : '');
  },

  retry: (ev, ui) => {
    ui.tl.line(`重试第 ${ev.attempt} 次`, ev.reason || '', 'bad');
  },

  done: (ev, ui) => {
    ui.tl.settle();
    ui.stats = ev.stats || {};
    ui.doc = ev.document;
  },

  saved: (ev, ui) => {
    ui.articleId = ev.article_id;
  },

  cancelled: (ev, ui) => {
    ui.cancelled = true;
  },

  error: (ev, ui) => {
    ui.tl.settle('bad');
    ui.tl.line('出错', ev.message || '', 'bad');
    ui.failed = ev.message || '生成失败';
  },
};

/* ------------------------------ 页面 ------------------------------ */

export async function init() {
  const wordsEl = $('#words');
  const goBtn = $('#go');
  const stopBtn = $('#stop');
  const tl = new Timeline($('#timeline'), $('#progressMeta'));
  let aborter = null;

  /* 预览：用服务端同一套解析，避免前后端对「什么算一个词」判断不一致 */
  const refreshPreview = debounce(async () => {
    const raw = wordsEl.value.trim();
    const box = $('#preview');
    if (!raw) { box.textContent = ''; box.className = 'hint'; return; }
    try {
      const p = await api.article.preview(raw);
      box.className = 'hint' + (p.warning ? ' ' : '');
      box.textContent = p.count
        ? `识别到 ${p.count} 个词 → 约 ${p.paragraphs} 段、${p.estimated_words} 词。`
          + (p.warning ? ' ' + p.warning : '')
        : '';
      box.style.color = p.warning ? 'var(--warn)' : '';
    } catch (err) {
      box.textContent = '';
    }
  }, 250);

  wordsEl.addEventListener('input', refreshPreview);

  // 中止只能停在两次管线事件之间——正在飞的那次模型调用掐不断，
  // 所以按钮明说「正在停止」，不假装立刻停住。桌面版当初也是这么做的。
  stopBtn.addEventListener('click', () => {
    if (!aborter) return;
    aborter.abort();
    stopBtn.disabled = true;
    stopBtn.textContent = '正在停止…';
  });

  goBtn.addEventListener('click', async () => {
    const raw = wordsEl.value.trim();
    if (!raw) { wordsEl.focus(); return; }

    // 用服务端的解析结果当目标词列表，保证 chip 和后端认定的词完全一致
    let targets = [];
    try {
      targets = (await api.article.preview(raw)).words || [];
    } catch (err) {
      toast(err.message, 'bad');
      return;
    }

    goBtn.disabled = true;
    goBtn.textContent = '生成中…';
    stopBtn.hidden = false;
    stopBtn.disabled = false;
    stopBtn.textContent = '停止';
    aborter = new AbortController();
    $('#resultCard').hidden = true;
    $('#progressCard').hidden = false;
    tl.reset();
    tl.begin('正在连接模型…');

    const ui = { tl, stats: null, doc: null, articleId: null, failed: '', cancelled: false };
    const started = Date.now();
    const ticker = setInterval(
      () => tl.meta(`已用 ${Math.round((Date.now() - started) / 1000)} 秒`), 1000);

    try {
      await api.article.generate({ words: raw, level: $('#level').value }, (ev) => {
        const handler = EVENT_HANDLERS[ev.type];
        if (handler) handler(ev, ui);
        else console.debug('[index] 未处理的事件类型', ev.type, ev);
      }, { signal: aborter.signal });
    } catch (err) {
      // abort 走的也是异常路径，但那是用户主动的，不该报成「出错」
      if (err.name === 'AbortError' || ui.cancelled) ui.cancelled = true;
      else {
        ui.failed = err.message;
        tl.settle('bad');
        tl.line('请求失败', err.message, 'bad');
      }
    } finally {
      clearInterval(ticker);
      tl.settle();
      aborter = null;
      stopBtn.hidden = true;
      goBtn.disabled = false;
      goBtn.textContent = '开始生成';
    }

    const secs = Math.round((Date.now() - started) / 1000);
    if (ui.cancelled) {
      tl.line('已停止', '这次没有落库；已经花掉的调用无法退回', 'bad');
      tl.meta(`${secs} 秒后停止`);
      toast('已停止生成', 'warn');
      return;
    }
    if (ui.failed) {
      tl.meta(`${secs} 秒后中止`);
      toast(ui.failed, 'bad');
      return;
    }

    tl.meta(`共 ${secs} 秒 · ${ui.stats?.llm_calls ?? '?'} 次模型调用 · ${ui.stats?.tokens ?? '?'} tokens`);

    if (ui.stats && ui.articleId) {
      renderStats($('#stats'), ui.stats, targets);
      $('#openReader').href = `/read/${ui.articleId}`;
      $('#resultCard').hidden = false;
      $('#resultCard').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      toast('生成完成', 'ok');
    }
  });
}
