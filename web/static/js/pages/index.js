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

import { $, escapeHtml, debounce, toast, html, fmtTime } from '../core.js';
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

const STAGE_TEXT = {
  plan: '正在选题', write: '正在写正文', repair: '正在修复这一段',
  audit: '正在审查语境线索', clue_fix: '正在补线索', glossary: '正在生成释义',
};

const EVENT_HANDLERS = {
  //  每发出一次模型请求都会来一条。进度和剩余时间全靠它——
  //  调用次数是这条管线里唯一和耗时成正比的东西。
  call: (ev, ui) => ui.pg.onCall(),

  phase: (ev, ui) => {
    ui.pg.setStage(ev.index && ev.total
      ? `${STAGE_TEXT[ev.phase] || ''}（第 ${ev.index}/${ev.total} 段）`
      : (STAGE_TEXT[ev.phase] || ev.message));
    // 修复和补线索是「本来不该发生」的事，单独标出来，别混在正常步骤里
    const tone = (ev.phase === 'repair' || ev.phase === 'clue_fix') ? 'warn' : '';
    if (tone) ui.tl.line(ev.message, '', '');
    else ui.tl.begin(ev.message);
  },

  plan: (ev, ui) => {
    ui.pg.setParagraphs(ev.paragraphs);   // 选题回来才知道真实段数
    ui.tl.settle();
    ui.tl.line(`选题《${ev.title_en || '未命名'}》${ev.title_zh ? ' / ' + ev.title_zh : ''}`,
               [ev.genre, ev.reason].filter(Boolean).join(' · '));
  },

  paragraph: (ev, ui) => {
    const audits = ev.paragraph?.audits || [];
    const detail = audits.map((a) => `${a.lemma}=${a.strength}`).join('，');
    // 走到这里还有词不是 strong，说明补线索那两轮也没救回来——这一段里
    // 那几个词读完猜不出意思，等于白读。原先这个判断算出来了却没用
    // （两个分支都返回空串），于是它和一段全 strong 的长得一模一样。
    const weak = audits.some((a) => a.strength !== 'strong');
    ui.tl.settle();
    ui.tl.line(`第 ${ev.index} 段完成`, detail, weak ? 'bad' : '');
  },

  retry: (ev, ui) => {
    ui.tl.line(`重试第 ${ev.attempt} 次`, ev.reason || '', 'bad');
  },

  done: (ev, ui) => {
    ui.pg.finish();
    ui.tl.settle();
    ui.stats = ev.stats || {};
    ui.doc = ev.document;
  },

  saved: (ev, ui) => {
    ui.articleId = ev.article_id;
  },

  cancelled: (ev, ui) => {
    ui.cancelled = true;
    ui.pg.stop('已停止');
  },

  error: (ev, ui) => {
    ui.pg.stop('出错了');
    ui.tl.settle('bad');
    ui.tl.line('出错', ev.message || '', 'bad');
    ui.failed = ev.message || '生成失败';
  },
};

/* ------------------------------ 最近生成 ------------------------------
   首页原先只有一张输入卡，回访时是一片空白——已经生成过的东西一个都看不见，
   想接着读还得先去文库。顺带把线索比带上：那是判断「这篇值不值得读」的指标。 */

function clueTag(clue) {
  const m = /^(\d+)\/(\d+)$/.exec(clue || '');
  if (!m) return '';
  const ok = m[1] === m[2];
  return `<span class="tag ${ok ? 'ok' : 'warn'}">线索 ${m[1]}/${m[2]}</span>`;
}

async function loadRecent() {
  let list;
  try {
    list = (await api.article.list()).articles || [];
  } catch (err) {
    return;                       // 首页的次要内容，拉不到就当没有，不打断主流程
  }
  if (!list.length) return;
  html($('#recent'), list.slice(0, 5).map((a) => `
    <a href="/read/${a.id}">
      <span class="t">${escapeHtml(a.title_en || '（无标题）')}</span>
      <span class="m">${escapeHtml(fmtTime(a.created_at))}${clueTag(a.clue)}</span>
    </a>`).join(''));
  $('#recentN').textContent = list.length;
  $('#recentCard').hidden = false;
}

