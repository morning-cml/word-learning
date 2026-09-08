/* 用 jsdom 跑「服务端真实渲染的 HTML + 真实前端模块」，无浏览器验证前端逻辑。 */
import { JSDOM } from 'jsdom';
import { readFileSync } from 'fs';
import { pathToFileURL } from 'url';

const API = JSON.parse(readFileSync('./.fixtures/api.json', 'utf8'));
const JS = (p) => pathToFileURL(`./.fixtures/static/js/${p}`).href + `?t=${Math.random()}`;

let ok = 0, fail = 0;
const check = (name, cond, extra = '') => {
  cond ? ok++ : fail++;
  console.log(`  ${cond ? 'ok  ' : 'FAIL'} ${name}${extra ? '  ' + extra : ''}`);
};

/** 建一个带真实 HTML 的 DOM，并把 fetch 打桩到导出的 API 夹具上。 */
function boot(page, { routes = {}, onFetch } = {}) {
  const dom = new JSDOM(readFileSync(`./.fixtures/${page}.html`, 'utf8'), {
    url: 'http://127.0.0.1:8000/', pretendToBeVisual: true,
  });
  const w = dom.window;
  const calls = [];
  w.fetch = async (url, opts = {}) => {
    const path = String(url);
    calls.push({ path, method: opts.method || 'GET', body: opts.body });
    if (onFetch) { const r = onFetch(path, opts); if (r !== undefined) return r; }
    const data = routes[path] ?? API[path];
    if (data === undefined) return { ok: false, status: 404, json: async () => ({ detail: 'not found' }) };
    return { ok: true, status: 200, json: async () => data };
  };
  // Node 24 里 navigator 是只读的 getter，赋值会抛；跳过赋不上的即可
  for (const k of ['window', 'document', 'navigator', 'localStorage', 'matchMedia',
                   'requestAnimationFrame', 'getComputedStyle', 'Node', 'Element',
                   'HTMLElement', 'KeyboardEvent', 'MouseEvent', 'Event', 'fetch', 'TextDecoder']) {
    // jsdom 的 window 上没有的（比如 TextDecoder）不能拷：拷过去是 undefined，
    // 反而把 Node 自带的实现盖掉了。只读全局（navigator）赋值会抛，一并忽略。
    if (w[k] === undefined) continue;
    try { globalThis[k] = w[k]; } catch (e) { /* 只读全局，忽略 */ }
  }
  globalThis.scrollBy = () => {};
  w.scrollBy = () => {};
  w.HTMLElement.prototype.scrollIntoView = () => {};
  return { dom, w, doc: w.document, calls };
}

const q = (ctx, sel) => ctx.doc.querySelector(sel);
const qa = (ctx, sel) => [...ctx.doc.querySelectorAll(sel)];

/* ============================ core / api ============================ */
console.log('1. core 工具');
{
  const ctx = boot('index');
  const core = await import(JS('core.js'));
  check('escapeHtml 转义尖括号与引号',
        core.escapeHtml('<b>&"x"</b>') === '&lt;b&gt;&amp;&quot;x&quot;&lt;/b&gt;');
  check('escapeRe 转义正则元字符', core.escapeRe('a.b*c') === 'a\\.b\\*c');
  check('toast 渲染到 #toasts', (core.toast('测试', 'ok'), qa(ctx, '#toasts .toast.ok').length === 1));
  check('fmtTime 解析 ISO', /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(core.fmtTime('2026-09-01T09:20:00')));
  check('fmtTime 容忍空值', core.fmtTime('') === '' && core.fmtTime('乱码') === '');
}

console.log('\n2. api 错误处理');
{
  const ctx = boot('index', { onFetch: (p) => p === '/boom'
    ? { ok: false, status: 500, json: async () => ({ detail: '服务端炸了' }) } : undefined });
  const api = await import(JS('api.js'));
  let msg = '';
  try { await api.get('/boom'); } catch (e) { msg = e.message; }
  check('把 FastAPI 的 detail 取出来当报错', msg === '服务端炸了', msg);
  const ctx2 = boot('index', { onFetch: () => { throw new TypeError('Failed to fetch'); } });
  const api2 = await import(JS('api.js'));
  let msg2 = '';
  try { await api2.get('/x'); } catch (e) { msg2 = e.message; }
  check('网络层失败给出人话', msg2.includes('连不上本地服务'), msg2);
}

