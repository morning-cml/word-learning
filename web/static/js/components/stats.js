/* ===========================================================================
   生成结果的数据面板。生成页和阅读页共用同一份渲染，所以放在 components/ 而不是
   某个 pages/ 里——两处显示不一致会让人怀疑数字是不是算错了。
   =========================================================================== */

'use strict';

import { escapeHtml } from '../core.js';

/** 一个数字 + 一行说明。 */
function stat(value, caption, tone = '') {
  return `<div class="stat${tone ? ' ' + tone : ''}"><b>${escapeHtml(value)}</b><span>${escapeHtml(caption)}</span></div>`;
}

const STRENGTH = {
  strong: { label: '线索充分', cls: 'ok' },
  weak:   { label: '线索偏弱', cls: 'warn' },
  none:   { label: '无线索',   cls: 'bad' },
};

/** CEFR 徽章。超纲词列表里带上等级，才看得出「超了多少」。 */
function lv(level) {
  const v = (level || '').toLowerCase();
  return /^[abc][12]$/.test(v) ? `<span class="lv lv-${v}">${escapeHtml(level)}</span>` : '';
}

/** 逐词的线索结论。
 *
 * 「有 2 个词的语境线索不足」原先只是一句话——用户知道有问题，
 * 但不知道是哪两个词，也就没法拿它们再生成一篇。这一整套管线里最贵的
 * 那次调用（线索审计）产出的结论，到这里才算真正交到用户手上。 */
function auditList(audits) {
  const weak = (audits || []).filter((a) => a && a.strength !== 'strong');
  if (!weak.length) return '';
  const rows = weak.map((a) => {
    const st = STRENGTH[a.strength] || STRENGTH.none;
    return `<li>
      <span class="aw">${escapeHtml(a.lemma || '')}</span>
      <span class="tag ${st.cls}">${st.label}</span>
      <span class="why">${escapeHtml(a.why || '')}</span>
    </li>`;
  }).join('');
  return `<div class="audit-list">
    <div class="section-title">这些词读完可能猜不出意思 <span class="n">${weak.length}</span></div>
    <ul>${rows}</ul>
    <p class="hint">拿它们再生成一篇、换个场景，比在这一篇上反复补线索有效。</p>
  </div>`;
}

//  每个 #stats 容器的「忽略」回调。放 WeakMap 而不是闭包里：renderStats 会被
//  反复调用，监听器只挂一次，但回调要能跟着最新一次调用换。
const ignoreHandlers = new WeakMap();

/** 让超纲词列表可点：点一下 = 「这个词不用管」。
 *
 * 入口放在这里而不是正文里，是因为**问题就是在这里报出来的**——标尺说
 * 「文中仍有超纲词：toward、You're」，纠正它的动作就该在同一个地方。
 * 而且实测被误报的多数不是人名，是标尺自己不认识的词（CEFR-J 只有 8653 条），
 * 「我认识这个词」和「这是人名」需要的是同一个动作。
 */
function bindIgnore(el, onIgnore) {
  ignoreHandlers.set(el, onIgnore);
  if (el.dataset.ignoreBound) return;
  el.dataset.ignoreBound = '1';
  el.addEventListener('click', async (e) => {
    const btn = e.target.closest('button[data-ignore]');
    if (!btn || btn.disabled) return;
    const handler = ignoreHandlers.get(el);
    if (!handler) return;
    btn.disabled = true;
    try {
      await handler(btn.dataset.ignore);
      //  只加个类，不动 textContent——那会把里面的等级徽章一起冲掉
      btn.classList.add('ignored');
      btn.title = '已收进词库并标成「忽略」，以后不再判它超纲';
    } catch (err) {
      btn.disabled = false;
      btn.title = err.message || '没能保存';
    }
  });
}

