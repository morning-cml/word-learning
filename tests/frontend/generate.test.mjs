/* 生成页：把真实管线会 yield 的事件序列灌进去，验证时间线与结果面板。 */
import { JSDOM } from 'jsdom';
import { readFileSync } from 'fs';
import { pathToFileURL } from 'url';

let ok = 0, fail = 0;
const check = (n, c, e = '') => { c ? ok++ : fail++; console.log(`  ${c ? 'ok  ' : 'FAIL'} ${n}${e ? '  ' + e : ''}`); };

const dom = new JSDOM(readFileSync('./.fixtures/index.html', 'utf8'), { url: 'http://127.0.0.1:8000/', pretendToBeVisual: true });
const w = dom.window, doc = w.document;
for (const k of ['window','document','localStorage','matchMedia','requestAnimationFrame',
                 'Node','Element','HTMLElement','KeyboardEvent','MouseEvent','Event','TextDecoder'])
  { if (w[k] !== undefined) { try { globalThis[k] = w[k]; } catch (e) {} } }
w.HTMLElement.prototype.scrollIntoView = () => {};

// 一次真实生成会 yield 的事件序列（含一次修复和一次重试，故意不走顺风路径）
const EVENTS = [
  { type:'phase', phase:'plan', message:'正在为 5 个词选题，规划 2 段' },
  { type:'plan', topic:'深夜电台', genre:'短篇小说', title_en:'The Wrong Number',
    title_zh:'打错的电话', reason:'这批词有共同的情绪场', paragraphs:2 },
  { type:'phase', phase:'write', index:1, total:2, message:'第 1/2 段：abandon、silence' },
  { type:'retry', attempt:1, reason:'空响应：思考用了 3110 tokens，只剩 0 给正文' },
  { type:'phase', phase:'repair', index:1, message:'第 1 段校验未过（too_hard），第 1 次修复' },
  { type:'phase', phase:'audit', index:1, message:'第 1 段：审查 2 个词的语境线索' },
  { type:'paragraph', index:1, paragraph:{ sentences:[{},{}],
    audits:[{lemma:'abandon',strength:'strong'},{lemma:'silence',strength:'strong'}] } },
  { type:'phase', phase:'write', index:2, total:2, message:'第 2/2 段：confess、hesitate、fragile' },
  { type:'paragraph', index:2, paragraph:{ sentences:[{}],
    audits:[{lemma:'confess',strength:'weak'}] } },
  { type:'phase', phase:'glossary', message:'生成中文释义' },
  { type:'done', document:{ title_en:'The Wrong Number' }, stats:{
      clue_strength:{strong:4,weak:1,none:0}, targets_hit:4, targets_total:5,
      targets_missed:['fragile'], unplaced:['fragile'], offender_rate:0.012,
      word_count:212, sentence_count:13, llm_calls:6, tokens:24310,
      offenders:[], using_real_cefr:true } },
  { type:'saved', article_id:42 },
];

function sseBody(events) {
  const enc = new TextEncoder();
  // 故意把 chunk 切在事件中间，验证缓冲拼接是对的
  const raw = events.map(e => `data: ${JSON.stringify(e)}\n\n`).join('');
  const bytes = enc.encode(raw);
  let i = 0;
  return { getReader: () => ({ read: async () => {
    if (i >= bytes.length) return { done: true };
    const end = Math.min(i + 37, bytes.length);      // 37 字节一刀，必然切断事件
    const value = bytes.slice(i, end); i = end;
    return { done: false, value };
  } }) };
}

w.fetch = async (url, opts = {}) => {
  const p = String(url);
  if (p === '/api/article/plan-preview')
    return { ok:true, status:200, json: async () => ({
      words:['abandon','silence','confess','hesitate','fragile'], count:5,
      paragraphs:2, estimated_words:170, warning:'' }) };
  if (p === '/api/article/generate') return { ok:true, status:200, body: sseBody(EVENTS) };
  return { ok:true, status:200, json: async () => ({}) };
};
globalThis.fetch = w.fetch;

const mod = await import(pathToFileURL('./.fixtures/static/js/pages/index.js').href);
await mod.init({});

doc.querySelector('#words').value = 'abandon silence confess hesitate fragile';
doc.querySelector('#go').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
await new Promise(r => setTimeout(r, 300));

const steps = [...doc.querySelectorAll('#timeline .step')];
const text = doc.querySelector('#timeline').textContent;
console.log('生成页 SSE');
check('时间线渲染出多步', steps.length >= 8, `${steps.length} 步`);
check('切碎的 SSE 分片被正确拼回', text.includes('The Wrong Number'));
check('选题理由显示', text.includes('这批词有共同的情绪场'));
check('重试被标红', [...doc.querySelectorAll('#timeline .step.bad')].some(s => s.textContent.includes('重试')));
check('修复步骤可见', text.includes('第 1 次修复'));
check('逐段线索结论显示', text.includes('abandon=strong') && text.includes('confess=weak'));
check('没有残留的进行中步骤', doc.querySelectorAll('#timeline .step.active').length === 0);
check('结果卡片出现', !doc.querySelector('#resultCard').hidden);
check('阅读器链接指向新文章', doc.querySelector('#openReader').getAttribute('href') === '/read/42');
check('按钮已复位', !doc.querySelector('#go').disabled && doc.querySelector('#go').textContent === '开始生成');

const stats = doc.querySelector('#stats').innerHTML;
check('线索分母 4/5', /4\/5<\/b><span>语境线索充分/.test(stats));
check('fragile 标成 skip 而非 miss', /class="chip skip"[^>]*>fragile/.test(stats));
check('用时与调用数写进 meta', /\d+ 秒 · 6 次模型调用 · 24310 tokens/.test(doc.querySelector('#progressMeta').textContent),
      doc.querySelector('#progressMeta').textContent);
check('成功 toast', [...doc.querySelectorAll('#toasts .toast')].some(t => t.textContent === '生成完成'));

console.log(`\n${ok}/${ok+fail} 通过`);
process.exit(fail ? 1 : 0);
