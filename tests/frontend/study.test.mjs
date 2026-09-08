/* 背单词：录入预览、词表顺序、翻卡片。

   这一组的重点只有一个——**顺序**。后端已经保证了它（tests/test_wordbook.py），
   但前端随手一个 .sort() 就能再破一次，而且同样不报错：页面照常渲染，
   只是第 42 页排到了第 43 页后面，用户对着书才发现。
   所以这里断言的是「渲染出来的顺序 == 接口给的顺序」，不是「渲染出来了」。 */
import { JSDOM } from 'jsdom';
import { readFileSync } from 'fs';
import { pathToFileURL } from 'url';

let ok = 0, fail = 0;
const check = (n, c, e = '') => { c ? ok++ : fail++; console.log(`  ${c ? 'ok  ' : 'FAIL'} ${n}${e ? '  ' + e : ''}`); };

/* 接口给的顺序：P42 在前（虽然它是后录的），中间夹一个词典查不到的词。
   两件事都要活着穿过前端。 */
const ENTRIES = [
  { id: 1, idx: 0, page: 42, headword: 'zebra', lemma: 'zebra', phonetic: 'ˈziːbrə',
    translation: 'n. 斑马', inflections: [{ label: '复数', word: 'zebras' }],
    derivatives: ['zebraic'], note: '', status: 0, status_label: '未学',
    right: 0, wrong: 0, extra: {}, due: true, due_at: '' },
  { id: 2, idx: 1, page: 42, headword: 'yacht', lemma: 'yacht', phonetic: 'jɒt',
    translation: 'n. 游艇', inflections: [], derivatives: [], note: '',
    status: 0, status_label: '未学', right: 0, wrong: 0, extra: {}, due: true, due_at: '' },
  { id: 3, idx: 2, page: 43, headword: 'zzzznotaword', lemma: 'zzzznotaword',
    phonetic: '', translation: '', inflections: [], derivatives: [], note: '',
    status: 0, status_label: '未学', right: 0, wrong: 0, extra: { no_dict: true }, due: true, due_at: '' },
  { id: 4, idx: 3, page: 43, headword: 'abandon', lemma: 'abandon', phonetic: 'əˈbændən',
    translation: 'vt. 放弃, 抛弃', inflections: [{ label: '过去式', word: 'abandoned' }],
    derivatives: ['abandonment'], note: '', status: 0, status_label: '未学',
    right: 0, wrong: 0, extra: {}, due: true, due_at: '' },
];

function makeDom(fixture, dataset = {}) {
  const dom = new JSDOM(readFileSync(fixture, 'utf8'),
    { url: 'http://127.0.0.1:8000/', pretendToBeVisual: true });
  const w = dom.window;
  for (const k of ['window', 'document', 'localStorage', 'matchMedia', 'requestAnimationFrame',
                   'Node', 'Element', 'HTMLElement', 'KeyboardEvent', 'MouseEvent', 'Event'])
    { if (w[k] !== undefined) { try { globalThis[k] = w[k]; } catch (e) {} } }
  Object.assign(w.document.body.dataset, dataset);
  return dom;
}

const reviewed = [];
/* 让某一次「加入这一页」返回指定的结果，用来验前端读不读那几个字段 */
let addResult = null;
/* 让下一次「记一笔复习」失败，用来验没记上的那张卡去哪了 */
let failReview = false;