/* ------------------------------ 进度与剩余时间 ------------------------------

   时间线回答「在干什么」，这里回答「还要多久」——后者才是决定
   「等一下还是先去干别的」的那个信息。

   计量单位是**模型调用次数**，不是阶段数。理由：调用是这条管线里唯一实质
   耗时的东西（一次 30-40 秒），而阶段数推不出调用数——修复和补线索是
   条件触发的，事先不知道会不会发生、发生几次。所以 core/llm/client.py
   每发一次请求就报一个 call 事件，这里数它。

   预估总数按顺风路径算：选题 1 次 + 每段（写 1 + 审 1）+ 释义 1 次。
   真跑出修复或补线索就会超出——那时**把分母加大**，而不是让条子卡在
   99% 不动：卡住的条子会被读成「死了」，而它其实在正常干活。

   每次调用要多久：优先用本次已经测到的，没测够就用历史中位数
   （/api/timing，取自用户自己以前几篇）。两个都没有就不显示时间——
   宁可不说，也不要给一个编出来的数字让人据此安排。 */

/** 秒 → 「1 分 20 秒」。估算不该精确到秒，取整反而显得可信。 */
function fmtDur(sec) {
  const s = Math.max(0, Math.round(sec));
  if (s < 60) return `${Math.max(5, Math.round(s / 5) * 5)} 秒`;
  const m = Math.floor(s / 60);
  const r = Math.round((s % 60) / 15) * 15;
  if (r >= 60) return `${m + 1} 分`;
  return r ? `${m} 分 ${r} 秒` : `${m} 分`;
}

export class Progress {
  constructor({ fill, stage, eta }) {
    this.fill = fill;
    this.stageEl = stage;
    this.etaEl = eta;
  }

  reset(paragraphs, priorSec) {
    this.total = 2 + 2 * Math.max(1, paragraphs || 2);   // 选题 + 每段(写+审) + 释义
    this.done = 0;
    this.samples = [];
    this.prior = priorSec || null;
    this.callStartedAt = null;
    this.pct = 0;
    this.finished = false;
    this.stage = '正在连接模型…';
    this.fill.className = 'progress-fill';
    this.fill.style.width = '0%';
    this.etaEl.textContent = '';
    // 立刻画一次。有历史先验的话，点下「开始生成」的那一刻就该看到
    // 「预计还需 X」——而那正是最想知道它的时刻；等到第一次调用回来才显示
    // 就晚了半分钟，那半分钟里屏幕上只有一条不动的空条。
    this.render();
  }

  /** 选题回来后才知道真实段数，据此校正分母。 */
  setParagraphs(n) {
    if (!n) return;
    this.total = Math.max(this.done + 1, 2 + 2 * n);
    this.render();
  }

  setStage(text) {
    if (text) { this.stage = text; this.render(); }
  }

  /** 一次新的模型调用开始了：上一次到此为止。 */
  onCall() {
    const now = Date.now();
    if (this.callStartedAt !== null) {
      this.samples.push((now - this.callStartedAt) / 1000);
      this.done += 1;
    }
    this.callStartedAt = now;
    // 修复 / 补线索会让实际调用数超出预估。加大分母，别让条子卡死在满格
    if (this.done + 1 > this.total) this.total = this.done + 1;
    this.render();
  }

  /** 每次调用平均多久。本次测到两次以上就以本次为准——
      当天的网络和模型状态比历史更能代表现在。 */
  get secPerCall() {
    if (this.samples.length >= 2) {
      return this.samples.reduce((a, b) => a + b, 0) / this.samples.length;
    }
    return this.prior || this.samples[0] || null;
  }

  render() {
    if (this.finished) return;
    const per = this.secPerCall;
    const base = this.done / this.total;
    const next = (this.done + 1) / this.total;

    // 当前这次调用飞到一半时也给部分进度，否则条子会静止半分钟，看着像卡死
    let inflight = 0;
    if (this.callStartedAt !== null && per) {
      inflight = Math.min((Date.now() - this.callStartedAt) / 1000 / per, 0.95);
    }
    // 只涨不退（往回缩会被读成出错），封顶 98%（没真跑完就显示 100% 是撒谎）
    this.pct = Math.min(Math.max(this.pct, base + (next - base) * inflight), 0.98);
    this.fill.style.width = (this.pct * 100).toFixed(1) + '%';
    this.stageEl.textContent = this.stage;

    if (!per) {
      this.etaEl.textContent = '第一次生成，跑完一步才估得出';
      return;
    }
    const inCall = this.callStartedAt !== null ? (Date.now() - this.callStartedAt) / 1000 : 0;
    const left = (this.total - this.done) * per - inCall;
    this.etaEl.textContent = left > 5 ? `预计还需 ${fmtDur(left)}` : '快好了…';
  }

