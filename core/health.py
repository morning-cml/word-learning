"""模型接入的四层正确性检验。

为什么不是一个「测试连接」按钮：很多模型 L1 过、L3 挂——key 有效、
网络通、模型名也对，但它就是不肯稳定吐出合法 JSON，或者中文翻译整片漏掉。
只测连通性的话，你要等生成完一整篇废文才发现。

  L1 连通性   打 /models 或一次最小 chat —— 验 key、网络、base_url
  L2 JSON     要一个 {"ok": true} —— 验它真的支持 json_object，
              以及会不会撞上 DeepSeek 官方承认的空响应问题
  L3 任务验收 给 3 个词生成 2 句话 —— 验 schema 完整、中英一一对齐、
              目标词命中、中文非空
  L4 审计校准 给一段一半有线索一半没线索的文本 —— 验它分不分得清

L4 是后加的，理由值得写下来：这个产品的全部价值判定压在语境线索审计
那一次调用上，而 L1-L3 恰好把唯一要紧的能力漏掉了——它们验的是「产出的
形状对不对」，不是「它判得准不准」。审计判错时没有任何人会发现：
能看出「这个语境够不够推出词义」的读者，本来就已经认识这个词了。
没有反馈回路的错误不会自我纠正，所以只能在入口处一次性验掉。
"""
from __future__ import annotations

import time
from typing import Any

from core.llm import jsonfix
from core.llm.client import LLM
from core.provider import registry
from core.provider.base import ProviderError

PROBE_ROUNDS = 3

L3_PROMPT = """\
用 json 回答。给你 3 个英文单词：river、promise、return。

写 2 句连贯的英文，把这 3 个词自然用进去，并给每句配一句中文翻译。
格式与下例完全一致，不要任何解释文字：
{
  "sentences": [
    {"en": "...", "zh": "...", "targets": [{"lemma": "river", "surface": "river"}]},
    {"en": "...", "zh": "...", "targets": [{"lemma": "promise", "surface": "promised"}]}
  ]
}
"""


# L4 定标文本：同一段话里一个词有强线索、一个词完全没有。
# 放在一次调用里而不是两次，是因为这样测的是「分辨力」而不是「倾向」——
# 见词就说 strong 的模型和见词就说 none 的模型会同时挂掉。
CALIBRATION_TEXT = (
    "Ben opened the folder and started copying the numbers across. It was tedious. "
    "He kept at it until six. Then he lined up every pencil on the desk so the tips "
    "pointed the same way, and squared each stack of paper against the edge — he was "
    "meticulous, the kind of man who could not walk past one crooked thing."
)
CALIBRATION_WORDS = ["tedious", "meticulous"]


def _step(name: str, label: str) -> dict[str, Any]:
    return {"id": name, "label": label, "ok": False, "ms": 0, "detail": "", "error": ""}


