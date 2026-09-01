# 前端测试

在 [jsdom](https://github.com/jsdom/jsdom) 里跑**服务端真实渲染的 HTML**
和 `web/static/js` 里**真实的前端模块**——不是手写的假 DOM，也不是复制品。
模板或接口字段改了，夹具会跟着变，不会出现「测试还在测三个月前的页面」。

```bash
cd tests/frontend
npm install
python make_fixtures.py     # 需要先装好 Python 依赖
npm test
```

## 它能测什么

DOM 结构、事件流、模块之间的接线。举两个靠读代码看不出来的例子：

- 回忆模式下焦点落在挖空词上按空格，应该**只揭示该词**而不跳出回忆模式
  （少一个 `stopPropagation` 就会两件事一起发生）
- 生成的 SSE 分片被故意切在事件中间，验证缓冲拼接确实把它们拼回来了

## 它测不了视觉

jsdom **没有布局引擎**：`offsetHeight` 恒为 0，`getBoundingClientRect()` 全是零，
**CSS 完全不参与**。所以配色、间距、深色模式、响应式一概验不到；
依赖真实布局的 `lockHeights()`（防止中英切换时页面跳动）在这里等于空跑。

要验视觉得用真浏览器截图——比如 puppeteer-core 指向本机已装的 Chrome，
再配合 `getComputedStyle` 和实测宽高。这一步没做进 CI：它需要一个真实浏览器，
而截图比对的维护成本在这个项目的规模上不划算。改动 CSS 之后，
自己开一遍浏览器看看是更实际的做法。
