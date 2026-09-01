/* ===========================================================================
   引导脚本。base.html 只加载这一个文件，它负责：

     1. 恢复主题、接上切换按钮
     2. 填顶栏的模型状态
     3. 按 <body data-page="xxx"> 动态载入 pages/xxx.js 并调用它的 init()

   第 3 条是这套前端的扩展点：**新增一个页面不需要改这里，也不需要改 base.html**。
   在 web/pages.py 里加一条记录（导航自动出现）、写一个同名模板、
   往 pages/ 丢一个导出 init() 的模块，就完事了。
   页面没有 JS 时连文件都不用建——动态 import 失败会被安静地忽略。
   =========================================================================== */

'use strict';

import { $, restoreTheme, toggleTheme, currentTheme, toast } from './core.js';
import { tryGet } from './api.js';

restoreTheme();

function initThemeToggle() {
  const btn = $('#themeBtn');
  if (!btn) return;
  const sync = () => {
    const dark = currentTheme() === 'dark';
    btn.textContent = dark ? '☀' : '☾';
    btn.title = dark ? '切换到浅色' : '切换到深色';
  };
  sync();
  btn.addEventListener('click', () => { toggleTheme(); sync(); });
  // 没手动切过时跟随系统，系统主题变了要跟着变
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', sync);
}

async function initModelChip() {
  const chip = $('#modelChip');
  if (!chip) return;
  const s = await tryGet('/api/status');
  if (!s) { chip.textContent = '状态未知'; chip.classList.add('warn'); return; }
  chip.textContent = s.has_key ? `${s.provider} / ${s.model}` : '未配置 API Key';
  chip.classList.toggle('warn', !s.has_key);
  chip.title = s.has_key
    ? `词表 ${s.cefr.real_data ? 'CEFR-J' : '内置兜底'} ${s.cefr.size} 词 · 点击去设置`
    : '还没填 API Key，点这里去设置';
  chip.addEventListener('click', () => { location.href = '/settings'; });
}

async function initPage() {
  const id = document.body.dataset.page;
  if (!id) return;
  let mod;
  try {
    mod = await import(`./pages/${id}.js`);
  } catch (err) {
    // 页面没有配套 JS 是正常情况（纯静态页），只有真的报错才值得说一声
    if (!/Failed to fetch dynamically imported module|404/i.test(String(err))) {
      console.error(`[main] 页面模块 ${id}.js 加载失败`, err);
      toast(`页面脚本 ${id}.js 出错，功能可能不完整`, 'bad');
    }
    return;
  }
  try {
    await mod.init?.(document.body.dataset);   // data-* 全部透传，页面自己取需要的
  } catch (err) {
    console.error(`[main] ${id}.init() 抛异常`, err);
    toast(`页面初始化失败：${err.message}`, 'bad');
  }
}

initThemeToggle();
initModelChip();
initPage();