def check(provider_id: str, model: str, api_key: str) -> dict[str, Any]:
    """跑完三层检验，任何一层失败都不影响后面几层的结果结构。"""
    # 按模型解析：同一家的模型能力不一样，而检验的全部意义就是
    # 「**这个**模型能不能干这个活」，拿厂商的声明去验等于验了个别的东西
    spec = registry.get_spec(provider_id).for_model(model)
    provider = registry.build(provider_id, api_key, timeout=90.0)
    llm = LLM(provider=provider, model=model, max_retries=1)

    l1 = _step("connect", "连通性")
    l2 = _step("json", "JSON 输出")
    l3 = _step("task", "任务验收")
    l4 = _step("clue", "线索审计校准")
    result = {
        "provider": provider_id,
        "model": model,
        "steps": [l1, l2, l3, l4],
        "ok": False,
        "tokens": 0,
    }

    if not api_key:
        l1["error"] = "还没填 API Key"
        return result

    # ---------------------------------------------------------------- L1
    t0 = time.perf_counter()
    try:
        if spec.capabilities.models_endpoint:
            ids = provider.list_models()
            l1["ok"] = True
            known = model in ids
            l1["detail"] = (
                f"可用模型 {len(ids)} 个；当前模型 {model} "
                + ("在列表内" if known else "不在列表内（可能已下线或需申请）")
            )
            if ids and not known:
                l1["detail"] += "。近似可用：" + "、".join(ids[:5])
        else:
            provider.chat([{"role": "user", "content": "hi"}], model=model, max_tokens=8)
            l1["ok"] = True
            l1["detail"] = "该端点不支持 /models，已用最小对话验证"
    except ProviderError as exc:
        l1["error"] = str(exc)
        if exc.body:
            l1["detail"] = exc.body[:200]
    l1["ms"] = int((time.perf_counter() - t0) * 1000)

    if not l1["ok"]:
        return result

    # ---------------------------------------------------------------- L2
    # 探针走 purpose="probe"：只要一个 {"ok":true}，让推理模型为此思考几千 token
    # 纯属浪费钱和时间。跑多轮是因为单次成功证明不了稳定性——空响应恰恰是偶发的。
    t0 = time.perf_counter()
    good, notes = 0, []
    for _ in range(PROBE_ROUNDS):
        try:
            res = llm.chat(
                [{"role": "user", "content": '请回答。只输出 json，形如 {"ok": true}。'}],
                purpose="probe", json_mode=True, max_tokens=64,
            )
            if not res.text.strip():
                why = res.diagnose()
                notes.append("空响应" + (f"（{why}）" if why else ""))
                continue
            jsonfix.loads(res.text)
            good += 1
        except (ProviderError, jsonfix.JsonParseError) as exc:
            notes.append(str(exc)[:80])

    l2["ok"] = good > 0
    l2["detail"] = f"{PROBE_ROUNDS} 次探测成功 {good} 次。"
    if good == PROBE_ROUNDS:
        l2["detail"] += "JSON 输出稳定。"
        if spec.capabilities.json_schema:
            l2["detail"] += "该模型还支持 Structured Output，格式更稳。"
    elif good:
        l2["detail"] += (
            "偶发失败（" + "；".join(dict.fromkeys(notes)) + "）。"
            "生成管线内置重试与 JSON 修复，能扛住，但会多花时间和 token。"
        )
    else:
        l2["error"] = "；".join(dict.fromkeys(notes)) or "全部失败"
    l2["ms"] = int((time.perf_counter() - t0) * 1000)

    # ---------------------------------------------------------------- L3
    t0 = time.perf_counter()
    try:
        doc = llm.json(
            [{"role": "user", "content": L3_PROMPT}],
            purpose="creative", max_tokens=1200,
        )
        issues = _audit(doc)
        l3["ok"] = not issues
        sents = _sentences(doc)
        if issues:
            l3["error"] = "；".join(issues)
        if sents:
            first = sents[0]
            l3["detail"] = (
                f"产出 {len(sents)} 句。样例 → "
                f"EN: {_field(first, 'en')[:70]} / "
                f"ZH: {_field(first, 'zh')[:40]}"
            )
    except (ProviderError, jsonfix.JsonParseError) as exc:
        l3["error"] = str(exc)
    l3["ms"] = int((time.perf_counter() - t0) * 1000)
    if spec.reasoning.counts_toward_max_tokens:
        l3["detail"] = (l3["detail"] + "  " if l3["detail"] else "") + (
            f"（推理模型：max_tokens 含思考 token，程序已自动追加 "
            f"{spec.reasoning.headroom} tokens 思考预算）"
        )

    # ---------------------------------------------------------------- L4
    t0 = time.perf_counter()
    try:
        verdicts = _calibrate(llm)
        weak = verdicts.get("tedious")
        strong = verdicts.get("meticulous")
        problems = []
        missing = [w for w in CALIBRATION_WORDS if not verdicts.get(w)]
        if missing:
            # 漏审的词在真实管线里会被按 none 兜底（不能默认放行），
            # 于是每一段都要为它多烧一轮补线索。不是「安全」，是持续多花钱。
            problems.append("漏审了 " + "、".join(missing) + "——审计会漏词，"
                            "漏掉的词管线只能按最坏情况处理，每段多烧一次补线索调用")
        if strong and strong != "strong":
            problems.append(
                f"带强线索的 meticulous 被判成 {strong}——"
                "会导致明明写好的段落被反复要求「补线索」，白烧调用")
        if weak == "strong":
            problems.append(
                "无线索的 tedious 被判成 strong——审计等于橡皮图章，"
                "读者会拿到一篇词都在、但一个也猜不出来的文章")
        l4["ok"] = not problems
        l4["error"] = "；".join(problems)
        l4["detail"] = (
            f"同一段文本里：meticulous（破折号释义 + 具体画面）判 {strong or '漏审'}，"
            f"tedious（换成任何生词句子照样通顺）判 {weak or '漏审'}。"
        )
    except (ProviderError, jsonfix.JsonParseError) as exc:
        l4["error"] = str(exc)
    l4["ms"] = int((time.perf_counter() - t0) * 1000)

    result["ok"] = all(s["ok"] for s in result["steps"])
    result["tokens"] = llm.usage.total_tokens
    return result