function stubFetch(w) {
  w.fetch = async (url, opts = {}) => {
    const p = String(url);
    const json = (v) => ({ ok: true, status: 200, json: async () => v });
    if (opts.method === 'POST' && /\/api\/wordbook\/units\/\d+\/entries$/.test(p)) {
      return json(addResult ?? { added: 0, missing: [], restored: 0, truncated: false, entries: [] });
    }
    if (p.startsWith('/api/wordbook/preview')) {
      return json({ count: 3, matched: 2, missing: ['zzzznotaword'],
                    sample: [{ headword: 'abandon', lemma: 'abandon', phonetic: '',
                               translation: 'vt. 放弃', fallback: false },
                             { headword: 'thresholds', lemma: 'threshold', phonetic: '',
                               translation: 'n. 门槛', fallback: true }] });
    }
    if (p === '/api/wordbook/status') return json({ dictionary: { ready: true, size: 33217, source: 'ECDICT (MIT)' } });
    if (p === '/api/wordbook/books') return json({ books: [{ id: 1, name: '新东方六级', note: '', source: 'manual', units: 1, total: 4, new: 4, learning: 0, known: 0 }] });
    if (p === '/api/wordbook/books/1/units') return json({ units: [{ id: 9, label: 'List 07', idx: 7, note: '', pages: [42, 43], page_range: 'P42–43', total: 4, new: 4, learning: 0, known: 0 }] });
    if (p.startsWith('/api/wordbook/units/9/entries')) return json({ entries: ENTRIES });
    if (/\/api\/wordbook\/entries\/\d+\/review$/.test(p)) {
      const body = JSON.parse(opts.body);
      reviewed.push({ p, known: body.known, mode: body.mode });
      if (failReview) return { ok: false, status: 500, json: async () => ({ detail: '写不进去' }) };
      //  回的必须是**这一条**：随便回一条的话，前端那句 Object.assign(entry, got)
      //  会把别的词的 headword / id 盖到它身上，后面验顺序的那几条就在验假数据
      const id = Number(p.match(/entries\/(\d+)\//)[1]);
      const e = ENTRIES.find((x) => x.id === id) ?? ENTRIES[0];
      return json({ ...e, status: 1, status_label: '刚认识' });
    }
    if (p === '/api/words') return json({ stats: {}, words: [] });
    return json({});
  };
  globalThis.fetch = w.fetch;
}

/* ------------------------------ 录入页 ------------------------------ */

console.log('背单词 · 录入页');
{
  const dom = makeDom('./.fixtures/study.html');
  stubFetch(dom.window);
  const mod = await import(pathToFileURL('./.fixtures/static/js/pages/study.js').href);
  await mod.init({});
  await new Promise((r) => setTimeout(r, 200));
  const doc = dom.window.document;

  check('词书下拉填上了', doc.querySelector('#bookSel').textContent.includes('新东方六级'));
  check('单元下拉填上了', doc.querySelector('#unitSel').textContent.includes('List 07'));
  check('单元卡带着页码范围', doc.querySelector('#units').textContent.includes('P42–43'));

  //  导出链接必须指向**当前选中**的那本书。不跟着走的话点下去导的是上一本，
  //  而两份 CSV 长得一模一样——用户拿去对着书核才会发现，那时已经在怀疑
  //  自己是不是录错了。
  check('导出链接指向当前这本书',
        doc.querySelector('#exportBook').getAttribute('href') === '/api/wordbook/books/1/export.csv',
        doc.querySelector('#exportBook').getAttribute('href'));
  check('导出链接带 download', doc.querySelector('#exportBook').hasAttribute('download'));
  check('词典状态说出来了', doc.querySelector('#dictNote').textContent.includes('33,217'));

  //  「＋」走内联输入行，不是 prompt()——模态框会把整个页面卡住
  check('新建走内联输入行', doc.querySelector('#newBookRow') !== null
        && doc.querySelector('#newBookRow').hidden === true);
  doc.querySelector('#newBook').dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
  check('点＋展开输入行', doc.querySelector('#newBookRow').hidden === false);

  //  预览必须**逐个列出**没认出来的词：只报个数量的话，用户不知道该去改哪一个
  const paste = doc.querySelector('#paste');
  paste.value = 'abandon\nzzzznotaword\nthresholds';
  paste.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 500));
  const preview = doc.querySelector('#pastePreview').textContent;
  check('预览报出识别数', preview.includes('识别到 3 个词'), preview);
  check('没认出来的词逐个列出来', preview.includes('zzzznotaword'), preview);
  check('说清楚它仍然会录进去', preview.includes('仍然会按顺序录进去'), preview);
  check('还原到原形的也说一声', preview.includes('thresholds→threshold'), preview);

  //  一次粘贴超过 500 个词时，后端只收前 500 个、剩下的直接丢掉，而它唯一
  //  说这件事的办法就是 truncated 这个字段。前端不读它的话，用户看到的是
  //  「已录入 500 个词」，一个字都没提还有几百个没进去——算出来了但没在
  //  界面上编码，等于没算（需要注意.md 第 12b 条）。
  addResult = { added: 500, missing: [], restored: 0, truncated: true, entries: [] };
  doc.querySelector('#unitSel').value = '9';
  paste.value = 'abandon';
  doc.querySelector('#addEntries').dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 200));
  const toasts = [...doc.querySelectorAll('#toasts .toast')].map((t) => t.textContent).join(' | ');
  check('超出上限被丢掉的词要说出来', /没有录进去|超出/.test(toasts), toasts);
  addResult = null;
}

/* ------------------------------ 背诵页 ------------------------------ */