/* ============================ 阅读页 ============================ */
console.log('\n3. 阅读页（真实文章数据）');
{
  const ctx = boot('reader', { routes: { '/api/articles/1': API['/api/articles/1'] } });
  const mod = await import(JS('pages/reader.js'));
  await mod.init({ articleId: '1' });

  const tws = qa(ctx, '.tw');
  check('渲染出目标词', tws.length > 0, `${tws.length} 个`);
  check('每个目标词都有明文层和挖空层',
        tws.every((t) => t.querySelector('.tw-word') && t.querySelector('.tw-blank')));
  check('句子分中英两层', qa(ctx, '.s .en').length === qa(ctx, '.s .zh').length);
  check('标题渲染的是夹具里那篇', q(ctx, '.doc h1').textContent === 'The Wrong Number');
  check('目标词就是 abandon 与 silence',
        tws.map(t => t.dataset.lemma).sort().join() === 'abandon,silence');

  const modeBtn = (m) => q(ctx, `#modes button[data-mode="${m}"]`);
  modeBtn('zh').dispatchEvent(new ctx.w.MouseEvent('click', { bubbles: true }));
  check('切中文：doc 带 mode-zh 且句子 zh-on',
        q(ctx, '#doc').className.includes('mode-zh') && q(ctx, '.s').classList.contains('zh-on'));

  modeBtn('cloze').dispatchEvent(new ctx.w.MouseEvent('click', { bubbles: true }));
  check('切回忆：挖空且中文关掉',
        q(ctx, '#doc').className.includes('mode-cloze') && !q(ctx, '.s').classList.contains('zh-on'));
  check('进度条出现', !q(ctx, '#progress').hidden && /已揭示 0\//.test(q(ctx, '#progress').textContent));

  tws[0].dispatchEvent(new ctx.w.MouseEvent('click', { bubbles: true }));
  check('点挖空 → 揭示', tws[0].classList.contains('revealed'));
  check('揭示后进度更新', /已揭示 1\//.test(q(ctx, '#progress').textContent));
  check('第一下不开面板', !q(ctx, '#panel').classList.contains('open'));

  // 焦点在挖空词上按空格：应只揭示，不跳模式
  const before = q(ctx, '#doc').className;
  tws[1].dispatchEvent(new ctx.w.KeyboardEvent('keydown',
    { key: ' ', code: 'Space', bubbles: true, cancelable: true }));
  check('空格揭示且不跳出回忆模式',
        tws[1].classList.contains('revealed') && q(ctx, '#doc').className === before);

  q(ctx, '#reveal').dispatchEvent(new ctx.w.MouseEvent('click', { bubbles: true }));
  check('全部揭示', qa(ctx, '.tw.revealed').length === tws.length);

  // 已揭示的词再点 → 开词条面板
  const lemma = tws[0].dataset.lemma;
  ctx.w.fetch = async (u) => ({ ok: true, status: 200,
    json: async () => API[`/api/words/${API.__lemma__}`] });
  globalThis.fetch = ctx.w.fetch;
  tws[0].dispatchEvent(new ctx.w.MouseEvent('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 20));
  check('已揭示后再点 → 开词条面板', q(ctx, '#panel').classList.contains('open'));
  check('面板画出语境', qa(ctx, '#panel .ctx').length > 0 || !!q(ctx, '#panel .empty'));
  check('面板有掌握程度按钮', qa(ctx, '#panel button[data-status]').length === 7);

  q(ctx, '#marks').dispatchEvent(new ctx.w.MouseEvent('click', { bubbles: true }));
  check('高亮开关生效', q(ctx, '#doc').className.includes('hide-marks')
        && q(ctx, '#marks').textContent.includes('关'));
}

/* ============================ 词库页 ============================ */
console.log('\n4. 词库页（筛选 / 搜索）');
{
  const ctx = boot('words');
  const mod = await import(JS('pages/words.js'));
  await mod.init({});
  const rows = () => qa(ctx, '#table tbody tr');
  const total = (API['/api/words'].words || []).length;
  check('渲染三个词条', rows().length === 3 && total === 3, `${rows().length}/${total}`);
  check('汇总条出现', !q(ctx, '#summaryCard').hidden);
  check('掌握程度画成点或标签',
        rows().every((r) => r.querySelector('.meter') || r.querySelector('.tag')));
  //  书脊靠 data-level 着色 + 分线型。属性没打上的话整列退成一条灰边，
  //  而那是**看不出来的**——它本来就淡，少了也不像坏了。
  check('每行带着 CEFR 等级（书脊靠它着色）',
        rows().every((r) => r.dataset.level !== undefined),
        rows().map((r) => r.dataset.level || '空').join(','));

  const multiBtn = q(ctx, '#filters button[data-filter="multi"]');
  multiBtn.dispatchEvent(new ctx.w.MouseEvent('click', { bubbles: true }));
  //  夹具里 abandon 出现在两篇，silence 与 confess 各一篇
  check('筛选「多语境」只剩 abandon',
        rows().length === 1 && rows()[0].dataset.lemma === 'abandon',
        rows().map(r => r.dataset.lemma).join());
  check('筛选按钮高亮唯一', qa(ctx, '#filters button.on').length === 1);

  q(ctx, '#filters button[data-filter="all"]').dispatchEvent(new ctx.w.MouseEvent('click', { bubbles: true }));
  const target = (API['/api/words'].words || [])[0].lemma;
  q(ctx, '#search').value = target;
  q(ctx, '#search').dispatchEvent(new ctx.w.Event('input', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 220));
  check('搜索命中', rows().length >= 1 && rows().some((r) => r.dataset.lemma === target));
}

/* ============================ 文库页 ============================ */
console.log('\n5. 文库页');
{
  const ctx = boot('library');
  const mod = await import(JS('pages/library.js'));
  await mod.init({});
  const rows = qa(ctx, '#table tbody tr');
  check('渲染两篇文章', rows.length === 2, `${rows.length} 行`);
  check('线索列显示 2/2 与 1/2',
        rows.map(r => r.children[3].textContent.trim()).sort().join() === '1/2,2/2',
        rows.map(r => r.children[3].textContent.trim()).join());
  check('每行有删除按钮', rows.every((r) => r.querySelector('.del')));
  check('线索列有值', rows.some((r) => /\d+\/\d+/.test(r.children[3].textContent)));
  check('标题链到阅读页', rows[0].querySelector('a[href^="/read/"]') !== null);
  check('每行带着 CEFR 等级（书脊靠它着色）',
        rows.every((r) => r.dataset.level !== undefined),
        rows.map((r) => r.dataset.level || '空').join(','));
}

/* -------- 删除的二次确认 --------
   删一篇会连着它攒下的语境一起删（Encounter 跟着 Sentence 级联走），
   而那是一次次阅读攒出来的、删了补不回来。原来一次点击就没了，
   按钮平时还是隐形的——整个应用里最不可逆的动作，阻力最小。 */
{
  const impact = { contexts: 5, words: 3, orphaned: ['nostalgia', 'scrutiny'] };
  //  按路径形状打桩，不写死 id：夹具里的文章 id 不归这条测试管
  const ctx = boot('library', { onFetch: (path) =>
    path.endsWith('/impact') ? { ok: true, status: 200, json: async () => impact } : undefined });
  const mod = await import(JS('pages/library.js'));
  await mod.init({});

  const tr = q(ctx, '#table tbody tr');
  const id = tr.dataset.id;
  tr.querySelector('.del').dispatchEvent(new ctx.w.MouseEvent('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 20));

  check('第一下不删', !ctx.calls.some((c) => c.method === 'DELETE'));
  check('先问了这一篇的代价', ctx.calls.some((c) => c.path === `/api/articles/${id}/impact`));
  const warn = tr.querySelector('.del-warn');
  check('确认框出现', warn && !warn.hidden);
  check('说清了要丢多少语境', /5/.test(warn.textContent) && /语境/.test(warn.textContent));
  check('点名了只在这一篇里出现过的词',
        /nostalgia/.test(warn.textContent) && /scrutiny/.test(warn.textContent));
  check('确认框在标题那一格，不撑宽操作列', warn.closest('td') === tr.children[0]);

  //  取消要能真的退出去
  tr.querySelector('.cancel-del').dispatchEvent(new ctx.w.MouseEvent('click', { bubbles: true }));
  check('取消后确认框收起', tr.querySelector('.del-warn').hidden);
  check('取消之后仍然没有发删除请求', !ctx.calls.some((c) => c.method === 'DELETE'));

  //  再点一次 → 确认 → 才真删
  tr.querySelector('.del').dispatchEvent(new ctx.w.MouseEvent('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 20));
  tr.querySelector('.confirm-del').dispatchEvent(new ctx.w.MouseEvent('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 20));
  check('确认之后才发 DELETE',
        ctx.calls.filter((c) => c.method === 'DELETE' && c.path === `/api/articles/${id}`).length === 1);
}

{
  //  Esc 也要能退出去：确认态是个模式，得有一条不用找鼠标的出路
  const ctx = boot('library', { onFetch: (path) => path.endsWith('/impact')
    ? { ok: true, status: 200, json: async () => ({ contexts: 1, words: 1, orphaned: [] }) } : undefined });
  const mod = await import(JS('pages/library.js'));
  await mod.init({});
  const tr = q(ctx, '#table tbody tr');
  tr.querySelector('.del').dispatchEvent(new ctx.w.MouseEvent('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 20));
  check('Esc 之前是展开的', !tr.querySelector('.del-warn').hidden);
  ctx.doc.dispatchEvent(new ctx.w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  check('Esc 收起确认框', tr.querySelector('.del-warn').hidden);
  check('Esc 之后没有发删除请求', !ctx.calls.some((c) => c.method === 'DELETE'));
}

{
  //  代价算不出来（接口挂了）也得能删，只是如实说「说不清丢的是什么」
  const ctx = boot('library', { onFetch: (path) =>
    path.endsWith('/impact') ? { ok: false, status: 500, json: async () => ({}) } : undefined });
  const mod = await import(JS('pages/library.js'));
  await mod.init({});
  const tr = q(ctx, '#table tbody tr');
  tr.querySelector('.del').dispatchEvent(new ctx.w.MouseEvent('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 20));
  const warn = tr.querySelector('.del-warn');
  check('算不出代价仍然给得出确认框', !warn.hidden && warn.querySelector('.confirm-del') !== null);
  check('并且如实说没算出来', /没算出来|找不回来/.test(warn.textContent));
}

/* ============================ 设置页 ============================ */
/* -------- 阅读走势 --------
   单序列日柱。**空档必须画出来**——接口只回有文章的那几天，而没读的那些天
   正是要看的东西（「连续几天」就是从空档来的）。前端不补齐的话，
   一条断断续续的记录会被画成一条连续的柱阵，看着像天天都在读。 */
{
  const days = [
    { date: '2026-09-01', words: 200, articles: 1, total: 200 },
    { date: '2026-09-03', words: 450, articles: 2, total: 650 },
    { date: '2026-09-04', words: 120, articles: 1, total: 770 },
  ];
  const ctx = boot('library', { routes: {
    '/api/reading/history?days=56': {
      days, streak: 2, total_words: 770, total_articles: 4, missing_word_count: 2 },
  } });
  const mod = await import(JS('pages/library.js'));
  await mod.init({});
  await new Promise((r) => setTimeout(r, 40));

  check('走势区出现', !q(ctx, '#reading').hidden);
  const bars = qa(ctx, '#readingDays .reading-day');
  check('按天补齐了空档，不是只画有数据的三天', bars.length === 56, `${bars.length} 根`);
  const zeros = qa(ctx, '#readingDays .reading-day.zero');
  check('没读的那些天标成 zero', zeros.length === 53, `${zeros.length} 天`);
  check('最后一天是接口给的最后一天',
        bars[bars.length - 1].dataset.date === '2026-09-04',
        bars[bars.length - 1].dataset.date);

  check('累计词数报出来', /770/.test(q(ctx, '#readingTotals').textContent));
  check('连续天数报出来', /连续.*2.*天/.test(q(ctx, '#readingTotals').textContent),
        q(ctx, '#readingTotals').textContent);
  //  早期文章没记字数，不说的话那几天画成 0，看着像那天没读
  check('没有字数记录的文章如实说明',
        /2 篇/.test(q(ctx, '#readingNote').textContent), q(ctx, '#readingNote').textContent);

  //  只标最高那一天，绝不每根柱子都标数（dataviz：直接标注要稀疏才起作用）
  check('只直接标出最高的那一天', qa(ctx, '#readingPlot .reading-peak').length === 1);
  check('峰值标的是 450', /450/.test(q(ctx, '#readingPlot .reading-peak').textContent));

  //  柱子很细，悬停必须给得出是哪一天
  const bar = bars.find((b) => b.dataset.date === '2026-09-03');
  bar.dispatchEvent(new ctx.w.MouseEvent('mouseover', { bubbles: true }));
  check('悬停给出这一天的明细',
        q(ctx, '#readingTip').classList.contains('show')
        && /2026-09-03/.test(q(ctx, '#readingTip').textContent)
        && /450/.test(q(ctx, '#readingTip').textContent),
        q(ctx, '#readingTip').textContent);
  const empty = bars.find((b) => b.classList.contains('zero'));
  empty.dispatchEvent(new ctx.w.MouseEvent('mouseover', { bubbles: true }));
  check('空档也说得出是哪一天', /这天没读/.test(q(ctx, '#readingTip').textContent));
}

/* 一条记录都没有时整块不出现：画一条全零的线比不画更糟——
   它看着像「读了但都是 0 词」。 */
{
  const ctx = boot('library', { routes: {
    '/api/reading/history?days=56': {
      days: [], streak: 0, total_words: 0, total_articles: 0, missing_word_count: 0 },
  } });
  const mod = await import(JS('pages/library.js'));
  await mod.init({});
  await new Promise((r) => setTimeout(r, 40));
  check('没有阅读记录时整块不出现', q(ctx, '#reading').hidden);
}

console.log('\n6. 设置页');
{
  const ctx = boot('settings');
  const mod = await import(JS('pages/settings.js'));
  await mod.init({});
  check('提供商下拉已填', qa(ctx, '#provider option').length === API['/api/settings'].providers.length);
  check('模型下拉已填', qa(ctx, '#model option').length > 0);
  check('Key 用掩码做 placeholder', /已保存|粘贴/.test(q(ctx, '#key').placeholder));
  check('词表 / 备份状态已渲染', q(ctx, '#dataInfo').children.length >= 2);
  check('备份卡片提到快照', /快照|备份/.test(q(ctx, '#dataInfo').textContent));

  q(ctx, '#check').dispatchEvent(new ctx.w.MouseEvent('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 20));
  check('四层检验预画 4 步', qa(ctx, '#steps .check-step').length === 4);
}

/* ============================ stats 组件 ============================ */
console.log('\n7. 结果面板');
{
  const ctx = boot('index');
  const { renderStats } = await import(JS('components/stats.js'));
  const host = ctx.doc.createElement('div');
  renderStats(host, {
    clue_strength: { strong: 3, weak: 1, none: 0 }, targets_hit: 4, targets_total: 5,
    offender_rate: 0.021, word_count: 210, sentence_count: 12, tokens: 19467,
    targets_missed: ['fragile'], unplaced: ['fragile'],
    offenders: [{ surface: 'quixotic', level: null }], using_real_cefr: true,
  }, ['abandon', 'silence', 'confess', 'hesitate', 'fragile']);
  check('线索分母正确', /3\/4<\/b><span>语境线索充分/.test(host.innerHTML));
  check('被放掉的词标成 skip', /class="chip skip"[^>]*>fragile/.test(host.innerHTML));
  check('命中的词标成 hit', /class="chip hit">abandon/.test(host.innerHTML));
  check('线索不足有提醒', host.textContent.includes('语境线索不足'));
  check('unplaced 有专门说明', host.textContent.includes('没有硬塞'));
  check('超纲词列出', host.textContent.includes('quixotic'));
  check('顺风路径不提重写', !host.textContent.includes('被重写过'));
}

/* -------- 这一篇重写过几次 --------
   修复和补线索在顺风路径上一次都不发生，发生了就是那一段没写对、花了第二次钱
   重写它。两个数一直算着也一直入库，却只在生成当时的时间线里露过面——从文库
   打开一篇旧文章就看不到了，而那才是真要判断它值不值得读的时候。 */
{
  const ctx = boot('index');
  const { renderStats } = await import(JS('components/stats.js'));
  const el = q(ctx, '#stats');
  const base = { targets_hit: 2, targets_total: 2, word_count: 200,
                 sentence_count: 12, offender_rate: 0 };

  renderStats(el, { ...base, repairs: 2, clue_fixes: 1 }, ['abandon']);
  check('重写次数报出来', /被重写过/.test(el.textContent) && /3/.test(el.textContent),
        el.textContent.replace(/\s+/g, ' ').slice(0, 120));
  check('分别说清是哪一种',
        /2 次校验没过/.test(el.textContent) && /1 次线索不足/.test(el.textContent));
  check('报成需要留意而不是好消息',
        el.querySelector('.note.warn') !== null && el.querySelector('.note.ok') === null);

  //  只有一种的时候不能算出 NaN（另一个字段根本不存在）
  renderStats(el, { ...base, repairs: 2 }, ['abandon']);
  check('只有一种时数目仍然对', /被重写过/.test(el.textContent) && !/NaN/.test(el.textContent),
        el.textContent.replace(/\s+/g, ' ').slice(0, 90));

  renderStats(el, { ...base, repairs: 0, clue_fixes: 0 }, ['abandon']);
  check('一次都没重写就不占地方', !/被重写过/.test(el.textContent));
  renderStats(el, base, ['abandon']);
  check('老文章没有这两个字段时不报', !/被重写过/.test(el.textContent));
}

/* -------- JSON 兜底层的仪表 --------
   jsonfix 有五层兜底，一直没有任何仪表——第 1b 条那条「第 4 层一直在跑、
   但一次都没成功过」是靠人工翻代码发现的。层名现在记进了 stats，
   这里验最后一环：非顺风的层要报出来，而且**各报各的原因**——
   截断是 max_tokens 不够，不合法是模型吐格式不稳，两件事不能合成一句。 */
{
  const ctx = boot('index');
  const { renderStats } = await import(JS('components/stats.js'));
  const el = q(ctx, '#stats');
  const base = { targets_hit: 2, targets_total: 2, word_count: 200,
                 sentence_count: 12, offender_rate: 0 };

  renderStats(el, { ...base, json_layers: { direct: 6 } }, ['abandon']);
  check('全是顺风就不占地方', !/不是原样就能解析/.test(el.textContent));

  renderStats(el, { ...base, json_layers: { direct: 4, truncated: 2 } }, ['abandon']);
  check('截断被报出来', /输出被截断/.test(el.textContent));
  check('顺带说清该去查什么', /max_tokens/.test(el.textContent));
  check('顺风的那一层不当成问题报', !/direct/.test(el.textContent));

  renderStats(el, { ...base, json_layers: { fence: 1, repaired: 2 } }, ['abandon']);
  check('两种毛病分开说',
        /markdown 围栏/.test(el.textContent) && /JSON 本身不合法/.test(el.textContent));
  check('次数加总对', /有 3 次不是原样就能解析/.test(el.textContent),
        el.textContent.replace(/\s+/g, ' ').slice(0, 100));

  renderStats(el, base, ['abandon']);
  check('老文章没有 json_layers 时不报', !/不是原样就能解析/.test(el.textContent));
}


/* -------- 复现的旧词 --------
   学过的词在新文章里又出现，是这个产品声称最有效的机制（多语境重复），
   可它以前完全不可见：要么被判成超纲列出来，要么被修复指令删掉。 */
{
  const ctx = boot('index');
  const { renderStats } = await import(JS('components/stats.js'));
  const el = q(ctx, '#stats');

  renderStats(el, { targets_hit: 2, targets_total: 2, word_count: 100, sentence_count: 6,
                    offender_rate: 0, revisited: ['tedious', 'resilient'] }, ['abandon']);
  check('复现的旧词被报出来', /tedious/.test(el.textContent) && /resilient/.test(el.textContent));
  check('说清了它们不计入超纲', /不计入超纲/.test(el.textContent));
  check('报成好消息而不是问题', el.querySelector('.note.ok') !== null);

  renderStats(el, { targets_hit: 1, targets_total: 1, word_count: 50, sentence_count: 3,
                    offender_rate: 0 }, ['abandon']);
  check('老文章没有这个字段时不报', !/以前学过的词/.test(el.textContent));
}

/* -------- 超纲词可点：「这个词不用管」 --------
   入口放在超纲词列表上，因为**问题就是在这里报出来的**。而且实测被误报的
   多数不是人名，是标尺自己不认识的词（CEFR-J 只有 8653 条）。 */
{
  const ctx = boot('index');
  const { renderStats } = await import(JS('components/stats.js'));
  const el = q(ctx, '#stats');
  const stats = { targets_hit: 1, targets_total: 1, word_count: 90, sentence_count: 5,
                  offender_rate: 0.02,
                  offenders: [{ lemma: 'nora', surface: 'Nora', level: null },
                              { lemma: 'toward', surface: 'toward', level: null }] };

  //  不接回调时不该给出点不动的按钮
  renderStats(el, stats, ['abandon']);
  check('没接回调时超纲词不可点', el.querySelector('button[data-ignore]') === null);

  const asked = [];
  renderStats(el, stats, ['abandon'], { onIgnore: (lemma) => { asked.push(lemma); } });
  const btns = [...el.querySelectorAll('button[data-ignore]')];
  check('超纲词渲染成可点的按钮', btns.length === 2);
  check('按钮带的是还原后的原形，不是表面形态', btns[0].dataset.ignore === 'nora');
  check('说清了点了会怎样', /不用管/.test(el.textContent));

  btns[0].dispatchEvent(new ctx.w.MouseEvent('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 20));
  check('点一下就把这个词交出去', asked.join() === 'nora');
  check('点过的标成已处理', btns[0].classList.contains('ignored'));
  check('等级徽章没被文字覆盖冲掉', btns[0].textContent.includes('Nora'));

  //  重渲染之后回调要换成最新那个，监听器只挂一次
  const later = [];
  renderStats(el, stats, ['abandon'], { onIgnore: (lemma) => { later.push(lemma); } });
  el.querySelector('button[data-ignore]').dispatchEvent(new ctx.w.MouseEvent('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 20));
  check('重渲染后用的是新回调，且没有重复触发', later.join() === 'nora' && asked.length === 1);
}

/* ============================ 阅读器：热键与排版 ============================
   jsdom 没有布局引擎，量不了「面板还盖不盖得住正文」（那条用 puppeteer 量了，
   见 需要注意.md 第 16 条）。这里验的是它验得了的部分：事件流、DOM 状态、
   以及写没写进 localStorage。 */
console.log('\n8. 阅读器：掌握程度热键与排版控件');
{
  const detail = API[`/api/words/${API.__lemma__}`];
  const posted = [];
  const ctx = boot('reader', {
    routes: { '/api/articles/1': API['/api/articles/1'] },
    onFetch: (p, opts) => {
      if (p.endsWith('/status')) {
        posted.push(JSON.parse(opts.body).status);
        return { ok: true, status: 200, json: async () => ({ status: posted.at(-1) }) };
      }
      if (p.startsWith('/api/words/')) return { ok: true, status: 200, json: async () => detail };
      return undefined;
    },
  });
  const mod = await import(JS('pages/reader.js'));
  await mod.init({ articleId: '1' });

  const key = (k) => ctx.doc.dispatchEvent(
    new ctx.w.KeyboardEvent('keydown', { key: k, code: `Digit${k}`, bubbles: true, cancelable: true }));
  const wait = () => new Promise((r) => setTimeout(r, 30));

  qa(ctx, '.tw')[0].dispatchEvent(new ctx.w.MouseEvent('click', { bubbles: true }));
  await wait();
  check('开面板时给 body 打标记（正文靠它让位）',
        ctx.doc.body.classList.contains('panel-open'));

  const modeBefore = q(ctx, '#doc').className;
  key('3');
  await wait();
  check('面板开着按 3 → 改掌握程度', posted.at(-1) === 3, `posted=${posted}`);
  check('同一下按键不会顺手切模式', q(ctx, '#doc').className === modeBefore,
        `${modeBefore} -> ${q(ctx, '#doc').className}`);
  check('面板里把热键写出来了', /1.*2.*3.*4.*5.*W.*I/.test(q(ctx, '.panel-keys')?.textContent || ''));

  ctx.doc.dispatchEvent(new ctx.w.KeyboardEvent('keydown', { key: 'w', bubbles: true }));
  await wait();
  check('W = 已掌握（98）', posted.at(-1) === 98, `posted=${posted}`);

  ctx.doc.dispatchEvent(new ctx.w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  await wait();
  check('关面板时撤掉 body 标记', !ctx.doc.body.classList.contains('panel-open'));

  const n = posted.length;
  key('3');
  await wait();
  check('面板关掉后，3 重新是「对照」模式', q(ctx, '#doc').className.includes('mode-both'));
  check('而且没有再改掌握程度', posted.length === n);

  /* ---- 排版控件 ---- */
  const rootStyle = () => ctx.doc.documentElement.style.getPropertyValue('--reader-size');
  check('初始就把字号写进 CSS 变量', rootStyle() === '19px', rootStyle());
  q(ctx, '#typo').dispatchEvent(new ctx.w.MouseEvent('click', { bubbles: true }));
  check('点 Aa 展开控件', !q(ctx, '#typoPop').hidden);
  q(ctx, 'button[data-typo="size"][data-step="1"]')
    .dispatchEvent(new ctx.w.MouseEvent('click', { bubbles: true }));
  check('调大一档 → 21px', rootStyle() === '21px', rootStyle());
  check('存进 localStorage', ctx.w.localStorage.getItem('wl-reader-size') === '3',
        ctx.w.localStorage.getItem('wl-reader-size'));
  q(ctx, '#typoReset').dispatchEvent(new ctx.w.MouseEvent('click', { bubbles: true }));
  check('恢复默认回到 19px', rootStyle() === '19px', rootStyle());

  for (let i = 0; i < 4; i++)
    q(ctx, 'button[data-typo="size"][data-step="1"]')
      .dispatchEvent(new ctx.w.MouseEvent('click', { bubbles: true }));
  check('到顶就禁用，不会越界',
        q(ctx, 'button[data-typo="size"][data-step="1"]').disabled && rootStyle() === '23px',
        rootStyle());
}


/* ============================ 悬停浮层与专注模式 ============================
   浮层的定位（在词上方 / 不跑出视口）用 puppeteer 量了，jsdom 没有布局引擎。
   这里验它验得了的：事件流、缓存、以及那两条「什么时候**不能**显示」的规则。 */
console.log('\n9. 悬停浮层与专注模式');
{
  const detail = API[`/api/words/${API.__lemma__}`];
  let wordFetches = 0;
  const ctx = boot('reader', {
    routes: { '/api/articles/1': API['/api/articles/1'] },
    onFetch: (p, opts) => {
      if (p.endsWith('/status')) return { ok: true, status: 200, json: async () => ({}) };
      if (p.startsWith('/api/words/')) {
        wordFetches += 1;
        return { ok: true, status: 200, json: async () => detail };
      }
      return undefined;
    },
  });
  const mod = await import(JS('pages/reader.js'));
  await mod.init({ articleId: '1' });

  const tw = () => qa(ctx, '.tw')[0];
  const tip = () => ctx.doc.querySelector('.tw-tip');
  const mouse = (el, type) => el.dispatchEvent(new ctx.w.MouseEvent(type, { bubbles: true }));
  const shown = () => !!tip() && tip().classList.contains('show');
  const settle = (ms) => new Promise((r) => setTimeout(r, ms));

  mouse(tw(), 'mouseover');
  await settle(80);
  check('不立刻弹（鼠标扫过一行会经过好几个词）', !shown());
  await settle(500);
  check('停留够久才弹', shown());
  //  等级徽章只在词条真有 CEFR 等级时才渲染，而那取决于机器上有没有下载
  //  CEFR-J（CI 上没有，退回兜底表后 abandon 查不到等级）。断言写成
  //  「有数据时才要求它出现」，而不是写死一个只在本机成立的值——
  //  这正是 需要注意.md 第 17 条那个坑，第一版就踩进去了，CI 抓出来的。
  const hasCefr = !!detail.cefr;
  check('浮层给了词、释义和见过次数（有等级数据时还给徽章）',
        /abandon/.test(tip().textContent)
        && /抛弃/.test(tip().textContent)
        && /见过 2 次/.test(tip().textContent)
        && (!hasCefr || !!tip().querySelector('.lv')),
        tip().textContent.replace(/\s+/g, ' ').trim().slice(0, 44));
  check('浮层挂在 body 上，不在正文里',
        tip().parentElement === ctx.doc.body,
        tip().parentElement?.tagName);

  mouse(tw(), 'mouseout');
  await settle(60);
  check('移开就收起', !shown());

  const before = wordFetches;
  mouse(tw(), 'mouseover'); await settle(500);
  mouse(tw(), 'mouseout');  await settle(60);
  mouse(tw(), 'mouseover'); await settle(500);
  check('同一个词只查一次接口', wordFetches === before,
        `又发了 ${wordFetches - before} 次`);

  // 点开面板时浮层要走：点击不触发 mouseout，不管的话两套 UI 会同屏
  mouse(tw(), 'click');
  await settle(60);
  check('点击后浮层收起', !shown());
  ctx.doc.dispatchEvent(new ctx.w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  await settle(60);

  /* ---- 回忆模式：没揭示的词绝不能给 ---- */
  ctx.doc.dispatchEvent(new ctx.w.KeyboardEvent('keydown',
    { key: '4', code: 'Digit4', bubbles: true }));
  await settle(60);
  check('已切到回忆模式', q(ctx, '#doc').className.includes('mode-cloze'));
  mouse(tw(), 'mouseover');
  await settle(500);
  check('没揭示的词不给浮层（给了等于把答案摆出来）', !shown());
  mouse(tw(), 'mouseout'); await settle(60);
  tw().classList.add('revealed');
  mouse(tw(), 'mouseover');
  await settle(500);
  check('揭示之后才给', shown());
  mouse(tw(), 'mouseout'); await settle(60);

  /* ---- 专注模式 ---- */
  const btn = q(ctx, '#focus');
  check('默认不在专注模式', !ctx.doc.body.classList.contains('focus-mode'));
  ctx.doc.dispatchEvent(new ctx.w.KeyboardEvent('keydown', { key: 'f', bubbles: true }));
  await settle(60);
  check('按 F 进入专注模式', ctx.doc.body.classList.contains('focus-mode'));
  check('按钮同步成「退出专注」并标了 aria-pressed',
        btn.textContent.trim() === '退出专注' && btn.getAttribute('aria-pressed') === 'true',
        `${btn.textContent.trim()} / ${btn.getAttribute('aria-pressed')}`);
  check('存进 localStorage', ctx.w.localStorage.getItem('wl-reader-focus') === '1');
  btn.dispatchEvent(new ctx.w.MouseEvent('click', { bubbles: true }));
  await settle(60);
  check('点按钮退出', !ctx.doc.body.classList.contains('focus-mode')
        && ctx.w.localStorage.getItem('wl-reader-focus') === '0');
}

/* ============================ hidden 真的藏得住吗 ============================

   `hidden` 靠的是 UA 样式表里那条 `[hidden] { display: none }`，而**作者样式
   无论特异性高低都盖过 UA 样式**。所以只要某个类写了 display（flex / grid /
   inline-flex…），挂在它身上的 hidden 就当场失效——元素照常显示，而且点得动。

   这条不属于 CLAUDE.md 说的「jsdom 测不了视觉」：它测的不是布局（jsdom 确实
   没有布局引擎，offsetHeight 恒为 0），而是**层叠的结果**，getComputedStyle
   在简单选择器上是准的。而 `el.hidden === true` 这种断言恰恰验不出它——
   属性是 true，页面上却看得见。

   本仓库已经为这条各打过四个补丁（.del-warn / .empty-state / .inline-new /
   .typo-pop），第五处（.drill-judge）就是被这段测出来的：翻面前那两个判定
   按钮一直摆在那儿，可以没看释义就按「认识」。所以不逐个点名，
   整页扫一遍——以后再有人给某个 hidden 元素加 display，这里会先响。 */
console.log('\n11. hidden 的元素真的藏起来了');
{
  const inlineCss = (html) => html.replace(
    /<link rel="stylesheet" href="\/static\/css\/([^"]+)">/g,
    (_, f) => `<style>${readFileSync(`./.fixtures/static/css/${f}`, 'utf8')}</style>`);

  for (const page of ['index', 'library', 'words', 'settings', 'reader', 'study', 'drill']) {
    const dom = new JSDOM(inlineCss(readFileSync(`./.fixtures/${page}.html`, 'utf8')),
                          { url: 'http://127.0.0.1:8000/', pretendToBeVisual: true });
    const leaked = [...dom.window.document.querySelectorAll('[hidden]')]
      .filter((el) => dom.window.getComputedStyle(el).display !== 'none')
      .map((el) => `${el.tagName.toLowerCase()}#${el.id || '?'}.${el.className}`);
    check(`${page} 页没有「标了 hidden 却还显示」的元素`, leaked.length === 0, leaked.join(' '));
  }
}

console.log(`\n${ok}/${ok + fail} 通过`);
process.exit(fail ? 1 : 0);
