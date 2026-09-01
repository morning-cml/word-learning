/* ===========================================================================
   阅读页。只做「把部件接起来」这一件事：
   文档数据 → Reader，点词 → WordPanel，键盘 → 模式切换。
   阅读器本身的逻辑都在 components/reader.js 里。
   =========================================================================== */

'use strict';

import { $, toast } from '../core.js';
import * as api from '../api.js';
import { Reader, WordPanel, MODE_HINTS } from '../components/reader.js';
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

  $('#marks').addEventListener('click', () => {
    $('#marks').textContent = '高亮：' + (reader.toggleMarks() ? '开' : '关');
  });

  document.addEventListener('keydown', (e) => {
    if (e.target.matches('input, textarea') || e.metaKey || e.ctrlKey) return;
    if (e.code === 'Space') { e.preventDefault(); setMode(reader.mode === 'en' ? 'zh' : 'en'); }
    else if (e.key === '1') setMode('en');
    else if (e.key === '2') setMode('zh');
    else if (e.key === '3') setMode('both');
    else if (e.key === '4') setMode('cloze');
  });
}
