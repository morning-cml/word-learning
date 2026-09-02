/* ===========================================================================
   阅读页。只做「把部件接起来」这一件事：
   文档数据 → Reader，点词 → WordPanel，键盘 → 模式切换。
   阅读器本身的逻辑都在 components/reader.js 里。
   =========================================================================== */

'use strict';

import { $, $$, toast } from '../core.js';
import * as api from '../api.js';
import { Reader, WordPanel, WordTip, MODE_HINTS } from '../components/reader.js';
import { renderStats } from '../components/stats.js';

export async function init({ articleId }) {
  const panel = new WordPanel($('#panel'), {
    load: (lemma) => api.words.detail(lemma),
    save: (lemma, status) => api.words.setStatus(lemma, status),
  });
  const reader = new Reader($('#doc'), { onWord: (lemma) => panel.open(lemma) });

  let doc;
  try {
    doc = await api.article.read(articleId);
  } catch (err) {
    $('#doc').innerHTML = `<p class="empty">${err.message}</p>`;
    return;
  }

  document.title = (doc.title_en || '阅读') + ' · Word Learning';
  reader.render(doc);
  if (doc.stats && Object.keys(doc.stats).length) {
    renderStats($('#stats'), doc.stats, doc.target_words);
    $('#statsCard').hidden = false;
  }

  const refreshProgress = () => {
    const { total, revealed } = reader.clozeProgress;
    $('#progress').textContent = `已揭示 ${revealed}/${total}`;
  };

  const setMode = (mode) => {
    reader.setMode(mode);
    [...$('#modes').children].forEach((b) => b.classList.toggle('on', b.dataset.mode === mode));
    $('#barHint').textContent = MODE_HINTS[mode];
    const cloze = mode === 'cloze';
    $('#reveal').hidden = !cloze;
    $('#progress').hidden = !cloze;
    if (cloze) { reader.revealAll(false); allShown = false; $('#reveal').textContent = '全部揭示'; refreshProgress(); }
  };

  $('#modes').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-mode]');
    if (btn) setMode(btn.dataset.mode);
  });

  // 键盘揭示（Tab 到挖空词再按空格/回车）也要刷新进度，不能只认鼠标
  ['click', 'keydown'].forEach((type) => $('#doc').addEventListener(type, () => {
    if (reader.mode === 'cloze') refreshProgress();
  }));

  let allShown = false;
  $('#reveal').addEventListener('click', () => {
    allShown = !allShown;
    reader.revealAll(allShown);
    $('#reveal').textContent = allShown ? '重新挖空' : '全部揭示';
    refreshProgress();
  });



  /* 悬停浮层：只想瞄一眼释义时，不必开那个 430px 的面板。
     两条 suppress 规则缺一不可——回忆模式下没揭示的词给了就等于直接把答案
     摆出来，整个模式就废了；面板已经开着时再弹一个浮层是两套 UI 打架。 */
  const tip = new WordTip($('#doc'), {
    load: (lemma) => api.words.detail(lemma),
    suppress: (el) =>
      (reader.mode === 'cloze' && !el.classList.contains('revealed'))
      || $('#panel').classList.contains('open'),
  });

  /* 专注模式：收起顶栏。导航在读的时候用不上，却占着固定的 56px。
     状态存 localStorage——「我读书时要不要看见导航」是个稳定偏好，
     每开一篇文章重设一次很烦。 */
  const FOCUS_KEY = 'wl-reader-focus';
  let focusOn = false;
  function setFocus(on, persist = true) {
    focusOn = on;
    document.body.classList.toggle('focus-mode', on);
    $('#focus').setAttribute('aria-pressed', String(on));
    $('#focus').textContent = on ? '退出专注' : '专注';
    if (persist) {
      try { localStorage.setItem(FOCUS_KEY, on ? '1' : '0'); }
      catch (err) { /* 隐私模式下写不了，本次会话内仍然生效 */ }
    }
    // 顶栏一收，整页内容会往上跳一截，浮层却钉在旧坐标上
    tip.hide();
    // 可用宽度没变但视口高度变了，锁着的段落高度要重算
    reader.lockHeights();
  }
  let savedFocus = null;
  try { savedFocus = localStorage.getItem(FOCUS_KEY); } catch (err) { /* 默认关 */ }
  setFocus(savedFocus === '1', false);
  $('#focus').addEventListener('click', () => setFocus(!focusOn));

  /* 排版控件。字号和栏宽写进 :root 的 CSS 变量，reader.css 那边用
     var(--reader-size, 默认值) 接住——所以没设过的人看到的和以前一模一样。

     改完必须重锁段落高度：lockHeights() 锁的 min-height 是按当时的宽度和
     字号算出来的，字号一变每段的行数就变了，锁着的旧值会在段尾留出一条空白
     （或者把内容挤出去）。这是「中英切换不跳页」那套机制的必要维护。 */
  const TYPO = {
    size:    { prop: '--reader-size',    steps: [16, 17.5, 19, 21, 23], def: 2,
               out: '#typoSize',    fmt: (v) => `${v} px` },
    measure: { prop: '--reader-measure', steps: [620, 700, 760, 840, 920], def: 2,
               out: '#typoMeasure', fmt: (v) => `${v} px` },
  };
  const typoKey = (name) => `wl-reader-${name}`;
  const typoIndex = {};

  function applyTypo(name) {
    const cfg = TYPO[name];
    const i = typoIndex[name];
    document.documentElement.style.setProperty(cfg.prop, `${cfg.steps[i]}px`);
    $(cfg.out).textContent = cfg.fmt(cfg.steps[i]);
    for (const btn of $$(`button[data-typo="${name}"]`)) {
      const next = i + Number(btn.dataset.step);
      btn.disabled = next < 0 || next >= cfg.steps.length;
    }
  }

  function setTypo(name, index, persist = true) {
    const cfg = TYPO[name];
    typoIndex[name] = Math.min(Math.max(index, 0), cfg.steps.length - 1);
    applyTypo(name);
    if (persist) {
      try { localStorage.setItem(typoKey(name), String(typoIndex[name])); }
      catch (err) { /* 隐私模式下写不了，本次会话内仍然生效 */ }
    }
    tip.hide();          // 字号一变，浮层底下的那个词就不在原地了
    reader.lockHeights();
  }

  for (const name of Object.keys(TYPO)) {
    let saved = null;
    try { saved = localStorage.getItem(typoKey(name)); } catch (err) { /* 用默认档 */ }
    const i = Number(saved);
    setTypo(name, saved !== null && Number.isInteger(i) ? i : TYPO[name].def, false);
  }

  const typoPop = $('#typoPop');
  const closeTypo = () => {
    typoPop.hidden = true;
    $('#typo').setAttribute('aria-expanded', 'false');
  };
  $('#typo').addEventListener('click', (e) => {
    e.stopPropagation();
    typoPop.hidden = !typoPop.hidden;
    $('#typo').setAttribute('aria-expanded', String(!typoPop.hidden));
  });
  typoPop.addEventListener('click', (e) => {
    e.stopPropagation();
    const btn = e.target.closest('button[data-typo]');
    if (btn) setTypo(btn.dataset.typo, typoIndex[btn.dataset.typo] + Number(btn.dataset.step));
  });
  $('#typoReset').addEventListener('click', () => {
    for (const name of Object.keys(TYPO)) setTypo(name, TYPO[name].def);
  });
  // 点别处收起来。Esc 也收——面板那边已经用 Esc 关闭了，两个都收不冲突。
  document.addEventListener('click', closeTypo);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeTypo(); });

  $('#marks').addEventListener('click', () => {
    $('#marks').textContent = '高亮：' + (reader.toggleMarks() ? '开' : '关');
  });

  document.addEventListener('keydown', (e) => {
    // matches?. 而不是 matches：键盘事件没有聚焦元素时 e.target 是 document，
    // 它没有 matches()，直接调用会抛 TypeError——而这个监听器一抛，
    // 后面所有快捷键（空格切中英、1-4 切模式）就全都不响应了，
    // 而且控制台之外没有任何迹象。
    if (e.target.matches?.('input, textarea') || e.metaKey || e.ctrlKey) return;
    if (e.code === 'Space') { e.preventDefault(); setMode(reader.mode === 'en' ? 'zh' : 'en'); }
    // F 不和别的键冲突，面板开着也照样能按——它管的是整页的取景，不是某个词
    else if (e.key === 'f' || e.key === 'F') setFocus(!focusOn);
    // 面板开着时数字键归「改掌握程度」管（见 components/reader.js 的 HOTKEYS）。
    // 显式让路，不靠两个 document 监听器的注册顺序——那种依赖谁先挂上的写法，
    // 哪天有人调换两行 new 的位置就会静默失效。
    else if (!$('#panel').classList.contains('open')) {
      if (e.key === '1') setMode('en');
      else if (e.key === '2') setMode('zh');
      else if (e.key === '3') setMode('both');
      else if (e.key === '4') setMode('cloze');
    }
  });

  /* 阅读位置记忆。一篇文章读到一半退出去查个词再回来，从头开始是很烦的；
     Lute 把音频位置、UI 设置、专注模式都存了。这里只存滚动位置就够——
     它是唯一「丢了就得自己找回来」的状态。

     存 localStorage 不存库：这是每台设备各自的阅读姿势，不是学习状态，
     不该进那份不可再生的资产，也不该跟着账号跑。 */
  const POS_KEY = `wl-pos-${articleId}`;
  let posTimer;
  window.addEventListener('scroll', () => {
    clearTimeout(posTimer);
    posTimer = setTimeout(() => {
      try { localStorage.setItem(POS_KEY, String(Math.round(window.scrollY))); }
      catch (err) { /* 隐私模式下写不了，忽略 */ }
    }, 300);
  }, { passive: true });

  // 等 lockHeights() 把段落高度锁完再跳，否则跳到的是锁高之前的坐标
  requestAnimationFrame(() => requestAnimationFrame(() => {
    let saved = null;
    try { saved = localStorage.getItem(POS_KEY); } catch (err) { /* 读不到就从头开始 */ }
    const y = Number(saved);
    if (saved && Number.isFinite(y) && y > 0) window.scrollTo(0, y);
  }));
}
