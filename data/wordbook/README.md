# 背单词页的参考词典

`dict.csv` 是**生成物**，由 `scripts/build_wordbook_dict.py` 从 ECDICT 筛出来的。
要改收词范围或派生词算法，去改那个脚本再重新生成；直接手改这个文件，
下一次重新生成就把改动冲掉了（和 `core/lexicon/irregular_forms.py` 同一个规矩）。

## 数据来源与许可

[ECDICT](https://github.com/skywind3000/ECDICT) —— MIT License，Copyright (c) 2025 Linwei。

MIT 允许再分发，条件是保留版权与许可声明，所以这份声明必须跟着 `dict.csv` 一起留在仓库里。

原始表 77 万词 / 66MB，这里只保留用得上的 33,217 词（3.7MB）：

| 收进来的判据 | 为什么 |
|---|---|
| 八个考纲标签之一（`zk` `gk` `cet4` `cet6` `ky` `toefl` `ielts` `gre`） | 六级和考研的书里本来就混着四级、高考词 |
| 有柯林斯星级 | 高频常用词，书里的派生词多半落在这里 |
| 收进牛津 3000 | 同上 |
| 词频（`frq`）前 3 万 | 兜底，让「贴一页进去基本不用管」成立 |

字段：`word, phonetic, translation, exchange, derivatives, collins, frq, tag`。

`derivatives` **不是 ECDICT 给的**——它的 `exchange` 只有屈折形式
（abandon → abandoned / abandoning / abandons），没有 abandonment 这类派生词。
派生词是构建时拿整本 77 万词当池子扫出来的，判据是「加上后缀之后那个词真的存在」。
六级词里 92% 能扫出至少一个；只拿输出的这 3.3 万当池子会掉到 53%，所以池子必须是全表。

## 这里**没有**什么

任何一本纸质词汇书的内容：List 怎么划、每个 List 收哪些词、词根拆解、联想段子、
例句——那些是各本书的编辑创作，不在这份表里，也不该被扒进来。

词书的**顺序**由使用者自己从手上的书录入（背单词页的「粘一页」），
录进去的顺序原样存在 `data/app.db` 里，属于用户自己的数据。