console.log('\n背单词 · 背诵页');
{
  const dom = makeDom('./.fixtures/drill.html', { unitId: '9' });
  stubFetch(dom.window);
  const mod = await import(pathToFileURL('./.fixtures/static/js/pages/drill.js').href);
  await mod.init({ unitId: '9' });
  await new Promise((r) => setTimeout(r, 200));
  const doc = dom.window.document;
  const w = dom.window;

  //  卡片正面只有词和音标：翻面前给出释义，整个「先自己回忆」就废了
  check('正面是第一个词', doc.querySelector('#word').textContent === 'zebra');
  check('正面有音标', doc.querySelector('#phonetic').textContent.includes('ziːbrə'));
  check('正面带页码', doc.querySelector('#cardPage').textContent === 'P42');
  check('翻面前不给释义', doc.querySelector('#back').hidden === true);
  check('翻面前不给判定按钮', doc.querySelector('#judge').hidden === true);

  doc.querySelector('#flip').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  check('翻面后给释义', doc.querySelector('#translation').textContent === 'n. 斑马');
  check('翻面后给变形', doc.querySelector('#forms').textContent.includes('zebras'));
  check('翻面后给派生词', doc.querySelector('#forms').textContent.includes('zebraic'));
  check('翻面后才出判定按钮', doc.querySelector('#judge').hidden === false);

  //  「认识」只上报认不认识，不上报该推到哪一档——档位规则归后端，
  //  以后换成遗忘曲线时前端不用改
  doc.querySelector('#yes').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 120));
  check('上报了一次复习', reviewed.length === 1, JSON.stringify(reviewed[0]));
  check('只上报认不认识和模式',
        reviewed[0] && reviewed[0].known === true && reviewed[0].mode === 'flip');
  check('翻到下一张', doc.querySelector('#word').textContent === 'yacht');

  /*  ---- 没记上的那张卡不能就这么消失 ----
      卡片是先翻过去、再发请求的（一分钟几十次，等来回会明显卡顿）。
      原来失败分支里写的是 `Object.assign(entry, before)`——而 before 是发请求
      **之前**照着 entry 拍的快照，中间没有任何东西改过它（档位是后端推的），
      所以那句话是个空操作。真正丢掉的是**队列里的位置**：答「认识」的那张
      已经被 shift 出去了，这一轮再也不会出现，于是这次判定既没落库、
      也不会重问，而 toast 两三秒后就消失，一点痕迹都不留。 */
  const remaining = () => /本轮还剩 (\d+)/.exec(doc.querySelector('#progress').textContent)?.[1];
  const before = remaining();
  failReview = true;
  doc.querySelector('#flip').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  doc.querySelector('#yes').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 150));
  failReview = false;
  check('没记上也先翻到下一张（不能为了等接口卡住）',
        doc.querySelector('#word').textContent === 'zzzznotaword',
        doc.querySelector('#word').textContent);
  check('这张卡还留在本轮里', remaining() === before, `${before} -> ${remaining()}`);
  check('说清了它会再问一遍',
        [...doc.querySelectorAll('#toasts .toast')].some((t) => /再问一遍/.test(t.textContent)),
        [...doc.querySelectorAll('#toasts .toast')].map((t) => t.textContent).join(' | '));

  //  走到本轮末尾，它必须重新出现
  for (let i = 0; i < 2; i++) {
    doc.querySelector('#flip').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    doc.querySelector('#yes').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await new Promise((r) => setTimeout(r, 120));
  }
  check('本轮末尾又问了它一遍', doc.querySelector('#word').textContent === 'yacht',
        doc.querySelector('#word').textContent);

  //  ---- 顺序：这一组是这个功能的命根 ----
  doc.querySelector('#views button[data-view="list"]')
     .dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  const words = [...doc.querySelectorAll('#entryTable tbody tr')]
    .map((tr) => tr.children[2].textContent.trim());
  check('词表顺序 == 接口给的顺序',
        JSON.stringify(words) === JSON.stringify(ENTRIES.map((e) => e.headword)),
        words.join(','));
  const pages = [...doc.querySelectorAll('#entryTable tbody tr')]
    .map((tr) => tr.children[1].textContent.trim());
  check('后录的 P42 仍然排在 P43 前面',
        JSON.stringify(pages) === JSON.stringify(['42', '42', '43', '43']), pages.join(','));
  const nums = [...doc.querySelectorAll('#entryTable tbody tr')]
    .map((tr) => tr.children[0].textContent.trim());
  check('序号是连续的 1..N', JSON.stringify(nums) === JSON.stringify(['1', '2', '3', '4']));

  //  词典里没有的词也在表里，而且标出来「要你自己补」
  check('词典没有的词照样在原位', words[2] === 'zzzznotaword');
  const row = doc.querySelectorAll('#entryTable tbody tr')[2];
  check('标出来它的释义要自己补', row.textContent.includes('自己补'), row.textContent.trim());
}