export function renderStats(el, s, targets, { onIgnore } = {}) {
  if (!el || !s) return;
  if (onIgnore) bindIgnore(el, onIgnore);

  const missed  = new Set(s.targets_missed || []);
  // 「模型说塞不进、于是没写」和「本该写进去却没写」是两回事，chip 必须分开
  const skipped = new Set(s.unplaced || []);
  const cs = s.clue_strength || null;
  const clueTotal = cs ? cs.strong + cs.weak + cs.none : 0;
  const rate = ((s.offender_rate || 0) * 100).toFixed(1);

  const blocks = [];

  blocks.push('<div class="stats">');
  if (clueTotal) {
    // 这是衡量「这篇文章到底能不能帮你记住词」的唯一指标，放在最前
    blocks.push(stat(`${cs.strong}/${clueTotal}`, '语境线索充分',
                     cs.strong === clueTotal ? 'ok' : 'warn'));
  }
  blocks.push(stat(`${s.targets_hit || 0}/${s.targets_total || 0}`, '目标词命中'));
  blocks.push(stat(`${rate}%`, '超纲词占比'));
  blocks.push(stat(String(s.word_count || 0), '总词数'));
  blocks.push(stat(String(s.sentence_count || 0), '句'));
  if (s.tokens) blocks.push(stat(Number(s.tokens).toLocaleString(), 'tokens'));
  blocks.push('</div>');

  if (targets?.length) {
    blocks.push('<div class="chips" style="margin-top:18px">');
    for (const w of targets) {
      const cls = skipped.has(w) ? 'skip' : missed.has(w) ? 'miss' : 'hit';
      const tip = skipped.has(w) ? ' title="模型判断这个题材容不下它，没有硬塞"'
                : missed.has(w)  ? ' title="这个词没有出现在文章里"' : '';
      blocks.push(`<span class="chip ${cls}"${tip}>${escapeHtml(w)}</span>`);
    }
    blocks.push('</div>');
  }

  // 有逐词结论就直接列出来；没有（老文章）才退回那句概括
  const detail = auditList(s.audits);
  if (detail) blocks.push(detail);
  else if (cs && (cs.weak || cs.none)) {
    blocks.push(`<div class="note warn" style="margin-top:14px">
      有 ${cs.weak + cs.none} 个词的语境线索不足——这些词读完可能猜不出意思。
      建议之后用它们再生成一篇，换个场景。
    </div>`);
  }

  // 学过的词在这篇里又出现了。这是好消息——多语境重复正是这个产品声称
  // 最有效的机制——可它以前完全不可见：要么被判成超纲列进「文中仍有超纲词」，
  // 要么被修复指令直接从文章里删掉。顺带它也是这条豁免的反馈回路：
  // 放宽过头的话，这个名单会先变得不像话。
  if (s.revisited?.length) {
    const list = s.revisited.slice(0, 12).map(escapeHtml).join('、');
    blocks.push(`<div class="note ok" style="margin-top:10px">
      这一篇里还出现了 <b>${s.revisited.length}</b> 个你以前学过的词：${list}${
        s.revisited.length > 12 ? ' 等' : ''}。<br>
      <span class="hint">同一个词在不同故事里再遇到，比在单词书上重复看十遍有效——
      它们不计入超纲。</span>
    </div>`);
  }

  if (skipped.size) {
    blocks.push(`<div class="note warn" style="margin-top:10px">
      模型判断这个题材容不下 ${[...skipped].map(escapeHtml).join('、')}，没有硬塞——
      硬塞会把文章变成填空作业，反而破坏语境记忆。换个题材再生成一篇能覆盖到它们。
    </div>`);
  }

  if (s.offenders?.length) {
    const list = s.offenders.slice(0, 10).map((o) => {
      const body = `${escapeHtml(o.surface)}${lv(o.level)}`;
      if (!onIgnore) return `<span class="offender">${body}</span>`;
      return `<button type="button" class="offender" data-ignore="${escapeHtml(o.lemma || o.surface)}"`
           + ` title="点一下：以后不再把这个词判成超纲">${body}</button>`;
    }).join('');
    blocks.push(`<div class="offenders"><span class="hint">文中仍有超纲词</span>${list}`
      + (onIgnore
        ? '<span class="hint offenders-tip">点一下告诉程序这个词不用管——'
          + '人名，或者你本来就认识、只是词表里没收的词</span>'
        : '')
      + '</div>');
  }

  if (s.using_real_cefr === false) {
    blocks.push(`<div class="note warn" style="margin-top:10px">
      当前用的是内置兜底词表，难度判定较粗。跑一次
      <code>scripts/fetch_cefr.py</code> 下载 CEFR-J 完整词表可显著提升准确度。
    </div>`);
  }

  el.innerHTML = blocks.join('');
}