def _calibrate(llm: LLM) -> dict[str, str]:
    """跑一次真实的审计调用，返回 {lemma: strength}。

    刻意 import 任务层的 prompt 和归一函数，而不是在这里另抄一份：
    抄一份就变成「校验通过但实际审计仍然失灵」——测的必须是真正会跑的那段。
    """
    from tasks.article.prompts import audit_prompt
    from tasks.article.schema import AUDIT_SCHEMA, coerce_audits

    audits = coerce_audits(llm.json(
        audit_prompt(CALIBRATION_TEXT, CALIBRATION_WORDS),
        purpose="structured", max_tokens=2500, json_schema=AUDIT_SCHEMA,
    ))
    return {a["lemma"].lower(): a["strength"] for a in audits}


def _sentences(doc: Any) -> list[dict]:
    """安全地取出句子列表，只用来给检验结果配一句样例。

    这里绝不能直接 doc.get()。L3 存在的全部理由就是「模型可能吐出形状不对的
    东西」，而顶层给成数组、sentences 里躺着字符串都是真实发生过的形状
    （见 tasks/article/schema.py 顶上那段）。在这一行抛 AttributeError 的后果
    比看上去大得多：它不在下面 except 的捕获范围里，会一路冒到接口层变成
    HTTP 500——用户看到的是「检验请求失败」，而不是「任务验收没过、
    原因是顶层不是对象」，而且最要紧的 L4 一次都跑不到。
    检验本身在它该报告问题的时候崩掉，等于这一层不存在。
    """
    if isinstance(doc, list):
        items: Any = doc
    elif isinstance(doc, dict):
        items = doc.get("sentences")
    else:
        items = None
    return [s for s in items if isinstance(s, dict)] if isinstance(items, list) else []


def _field(sent: Any, key: str) -> str:
    """安全地取一句里的 en / zh。非字符串一律当空。

    上一轮守住的是**容器**的形状（见 _sentences），值没有守：模型把 en 写成
    对象、把 zh 写成数字或数组都出现过——`tasks/article/schema.py` 的 `_text`
    就是专为这件事存在的，而这里另起了一套取值方式，于是那道防线没跟过来。

    `(s.get("en") or "").strip()` 撞上这种值抛的是 AttributeError，
    它不在 check() 里那个 except 的捕获范围内，会一路冒到接口层变成 HTTP 500：
    用户看到的是「检验请求失败」，而不是「任务验收没过、原因是 en 不是字符串」，
    并且**最要紧的 L4 一次都跑不到**。检验本身在它该报告问题的时候崩掉，
    等于这一层不存在——和 _sentences 上面那段说的是同一件事。
    """
    value = sent.get(key) if isinstance(sent, dict) else None
    return value.strip() if isinstance(value, str) else ""


def _audit(doc: Any) -> list[str]:
    """L3 的验收标准——这几条不过，这个模型就干不了我们的活。"""
    issues: list[str] = []
    if not isinstance(doc, dict):
        return ["顶层不是 json 对象"]
    sents = doc.get("sentences")
    if not isinstance(sents, list) or not sents:
        return ["缺少 sentences 数组"]

    missing_zh = sum(1 for s in sents if not _field(s, "zh"))
    missing_en = sum(1 for s in sents if not _field(s, "en"))
    if missing_en:
        issues.append(f"{missing_en} 句英文为空")
    if missing_zh:
        issues.append(f"{missing_zh} 句缺中文——句级对齐会直接崩掉")

    text = " ".join(_field(s, "en").lower() for s in sents)
    missed = [w for w in ("river", "promise", "return") if w[:5] not in text]
    if missed:
        issues.append("目标词未命中：" + "、".join(missed))

    has_targets = any(
        isinstance(s, dict) and isinstance(s.get("targets"), list) and s["targets"]
        for s in sents
    )
    if not has_targets:
        issues.append("没有标注 targets——生词高亮会失效")
    return issues