/* ------------------------------ 间隔重复 ------------------------------

   接了 FSRS 之后，队列按「今天到期没有」筛。这一组盯两件事：

   1. **只过滤，不重排**。这一页唯一的承诺是「顺序 == 纸质书」，到期筛选和它
      有张力——解法是 filter 不动顺序，过一遍到期的词仍然从书的前面往后走。
      随手改成「先背最急的」就把那条承诺破掉了，而且不报错。
   2. **退路要在**。没有「不管到期，按书再过一遍」，间隔重复等于把
      「对着书从头背一遍」这个用法拿走了——而那正是他买那本书的理由。 */

console.log('\n背单词 · 间隔重复');
{
  //  书序：zebra(P42) → yacht(P42) → zzzznotaword(P43) → abandon(P43)
  //  今天到期的只有第 2 和第 4 个。筛完必须还是这个相对顺序。
  const MIXED = ENTRIES.map((e, i) => ({ ...e, due: i % 2 === 1, due_at: '2026-09-20T00:00:00+00:00' }));
  const dom = makeDom('./.fixtures/drill.html', { unitId: '9' });
  const w = dom.window;
  w.fetch = async (url) => {
    const p = String(url);
    const json = (v) => ({ ok: true, status: 200, json: async () => v });
    if (p.startsWith('/api/wordbook/units/9/entries')) return json({ entries: MIXED });
    if (p === '/api/words') return json({ stats: {}, words: [] });
    return json({});
  };
  globalThis.fetch = w.fetch;
  const mod = await import(pathToFileURL('./.fixtures/static/js/pages/drill.js').href
                           + `?t=${Math.random()}`);
  await mod.init({ unitId: '9' });
  await new Promise((r) => setTimeout(r, 200));
  const doc = w.document;

  check('没到期的词不进队列', doc.querySelector('#word').textContent === 'yacht',
        doc.querySelector('#word').textContent);
  check('进度说的是「今天还剩」而不是「本轮还剩」',
        /今天还剩 2/.test(doc.querySelector('#progress').textContent),
        doc.querySelector('#progress').textContent);

  //  过一遍：yacht -> abandon，也就是书序，不是「最急的先来」
  const seen = [doc.querySelector('#word').textContent];
  for (let i = 0; i < 2; i++) {
    doc.querySelector('#flip').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    doc.querySelector('#yes').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await new Promise((r) => setTimeout(r, 120));
    if (!doc.querySelector('#card').hidden) seen.push(doc.querySelector('#word').textContent);
  }
  check('到期的那些仍然按书上的顺序走', seen.join() === 'yacht,abandon', seen.join());

  //  过完之后：不能说成「这一单元背完了」——还有两个词只是没到期
  check('过完之后出现完成态', !doc.querySelector('#done').hidden);
  check('说清楚只是今天的过完了，不是整本背完',
        /今天该复习的过完了/.test(doc.querySelector('#doneTitle').textContent),
        doc.querySelector('#doneTitle').textContent);
  check('说出还有几个没到期', /还有 2 个词没到复习时间/.test(doc.querySelector('#doneNote').textContent),
        doc.querySelector('#doneNote').textContent);

  //  退路：按书再过一遍，这时四个词全回来，而且还是书序
  const again = doc.querySelector('#again');
  check('退路按钮明说自己干什么', /不管到期/.test(again.textContent), again.textContent);
  again.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 60));
  check('退路把没到期的也放回来', doc.querySelector('#word').textContent === 'zebra',
        doc.querySelector('#word').textContent);
  check('这一轮的进度说的是「本轮还剩」',
        /本轮还剩 4/.test(doc.querySelector('#progress').textContent),
        doc.querySelector('#progress').textContent);

  //  词表视图**完全不受到期影响**：那边永远是整本书的完整顺序
  doc.querySelector('#views button[data-view="list"]')
     .dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  const listed = [...doc.querySelectorAll('#entryTable tbody tr')]
    .map((tr) => tr.children[2].textContent.trim());
  check('词表不受到期筛选影响，仍是整本书的顺序',
        listed.join() === ENTRIES.map((e) => e.headword).join(), listed.join());
}

console.log(`\n${ok}/${ok + fail} 通过`);
process.exit(fail ? 1 : 0);
