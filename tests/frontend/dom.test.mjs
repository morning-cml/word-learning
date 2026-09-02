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
}

/* ============================ 设置页 ============================ */
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
  check('浮层给了释义和等级',
        /abandon/.test(tip().textContent) && !!tip().querySelector('.lv'),
        tip().textContent.replace(/\s+/g, ' ').trim().slice(0, 40));
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

console.log(`\n${ok}/${ok + fail} 通过`);
process.exit(fail ? 1 : 0);