  finish() {
    this.finished = true;
    this.pct = 1;
    this.fill.className = 'progress-fill done';
    this.fill.style.width = '100%';
    this.etaEl.textContent = '';
  }

  stop(text) {
    this.finished = true;
    this.fill.className = 'progress-fill bad';
    this.stageEl.textContent = text;
    this.etaEl.textContent = '';
  }
}

/* ------------------------------ 用词上限说明 ------------------------------
   词汇量现拉而不是写死在模板里：没下载 CEFR-J 时标尺会退回内置兜底表，
   写死的数字就会和程序实际拦的东西对不上。数字对不上没人报得出来——
   能判断「这篇文章的用词是不是超了 B2」的人本来就不需要这个功能。 */

async function loadLevels(levelEl) {
  const box = document.getElementById('levels');
  if (!box) return;

  // 四行既是说明也是控件，和下拉框双向同步
  const sync = () => [...box.children].forEach(
    (li) => li.classList.toggle('on', li.dataset.level === levelEl.value));
  box.addEventListener('click', (e) => {
    const li = e.target.closest('li[data-level]');
    if (!li) return;
    levelEl.value = li.dataset.level;
    sync();
  });
  levelEl.addEventListener('change', sync);
  sync();

  let info;
  try {
    info = await api.levels();
  } catch (err) {
    return;                      // 拉不到就只显示文字说明，不影响选档
  }
  const note = document.getElementById('levelNote');
  if (!info.using_real_data) {
    // 兜底表全标 A1，按它算出来的「累计词汇量」是假的，不如不给
    if (note) {
      note.innerHTML = '点一行就切到那一档。'
        + '当前用的是内置兜底词表，给不出各档的词汇量；'
        + '跑一次 <code>scripts/fetch_cefr.py</code> 下载 CEFR-J 后这里会显示真实数字。';
    }
    return;
  }
  for (const el of box.querySelectorAll('[data-count]')) {
    const n = info.cumulative?.[el.dataset.count];
    if (n) el.textContent = Number(n).toLocaleString() + ' 词';
  }
}

/* ------------------------------ 页面 ------------------------------ */

export async function init() {
  const wordsEl = $('#words');
  const goBtn = $('#go');
  const stopBtn = $('#stop');
  const tl = new Timeline($('#timeline'), $('#progressMeta'));
  const pg = new Progress({ fill: $('#progressFill'), stage: $('#progressStage'), eta: $('#progressEta') });
  let aborter = null;

  //  历史耗时当先验：刚开始生成时还没有本次的测量值，而那正是最想知道
  //  「还要多久」的时候。拿不到就不显示时间，不编一个常数。
  let priorSec = null;
  api.timing().then((t) => { priorSec = t.sec_per_call; }).catch(() => {});

  /* 预览：用服务端同一套解析，避免前后端对「什么算一个词」判断不一致 */
  const refreshPreview = debounce(async () => {
    const raw = wordsEl.value.trim();
    const box = $('#preview');
    if (!raw) { box.textContent = ''; box.className = 'hint'; return; }
    try {
      const p = await api.article.preview(raw);
      box.className = 'hint' + (p.warning ? ' warn' : '');
      box.textContent = p.count
        ? `识别到 ${p.count} 个词 → 约 ${p.paragraphs} 段、${p.estimated_words} 词。`
          + (p.warning ? ' ' + p.warning : '')
        : '';
    } catch (err) {
      box.textContent = '';
      box.className = 'hint';
    }
  }, 250);

  wordsEl.addEventListener('input', refreshPreview);
  loadRecent();
  loadLevels($('#level'));

  $('#presets')?.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-fill]');
    if (!btn) return;
    wordsEl.value = btn.dataset.fill.split(' ').join('\n');
    wordsEl.focus();
    refreshPreview();
  });

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
    let paragraphs = 0;
    try {
      const pv = await api.article.preview(raw);
      targets = pv.words || [];
      paragraphs = pv.paragraphs || 0;      // 先按预览的段数估，选题回来再校正
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
    pg.reset(paragraphs, priorSec);

    const ui = { tl, pg, stats: null, doc: null, articleId: null, failed: '', cancelled: false };
    const started = Date.now();
    //  每秒重画一次：正在飞的那次调用也要让条子往前走，
    //  否则一次调用 30-40 秒里条子纹丝不动，看着就是卡死了。
    const ticker = setInterval(() => {
      tl.meta(`已用 ${Math.round((Date.now() - started) / 1000)} 秒`);
      pg.render();
    }, 1000);

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
      loadRecent();
    }
  });
}
