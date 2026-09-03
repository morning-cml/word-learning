/* ===========================================================================
   后端调用的唯一入口。

   新增一个后端接口时，前端这边只要多一行 —— 比如
       export const words = { list: () => get('/api/words') };
   错误处理、JSON 解析、报错文案都由下面统一负责，不用每处再写一遍
   `if (!res.ok)`。这也是为什么值得单独开这个文件：请求散落在各页面里时，
   总有几处会忘记判 ok，表现出来就是「点了没反应」。
   =========================================================================== */

'use strict';

import { toast } from './core.js';

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function request(method, path, body) {
  let res;
  try {
    res = await fetch(path, {
      method,
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (err) {
    // 网络层就挂了，多半是后台没在跑。这句话比 "Failed to fetch" 有用得多。
    throw new ApiError('连不上本地服务，确认 main.py 还在运行', 0);
  }

  if (!res.ok) {
    // FastAPI 的错误体是 {"detail": ...}，尽量把它取出来给用户看
    let detail = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      if (typeof data?.detail === 'string') detail = data.detail;
    } catch (e) { /* 不是 JSON 就用状态码兜底 */ }
    throw new ApiError(detail, res.status);
  }

  if (res.status === 204) return null;
  return res.json();
}

export const get  = (path)       => request('GET', path);
export const post = (path, body) => request('POST', path, body ?? {});
export const del  = (path)       => request('DELETE', path);

/** 包一层：失败时弹 toast 并返回 fallback，用于「失败了也不该中断页面」的场景。 */
export async function tryGet(path, fallback = null) {
  try {
    return await get(path);
  } catch (err) {
    toast(err.message, 'bad');
    return fallback;
  }
}

/* --------------------------- 具体接口 ---------------------------
   按后端路由分组。加接口就在对应的组里加一行。 */

export const status = () => get('/api/status');
export const levels = () => get('/api/levels');
export const timing = () => get('/api/timing');

export const article = {
  preview:  (words)        => post('/api/article/plan-preview', { words }),
  list:     ()             => get('/api/articles'),
  read:     (id)           => get(`/api/articles/${id}`),
  impact:   (id)           => get(`/api/articles/${id}/impact`),
  remove:   (id)           => del(`/api/articles/${id}`),
  generate: (payload, onEvent, opts) => stream('/api/article/generate', payload, onEvent, opts),
};

export const words = {
  list:      ()             => get('/api/words'),
  // 「这个词不用管」——收进词库并标成忽略（99）。难度标尺读整个词库，
  // 所以以后就不会再把它判成超纲。99 这个数字只在这一处出现。
  ignore:    (lemma)        => post('/api/words', { lemma, status: 99 }),
  detail:    (lemma)        => get(`/api/words/${encodeURIComponent(lemma)}`),
  setStatus: (lemma, value) => post(`/api/words/${encodeURIComponent(lemma)}/status`, { status: value }),
};

export const settings = {
  read:   ()       => get('/api/settings'),
  save:   (patch)  => post('/api/settings', patch),
  models: (pid)    => get(`/api/settings/models/${encodeURIComponent(pid)}`),
  check:  (body)   => post('/api/settings/check', body),
};

/* --------------------------- SSE ---------------------------
   生成走 POST + text/event-stream，EventSource 只支持 GET，所以自己读 body。
   拆包放在这里而不是页面里：将来再有别的流式任务（出题、造例句）能直接复用。 */

export async function stream(path, payload, onEvent, { signal } = {}) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal,                       // abort 之后 reader.read() 会抛 AbortError
  });
  if (!res.ok || !res.body) throw new ApiError(`HTTP ${res.status}`, res.status);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split('\n\n');
    buffer = chunks.pop();                       // 最后一段可能被截断，留到下一轮
    for (const chunk of chunks) {
      const line = chunk.split('\n').find((l) => l.startsWith('data: '));
      if (!line) continue;
      try {
        onEvent(JSON.parse(line.slice(6)));
      } catch (e) {
        // 单个事件坏掉不该中断整条流：后面还有 done / saved 要收
        console.warn('[api] 跳过一个无法解析的事件', e);
      }
    }
  }
}
