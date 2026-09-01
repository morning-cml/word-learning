/* ===========================================================================
   全站共用的最小工具集。只放「每个页面都可能用到」的东西——
   页面专属逻辑一律写进 pages/<页面 id>.js，别往这里塞。

   刻意不引任何框架：这个应用一共五个页面，交互也简单，
   引一个框架带来的构建步骤和心智负担远大于它省下的代码。
   =========================================================================== */

'use strict';

/* ------------------------------ DOM ------------------------------ */

export const $  = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

/** 事件委托。列表内容是动态渲染的，逐个绑定会在重渲染后失效。 */
export function on(root, type, selector, handler) {
  root.addEventListener(type, (e) => {
    const hit = e.target.closest(selector);
    if (hit && root.contains(hit)) handler(e, hit);
  });
}

export function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

export function escapeRe(s) {
  return String(s ?? '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** 把 HTML 字符串塞进容器。集中在一处，方便日后需要时统一加处理。 */
export function html(el, markup) {
  if (el) el.innerHTML = markup;
  return el;
}

/* ------------------------------ 主题 ------------------------------ */

const THEME_KEY = 'wl-theme';

/** 当前生效的主题。没存过偏好时跟随系统——桌面版 app.py 也是跟随系统的。 */
export function currentTheme() {
  const explicit = document.documentElement.getAttribute('data-theme');
  if (explicit) return explicit;
  return matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

export function toggleTheme() {
  const next = currentTheme() === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* 隐私模式下写不了，忽略 */ }
  return next;
}

export function restoreTheme() {
  try {
    const saved = localStorage.getItem(THEME_KEY);
    if (saved) document.documentElement.setAttribute('data-theme', saved);
  } catch (e) { /* 读不到就跟随系统，属于正常降级 */ }
}

/* ------------------------------ toast ------------------------------ */

/**
 * 右下角浮条。用于「做完了」「失败了」这类一次性反馈。
 * 需要用户持续看到的信息（比如某个词没被覆盖）应该渲染进页面，别用 toast。
 */
export function toast(message, kind = '') {
  let box = $('#toasts');
  if (!box) {
    box = document.createElement('div');
    box.id = 'toasts';
    document.body.appendChild(box);
  }
  const el = document.createElement('div');
  el.className = 'toast' + (kind ? ' ' + kind : '');
  el.textContent = message;
  box.appendChild(el);
  setTimeout(() => {
    el.classList.add('leaving');
    setTimeout(() => el.remove(), 300);
  }, kind === 'bad' ? 5200 : 2800);   // 出错的多留一会儿，够看清
  return el;
}

/* ------------------------------ 杂项 ------------------------------ */

/** 输入框防抖。预览接口每敲一个字母都请求一次是浪费。 */
export function debounce(fn, ms = 250) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

export function fmtTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
