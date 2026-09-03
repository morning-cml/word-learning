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
  { type:'call', purpose:'structured', attempt:1 },
  { type:'plan', topic:'深夜电台', genre:'短篇小说', title_en:'The Wrong Number',
    title_zh:'打错的电话', reason:'这批词有共同的情绪场', paragraphs:2 },
  { type:'phase', phase:'write', index:1, total:2, message:'第 1/2 段：abandon、silence' },
  { type:'call', purpose:'creative', attempt:1 },
  { type:'retry', attempt:1, reason:'空响应：思考用了 3110 tokens，只剩 0 给正文' },
  { type:'call', purpose:'creative', attempt:2 },
  { type:'phase', phase:'repair', index:1, message:'第 1 段校验未过（too_hard），第 1 次修复' },
  { type:'call', purpose:'structured', attempt:1 },
  { type:'phase', phase:'audit', index:1, message:'第 1 段：审查 2 个词的语境线索' },
  { type:'call', purpose:'structured', attempt:1 },
  { type:'paragraph', index:1, paragraph:{ sentences:[{},{}],
    audits:[{lemma:'abandon',strength:'strong'},{lemma:'silence',strength:'strong'}] } },
  { type:'phase', phase:'write', index:2, total:2, message:'第 2/2 段：confess、hesitate、fragile' },
  { type:'call', purpose:'creative', attempt:1 },
  { type:'paragraph', index:2, paragraph:{ sentences:[{}],
    audits:[{lemma:'confess',strength:'weak'}] } },
  { type:'phase', phase:'glossary', message:'生成中文释义' },
  { type:'call', purpose:'structured', attempt:1 },
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
  if (p === '/api/timing') return { ok:true, status:200, json: async () => ({
    samples:3, sec_per_call:36.1, scope:'model' }) };
  if (p === '/api/levels') return { ok:true, status:200, json: async () => ({
    using_real_data: true,
    cumulative: { A1:1064, A2:2307, B1:4446, B2:6863, C1:7777, C2:8653 } }) };
  return { ok:true, status:200, json: async () => ({}) };
};
globalThis.fetch = w.fetch;

const mod = await import(pathToFileURL('./.fixtures/static/js/pages/index.js').href);
await mod.init({});

doc.querySelector('#words').value = 'abandon silence confess hesitate fragile';
doc.querySelector('#go').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
await new Promise(r => setTimeout(r, 300));

/* ---- 用词上限说明：既是文档也是控件 ---- */
console.log('用词上限说明');
const levelRows = [...doc.querySelectorAll('#levels li')];
check('四档都列出来了', levelRows.length === 4, `${levelRows.length} 行`);
check('词汇量是从接口拉的累计值，不是写死的',
      levelRows.map(r => r.querySelector('.lv-n').textContent).join('|') === '2,307 词|4,446 词|6,863 词|7,777 词',
      levelRows.map(r => r.querySelector('.lv-n').textContent).join('|'));
check('默认选中的那一档高亮', doc.querySelector('#levels li.on')?.dataset.level === 'B2');
check('例词用等宽字体列出', levelRows.every(r => r.querySelector('.ex')?.textContent.trim()));

//  点一行要真的切档：两处显示同一个状态却各说各话，比只有下拉框还糟
levelRows[1].dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
check('点一行就切到那一档', doc.querySelector('#level').value === 'B1');
check('高亮跟着走且唯一',
      doc.querySelectorAll('#levels li.on').length === 1
      && doc.querySelector('#levels li.on').dataset.level === 'B1');
doc.querySelector('#levels li[data-level="B2"]').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));

const steps = [...doc.querySelectorAll('#timeline .step')];
const text = doc.querySelector('#timeline').textContent;
console.log('生成页 SSE');
check('时间线渲染出多步', steps.length >= 8, `${steps.length} 步`);
check('切碎的 SSE 分片被正确拼回', text.includes('The Wrong Number'));
check('选题理由显示', text.includes('这批词有共同的情绪场'));
check('重试被标红', [...doc.querySelectorAll('#timeline .step.bad')].some(s => s.textContent.includes('重试')));
check('修复步骤可见', text.includes('第 1 次修复'));
check('逐段线索结论显示', text.includes('abandon=strong') && text.includes('confess=weak'));
//  补线索两轮都没救回来的那一段要看得出来。这个判断原先算了却没接上
//  （三元两个分支都返回空串），于是它和一段全 strong 的长得一模一样
const paraSteps = [...doc.querySelectorAll('#timeline .step')].filter(s => /第 \d+ 段完成/.test(s.textContent));
check('线索没达标的那一段被标出来',
      paraSteps.find(s => s.textContent.includes('confess=weak'))?.classList.contains('bad') === true);
check('全 strong 的那一段不标',
      paraSteps.find(s => s.textContent.includes('abandon=strong'))?.classList.contains('bad') === false);
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

/* ---- 进度条 ----
   它给的是「还要不要接着等」的依据，所以两件事不能出错：
   跑完之前不能显示满格（撒谎），跑完之后必须是满格（看着像卡住了）。 */
console.log('\n进度条');
const fill = doc.querySelector('#progressFill');
check('跑完是满格', fill.style.width === '100%', fill.style.width);
check('跑完标成完成态', fill.className.includes('done'), fill.className);
check('跑完不再显示剩余时间', doc.querySelector('#progressEta').textContent === '');

/* ---- 进度条的算术 ----
   上面验的是终态，这里验中间态。用假时钟推着走，因为真实一次调用要 30-40 秒，
   而条子在这几十秒里怎么动，恰恰是「看着像不像卡住了」的全部。 */
console.log('\n进度条的算术');
{
  const el = () => ({ style: {}, className: '', textContent: '' });
  const fill = el(), stage = el(), eta = el();
  const pg = new mod.Progress({ fill, stage, eta });

  let now = 0;
  const realNow = Date.now;
  Date.now = () => now;

  const pct = () => parseFloat(fill.style.width);
  const seen = [];
  const step = (sec) => { now += sec * 1000; pg.render(); seen.push(pct()); };

  pg.reset(2, 36.1);                       // 2 段 → 顺风 6 次调用；历史 36.1 秒/次
  check('刚开始是 0', pct() === 0, fill.style.width);
  check('有历史就能立刻给出剩余时间', /预计还需/.test(eta.textContent), eta.textContent);

  pg.onCall();                             // 第 1 次调用开始
  step(18); step(18);                      // 飞行途中条子也要走
  check('调用途中条子在往前走', seen[0] > 0 && seen[1] > seen[0], seen.join(' → '));

  pg.onCall();                             // 第 1 次完成、第 2 次开始
  check('完成一次后至少走到 1/6', pct() >= 100 / 6 - 0.5, fill.style.width);

  //  模拟修复 + 补线索：实际调用数超出顺风预估
  for (let i = 0; i < 10; i++) { step(30); pg.onCall(); }
  check('超出预估时不撒谎，封顶 98%', pct() <= 98, fill.style.width);
  check('一路只涨不退', seen.every((v, i) => i === 0 || v >= seen[i - 1]), seen.join(' → '));

  pg.finish();
  check('结束才允许满格', pct() === 100 && fill.className.includes('done'));

  //  没有历史、也还没测到任何一次：宁可不说
  const p2 = new mod.Progress({ fill: el(), stage: el(), eta: (globalThis.__e = el()) });
  p2.reset(2, null);
  check('没有先验时不编一个时间出来',
        !/预计还需/.test(globalThis.__e.textContent), globalThis.__e.textContent);

  Date.now = realNow;
}

console.log(`\n${ok}/${ok + fail} 通过`);
process.exit(fail ? 1 : 0);
