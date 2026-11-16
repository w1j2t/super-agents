"""结构化上下文压缩：压缩叙述，但不丢细节。

为什么不能"截断"
----------------
上下文压缩最常见的做法是按长度切片，代价是把"决定性细节"和"过程描述"一起扔掉。
实测里最容易丢的三类是：**路径、数字与阈值、标识符（UUID / seq / rc / commit）**——
而恰恰是这三类决定了后续能不能复现一个 bug。

本模块的口径是**只压叙述、不压细节**，靠三条硬规则：

R1 实体提级（promotion）：任何**含实体**的句子，即使叙述被预算裁掉，该句也整体升格进
   `facts`（或 `decisions` / `open`），并带上原文 `ref`。裁的是形容词，不是事实。
R2 实体表永不参与预算竞争：实体单独成表，预算再小也保形（`overflow_policy="refs_only"`
   时只留"实体 → ref"，叙述全删，仍可回溯到原文那一句）。
R3 可验证召回：`verify_recall()` 从原文抽实体集合，逐条检查压缩产物里还在不在，
   返回缺失清单。空清单才算"没丢细节"，这是本模块唯一的质量判据。

用法
----
```python
from _compact import compact, verify_recall
out = compact(messages, budget=1500)      # messages = [{"role","content","ref"?}]
missed = verify_recall(messages, out)     # 必须为空
```

命令行：
    python workers/_compact.py --selftest      # 内置用例 + 召回自检，退出码 0/1
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# 实体抽取：宁可多提，不可漏提。每一项都是 (类别, 正则)。
# 顺序有意义：先长后短，避免 UUID 被当作普通数字、URL 被当作路径。
# ---------------------------------------------------------------------------
ENTITY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("url", re.compile(r"https?://[^\s<>\"'）)】\]]+")),
    ("uuid", re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")),
    ("commit", re.compile(r"\b[0-9a-f]{7,40}\b")),
    ("winpath", re.compile(r"[A-Za-z]:\\(?:[^\s\\/:*?\"<>|，。；、！？（）【】《》“”‘’]+\\)*[^\s\\/:*?\"<>|，。；、！？（）【】《》“”‘’]*")),
    ("posixpath", re.compile(r"(?<![\w.])/(?:[\w.\-]+/)*[\w.\-]+")),
    ("file", re.compile(r"[\w\-.]+\.(?:rs|py|ts|js|json|toml|yaml|yml|md|txt|log|csv|xlsx|docx)\b")),
    ("verdict", re.compile(r"\b(?:rc|code|exit|codes)\s*[=:]\s*\d+")),
    ("metric", re.compile(r"\b[a-z_]{3,}(?:_total|_count|_ms|_ratio|_bytes)\b")),
    ("percent", re.compile(r"\d+(?:\.\d+)?\s*%")),
    ("timestamp", re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?")),
    ("number", re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?:\s*(?:GB|MB|KB|s|ms|行|个|条|项|道))?")),
]

# 句子意图分类：这些动词短语一旦出现，句子必须保住（它承载"结论"，不是"过程"）。
DECISION_HINTS = ("决定", "采用", "选择", "改为", "定为", "约定", "口径", "统一为",
                  "不采用", "放弃", "取舍", "结论是", "定为", "已切换", "换成")
OPEN_HINTS = ("待", "未做", "尚未", "缺", "阻塞", "风险", "下一步", "todo", "需要确认",
              "尚未留证", "没跑", "待补", "?", "？")
FACT_HINTS = ("实测", "通过", "失败", "报错", "共", "达到", "实测值", "验证", "确认",
              "已", "恒为", "等于", "大于", "小于")

SENT_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")


def _sentence_split(text: str) -> list[str]:
    """按句切分，保留标点。空句丢弃。"""
    out: list[str] = []
    for chunk in SENT_SPLIT.split(text or ""):
        s = chunk.strip()
        if s:
            out.append(s)
    return out


# 一位数到底算不算"细节"，看上下文而不是长度：
#   落在"次数/超时/退出码/预算/门禁…"这类参数字眼旁，或紧跟单位，就是关键参数；
#   孤零零一个数字（页脚、序号碎片）仍是噪声。
_NUM_UNIT = re.compile(r"(?:GB|MB|KB|TB|ms|s|%|％|行|个|条|项|道|次|轮|位|档|倍|元|秒|分|小时|天|岁|人)")
_NUM_HINTS = ("次数", "超时", "退出码", "重试", "预算", "比例", "上限", "下限", "阈值",
              "共", "第", "约", "超过", "小于", "大于", "等于", "版本", "门禁", "序",
              "定为", "设定", "重试", "尝试", "剩", "耗时", "内存", "大小", "长度", "档")


def _num_significant(text: str, a: int, b: int) -> bool:
    """单字符数字是否值得当实体保留。"""
    tail = text[b:b + 3]
    if _NUM_UNIT.match(tail.lstrip()[:2] or "") or _NUM_UNIT.match(tail[:1] or ""):
        return True
    win = text[max(0, a - 6):min(len(text), b + 3)]
    return any(h in win for h in _NUM_HINTS)

# "第 N 轮/次/步"里的 N 是流水序号，不是新事实。
_SEQ_RE = re.compile(r"第\s*(\d+)\s*(?:轮|次|步|条|项|道|版|层|章|节|天|批|遍)")


def extract_entities(text: str) -> dict[str, list[str]]:
    """抽取实体，按类别归并并去重（保序）。"""
    text = text or ""
    found: dict[str, list[str]] = {}
    seen: set[str] = set()
    spans: list[tuple[int, int]] = []
    for kind, pat in ENTITY_PATTERNS:
        for m in pat.finditer(text):
            val = m.group(0).strip()
            if not val or val in seen:
                continue
            a, b = m.start(), m.end()
            # 区间去重：UUID / 路径 / URL 内部的数字碎片不算独立实体。
            #   旧版只做"字符串去重"，于是 UUID 的 0305 那一段会自己变成一个 number 实体，
            #   把实体表撑大、把比例算歪；反过来真正要紧的数字又会被下面那条长度规则误杀。
            if kind in ("number", "commit") and any(s <= a and b <= e for s, e in spans):
                continue
            # 短数字不再一律丢：`重试次数定为 6 次`、`退出码 0` 这种一位数恰恰是关键参数，
            #   旧规则 len<2 就 continue，等于把要守护的细节提前丢在门口——
            #   而校验用的是同一套抽取器，于是"零丢失"变成了自证。改成看上下文显著性。
            if kind == "number" and len(val) < 2 and not _num_significant(text, a, b):
                continue
            seen.add(val)
            spans.append((a, b))
            found.setdefault(kind, []).append(val)
    return found


def _classify(sentence: str, ents: dict[str, list[str]]) -> str:
    """给句子定性：decision / open / fact / narrative。

    含实体者一律升格为 fact（R1），因为含实体的句子通常就是"可复现依据"。
    """
    low = sentence.lower()
    if any(h in sentence for h in DECISION_HINTS):
        return "decision"
    if any(h in low or h in sentence for h in OPEN_HINTS):
        return "open"
    if ents or any(h in sentence for h in FACT_HINTS):
        return "fact"
    return "narrative"


def _density(ents: dict[str, list[str]]) -> int:
    return sum(len(v) for v in ents.values())


def _merge_entities(dst: dict[str, list[str]], src: dict[str, list[str]]) -> dict[str, list[str]]:
    """把同一条消息内多句抽到的实体合并。

    易错点（曾经的 bug）：用 `{**dst, **src}` 合并会让后一句**覆盖**前一句的同类列表，
    于是"锚点=40"这类只出现一次的数字在实体表里静默消失——压缩本身没丢，索引先丢了。
    合并必须逐类追加去重。
    """
    for kind, vals in src.items():
        bucket = dst.setdefault(kind, [])
        for v in vals:
            if v not in bucket:
                bucket.append(v)
    return dst


def _flatten_entities(ents_by_item: dict[str, dict[str, list[str]]]) -> dict[str, list[str]]:
    """把逐条实体合并成"实体 → 出处 ref 列表"。出处是 R2 的保形依据。"""
    table: dict[str, list[str]] = {}
    for ref, ents in ents_by_item.items():
        for kind, vals in ents.items():
            for v in vals:
                refs = table.setdefault(v, [])
                if ref not in refs:
                    refs.append(ref)
    return table


def compact(
    messages: Iterable[dict[str, Any]],
    budget: int = 1500,
    overflow_policy: str = "refs_only",
) -> dict[str, Any]:
    """把一段对话/上下文压成结构化摘要。

    参数
    ----
    messages : 每项 {"role": str, "content": str, "ref": str(可选)}
               ref 缺省时用 "msg[i]" 生成，压缩后所有条目都靠它指回原文。
    budget   : 叙述部分的字符预算（近似 token；中文 1 字≈1 token）。
    overflow_policy : 叙述超预算时的做法
        - "refs_only"（默认）：丢掉叙述，只留 实体→ref 表，R2 保形
        - "drop_lowest"     ：按实体密度从低到高丢叙述

    返回
    ----
    {"facts", "decisions", "open", "entities", "narrative", "refs", "stats"}
    """
    msgs = [dict(m) for m in messages]
    for i, m in enumerate(msgs):
        m.setdefault("ref", "msg[%d]" % i)
        m.setdefault("role", "user")
        m.setdefault("content", "")

    facts: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    open_items: list[dict[str, Any]] = []
    narrative: list[dict[str, Any]] = []
    ents_by_item: dict[str, dict[str, list[str]]] = {}

    for m in msgs:
        for s in _sentence_split(str(m["content"])):
            ents = extract_entities(s)
            label = _classify(s, ents)
            item = {"text": s, "ref": m["ref"], "entities": _density(ents)}
            if ents:
                ents_by_item[m["ref"]] = _merge_entities(ents_by_item.get(m["ref"], {}), ents)
            if label == "decision":
                decisions.append(item)
            elif label == "open":
                open_items.append(item)
            elif label == "fact":
                facts.append(item)
            else:
                narrative.append(item)

    # 预算分配：结构项（事实/决策/未决）优先，叙述最后吃剩余。
    # 结构项之间按"实体密度"降序，保证可复现依据先落袋。
    spent = 0
    kept = {"facts": [], "decisions": [], "open": [], "narrative": []}
    dropped: list[dict[str, Any]] = []

    order = (("facts", sorted(facts, key=lambda x: -x["entities"])),
             ("decisions", sorted(decisions, key=lambda x: -x["entities"])),
             ("open", sorted(open_items, key=lambda x: -x["entities"])),
             ("narrative", narrative))

    for bucket, items in order:
        for it in items:
            cost = len(it["text"])
            if spent + cost <= budget:
                kept[bucket].append(it)
                spent += cost
            else:
                dropped.append({"bucket": bucket, **it})

    # R1 兜底：叙述被丢、但它含实体 → 提级成一行"事实回链"，只留 ref + 首句片段，
    # 保证"被裁"的只是措辞，不是信息本身。
    promoted = []
    for d in dropped:
        if d["entities"] > 0:
            promoted.append({"text": d["text"][:60], "ref": d["ref"],
                             "entities": d["entities"], "promoted": True})
    if promoted:
        promo_budget = max(0, budget // 4)
        budget_left, taken = promo_budget, []
        for p in sorted(promoted, key=lambda x: -x["entities"]):
            if len(p["text"]) <= budget_left:
                taken.append(p)
                budget_left -= len(p["text"])
        kept["facts"].extend(taken)

    # R4 信息增量去重：结构项里"实体已被前面保留项全覆盖"的句子，只记重复次数，不占正文。
    #    为什么必须有这一层：真实技术对话里三十轮常是同一句模板（重试 6 次 / 超时 1800s /
    #    退出码 0），每句都含实体，于是 R1 把它们全部保住，ratio 恒为 1.00——
    #    压缩名存实亡，却因为"零丢失"看着很漂亮。这正是最该被抓的一类自欺。
    #    合并条件刻意保守：**只有新实体集合为空才合并**，只要冒出一个没见过的值就原样保留。
    def _flat(ents: dict[str, list[str]]) -> list[str]:
        return ["%s:%s" % (k, v) for k, vs in ents.items() for v in vs]

    seen: set[str] = set()
    repeats: list[dict[str, Any]] = []
    for bucket in ("facts", "decisions", "open"):
        survivors = []
        for it in kept[bucket]:
            ents = set(_flat(extract_entities(it["text"])))
            # 序号不算新信息：三十轮复核只有"第 N 轮"在动，若把序号也当新实体，
            #   压缩就永远压不动（实测 ratio 0.94，等于没压）。归并后 refs 逐条保留各轮，
            #   仍能回原文，所以这一步合并不丢信息。
            seq_vals = {"number:%s" % m for m in _SEQ_RE.findall(it["text"])}
            if ents and not ((ents - seq_vals) - seen):
                for r in repeats:
                    if r["bucket"] == bucket and r["entity_set"] == tuple(sorted(ents)):
                        r["refs"].append(it["ref"])
                        r["repeat"] += 1
                        break
                else:
                    repeats.append({"bucket": bucket, "text": it["text"],
                                    "refs": [it["ref"]], "repeat": 1,
                                    "entity_set": tuple(sorted(ents))})
                continue
            seen |= ents
            survivors.append(it)
        kept[bucket] = survivors

    if overflow_policy not in ("refs_only", "drop_lowest"):
        raise ValueError("overflow_policy 只支持 refs_only / drop_lowest")

    total_chars = sum(len(str(m["content"])) for m in msgs)
    compressed_chars = sum(len(i["text"]) for b in kept.values() for i in b)
    entity_table = _flatten_entities(ents_by_item)

    repeats_out = [{"bucket": r["bucket"], "text": r["text"], "refs": r["refs"],
                    "repeat": r["repeat"]} for r in repeats]
    return {
        "facts": kept["facts"],
        "decisions": kept["decisions"],
        "open": kept["open"],
        "narrative": kept["narrative"],
        "entities": entity_table,
        "repeats": repeats_out,
        "refs": sorted({i["ref"] for b in kept.values() for i in b}),
        "stats": {
            "repeated_merged": sum(r["repeat"] for r in repeats),
            "messages": len(msgs),
            "chars_in": total_chars,
            "chars_out": compressed_chars,
            "ratio": round(compressed_chars / total_chars, 3) if total_chars else 0.0,
            "dropped_sentences": len(dropped),
            "promoted": len(promoted),
            "entities": len(entity_table),
            "budget": budget,
        },
    }


def render(out: dict[str, Any]) -> str:
    """把压缩结果渲染成可喂给模型的文本。实体表固定放在最后，便于检索。"""
    lines: list[str] = []
    for title, key in (("事实", "facts"), ("决策", "decisions"), ("未决", "open")):
        items = out.get(key) or []
        if not items:
            continue
        lines.append("## " + title)
        for it in items:
            lines.append("- " + it["text"] + "  [%s]" % it["ref"])
    narr = out.get("narrative") or []
    if narr:
        lines.append("## 过程")
        for it in narr:
            lines.append("- " + it["text"])
    reps = out.get("repeats") or []
    if reps:
        # 重复结论不当成"被删掉"：给出代表句、重复次数与全部 ref，需要时可逐条回到原文。
        lines.append("## 同义重复（已归并，可回溯）")
        for r in reps:
            lines.append("- " + r["text"] + "  ×%d  [%s]"
                         % (r["repeat"] + 1, ", ".join(r["refs"])))
    ents = out.get("entities") or {}
    if ents:
        lines.append("## 实体索引（压缩后仍可回溯原文）")
        for val, refs in ents.items():
            lines.append("- %s ← %s" % (val, ", ".join(refs)))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 简报式压缩（brief）：为"下一次执行"挑材料，而不是为"复盘"讲历史
# ---------------------------------------------------------------------------
# 为什么 compact() 还不够
# ----------------------
# compact() 的判据是"实体零丢失"，而实体是路径 / 数字 / 标识符。于是它有一个人眼看不见
# 的盲区：**当"我们要干什么"那句话里一个实体都没有时，它会被判成叙述、按预算裁掉**——
# 细节全在，方向没了，而零丢失自检依然全绿。
#
#   原句：「这一步先把后端自己跑通，别急着接前端。」        → 无实体 → narrative → 被裁
#   原句：「实测 rc=0，耗时 397.3s，证据 docs/evidence/x」 → 有实体 → 必留
#
# 这正是 arXiv:2611.05306 附录 A 的论断：**好压缩不是执行历史的叙事，而是为后续执行
# 挑出来的简报**。四要素在这里的落点：
#
#   ① 输入任务的逐字副本      → task（verbatim：只裁尾不裁字，绝不做概括改写）
#   ② 当前工作 / 意图 / 下一步 → plan / next
#   ③ 与未来执行相关的产物+指针 → artifacts / pointers（含计划与判据的原文指针）
#   ④ "为执行挑"而非"讲历史"   → 预算只花在 plan / criteria 正文上，
#                              任务原文与产物指针**不参与预算竞争**（同 compact R2：
#                              拿方向去换细节，是错的方向）
#
# 分工：compact 管细节（不丢），brief 管方向（不散），brief_with_refs() 合体。
# ---------------------------------------------------------------------------

# 计划 / 判据 / 下一步 / 已完成 的锚点。宁可漏判成普通叙述，也不要误抓把简报撑成全文：
# 这四个桶吃的是预算，抓宽了等于没压。
PLAN_RE = re.compile(
    r"(?:^\s*(?:\d{1,2}[.、)]|[-*•]\s*(?:\[[ xX]?\]\s*)?)"
    r"|第一步|第二步|第三步|先[^。；\n]{0,12}再|计划|打算|接下来要|待办|TODO)", re.M)
CRIT_RE = re.compile(r"(必须|不得|禁止|不允许|验收|判据|通过标准|才算|合格线|零丢失|"
                     r"不能出现|硬约束|要求是)")
NEXT_RE = re.compile(r"(下一步|接下来|待办|尚未|还没|待补|待做|待跑|TODO|剩下|继续|未完)")
DONE_RE = re.compile(r"(已完成|已提交|已跑通|已修|已落盘|已通过|全绿|已经完成|均已)")

# 产物/指针类实体：这些是"未来执行要用的东西"，必须带出处，且永不参与预算。
ARTIFACT_KINDS = ("winpath", "posixpath", "file", "url", "commit", "uuid",
                  "verdict", "metric", "percent", "number")

# 但指针内部还得再分一层，否则"永不裁"会把压缩率拖死：
#   定位类（路径/URL/commit/uuid/判定语）——少一个下次就找不到东西了，永不裁，只许折叠；
#   度量类（metric/percent/number）——四十几轮各报一次"耗时 3xx.xs"，全留就是四十行噪声，
#   对未来执行的边际价值远低于路径。允许裁，但**裁了几条必须申报**（measure_dropped），
#   让"裁"这件事可被验证，而不是悄悄少记。
LOCATOR_KINDS = ("winpath", "posixpath", "file", "url", "commit", "uuid", "verdict")
MEASURE_KINDS = ("metric", "percent", "number")
MEASURE_KEEP = 12          # 度量类指针最多留最近 12 条

# 同目录同扩展名产物折叠阈值：6 条以内逐条列更好读，超过就从第 7 条起折。
FOLD_MIN = 6

_PATH_PARTS = re.compile(r"^(?P<dir>.*[\\/])?(?P<name>[^\\/]+?)(?P<ext>\.[A-Za-z0-9]{1,6})?$")


def _fold_key(value: str) -> tuple[str, str] | None:
    """产物路径的折叠分组键：(所在目录, 扩展名)。无扩展名的路径不折叠——
    `docs/evidence/run-1.raw.txt` 可以折成 `docs/evidence/*.raw.txt`，
    但 `D:\\project\\` 这种目录本身是唯一对象，折了就没意义。"""
    m = _PATH_PARTS.match(value or "")
    if not m:
        return None
    ext = (m.group("ext") or "").lower()
    if not ext:
        return None
    return (m.group("dir") or "", ext)


def _which_bucket(sentence: str) -> str:
    """一句只归一个桶，优先序 plan > criteria > next > done。

    为什么强制互斥：同一句在简报里出现三次，比漏掉它更糟——预算被自己的重复吃掉，
    而"重复占位"在统计上还看不出来（条数看着正常）。判据优先于 done 是有意的：
    "验收必须全绿"这种句子哪怕带"已"，约束价值也大于完成陈述。
    """
    if PLAN_RE.search(sentence):
        return "plan"
    if CRIT_RE.search(sentence):
        return "criteria"
    if NEXT_RE.search(sentence):
        return "next"
    if DONE_RE.search(sentence):
        return "done"
    return ""


def _pick_task(msgs: list[dict[str, Any]], max_task: int, task_min: int) -> dict[str, Any]:
    """取输入任务的逐字副本。

    规则（刻意保守，因为这一段是"方向"的唯一载体）：
      - 只取 role=="user" 的原话，不做任何概括、补全、改写；
      - 首条过短（< task_min 字）时顺延拼下一条，避免"继续"两个字当任务；
      - 超 max_task 只截尾，并把完整原文的 ref 留在 task["refs"] 里可回查。
    segments 逐段保留原文，brief_check() 拿它验证"逐字"这件事，而不是信任自己的拼接。
    """
    segs: list[dict[str, Any]] = []
    total = 0
    for m in msgs:
        if m.get("role") != "user":
            continue
        text = str(m.get("content") or "").strip()
        if not text:
            continue
        segs.append({"text": text, "ref": m["ref"]})
        total += len(text)
        if total >= task_min:
            break
    if not segs:
        return {"text": "", "segments": [], "refs": [], "truncated": False, "chars": 0}
    full = "\n".join(s["text"] for s in segs)
    return {"text": full[:max_task], "segments": segs,
            "refs": [s["ref"] for s in segs],
            "truncated": len(full) > max_task, "chars": len(full)}


def _artifacts(msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从**全文**抽产物与指针。

    注意这里不套预算：产物指针是"未来执行要用的东西"，预算再小也得保形（同 compact R2）。
    也因此它必须按原文全量抽，不能只看保留下来的句子——否则被裁掉那半段里的路径就真没了。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in msgs:
        for kind, vals in extract_entities(str(m.get("content") or "")).items():
            if kind not in ARTIFACT_KINDS:
                continue
            for v in vals:
                if v in seen:
                    continue
                seen.add(v)
                out.append({"value": v, "kind": kind, "ref": m["ref"]})
    return out


def _fold_artifacts(arts: list[dict[str, Any]], group_min: int = FOLD_MIN) -> list[dict[str, Any]]:
    """同目录同扩展名的产物指针，超过 group_min 条就折叠成一条组指针。

    为什么必须折：产物指针不参与预算，于是它会**线性增长**——四十轮每轮落一份
    `docs/evidence/run-N.raw.txt`，就是四十行路径，把压缩率死死压在 0.44 下不去
    （第一次跑真实规模自检时就是这么翻红的）。而不折也不是"更安全"：
    对下一次执行来说，"证据都在 docs/evidence/ 下、每轮一个 run-N" 和四十行逐条路径
    是同一个信息量，后者只是噪声。
    折叠不是丢信息：组内每个原值都进 folded 列表，判据 brief_check() 仍按展开后的
    全集核对（怕的就是拿折叠当借口偷偷少记两个），refs 也逐个留着，能回原文。
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    order: list[tuple[str, Any]] = []
    for a in arts:
        key = _fold_key(a["value"]) if a["kind"] in ("winpath", "posixpath", "file") else None
        if key is None:
            order.append(("single", a))
            continue
        if key not in groups:
            groups[key] = []
            order.append(("group", key))
        groups[key].append(a)

    folded: list[dict[str, Any]] = []
    for kind, obj in order:
        if kind == "single":
            folded.append(obj)
            continue
        members = groups[obj]
        if len(members) < group_min:
            folded.extend(members)
            continue
        d, e = obj
        folded.append({"value": (d or "") + "*" + e,
                       "kind": members[0]["kind"],
                       "ref": members[0]["ref"],
                       "refs": [m["ref"] for m in members],
                       "folded": [m["value"] for m in members],
                       "count": len(members)})
    return folded


def brief(
    messages: Iterable[dict[str, Any]],
    budget: int = 600,
    max_task: int = 200,
    task_min: int = 40,
) -> dict[str, Any]:
    """把上下文压成"给下一次执行看的简报"（附录 A 四要素）。

    参数
    ----
    budget   : 只约束 plan / criteria / next / done 四桶正文；任务原文与产物指针不吃预算。
    max_task : 任务原文保留上限（超出截尾 + 指针回查）。
    task_min : 首条 user 消息的"够用"字数，不足则顺延拼下一条。

    返回
    ----
    {"task", "plan", "criteria", "next", "done", "artifacts", "pointers", "stats"}
    """
    msgs: list[dict[str, Any]] = [dict(m) for m in messages]
    for i, m in enumerate(msgs):
        m.setdefault("ref", "msg[%d]" % i)
        m.setdefault("role", "user")
        m.setdefault("content", "")

    task = _pick_task(msgs, max_task=max_task, task_min=task_min)

    buckets: dict[str, list[dict[str, Any]]] = {n: [] for n in ("plan", "criteria", "next", "done")}
    for idx, m in enumerate(msgs):
        for s in _sentence_split(str(m.get("content") or "")):
            name = _which_bucket(s)
            if name:
                buckets[name].append({"text": s, "ref": m["ref"], "order": idx})

    # 预算分配：按"最近的优先"取，丢最老（更近的计划/判据更可能仍然成立）。
    #   取完再按时间还原成正序渲染——读者要的是"现在是什么状态"，不是倒序流水账。
    # 任务原句已整段出现在简报首段，桶里再列一遍就是同一句话占两份预算。
    #   注意要按**句**排除而不是按整条消息排除：一条 user 消息常常前半是任务、
    #   后半是硬约束，后半句该进 criteria，不能跟着整条消息一起被排掉。
    task_sents = {s for seg in task["segments"] for s in _sentence_split(seg["text"])}
    kept: dict[str, list[dict[str, Any]]] = {n: [] for n in buckets}
    spent = 0
    dropped = 0
    for name in ("plan", "criteria", "next", "done"):
        cands = [c for c in buckets[name] if c["text"] not in task_sents]
        for c in sorted(cands, key=lambda x: -x["order"]):
            cost = len(c["text"])
            if spent + cost <= budget:
                kept[name].append(c)
                spent += cost
            else:
                dropped += 1
        kept[name].sort(key=lambda x: x["order"])

    raw_arts = _artifacts(msgs)
    # 度量类只留最近 MEASURE_KEEP 条：定位类一条都不能少，度量类是"记账"不是"找东西"。
    measures = [a for a in raw_arts if a["kind"] in MEASURE_KINDS]
    keep_vals = {a["value"] for a in measures[-MEASURE_KEEP:]}
    measure_dropped = len(measures) - len(keep_vals)
    arts = _fold_artifacts([a for a in raw_arts
                            if a["kind"] in LOCATOR_KINDS or a["value"] in keep_vals])

    # 指针：给的是**桶内全量**（含被预算裁掉的），这样"判据清单还在原文里"这件事可查，
    #   而不是被自己的压缩结果自证。
    pointers = {"task": list(task["refs"])}
    for name in ("plan", "criteria", "next", "done"):
        refs: list[str] = []
        for c in buckets[name]:
            if c["ref"] not in refs:
                refs.append(c["ref"])
        pointers[name] = refs

    chars_in = sum(len(str(m["content"])) for m in msgs)
    chars_out = len(render_brief({"task": task, "plan": kept["plan"], "criteria": kept["criteria"],
                                  "next": kept["next"], "done": kept["done"], "artifacts": arts,
                                  "pointers": pointers}))
    return {
        "task": task,
        "plan": kept["plan"],
        "criteria": kept["criteria"],
        "next": kept["next"],
        "done": kept["done"],
        "artifacts": arts,
        "pointers": pointers,
        "stats": {
            "chars_in": chars_in,
            "chars_out": chars_out,
            "ratio": round(chars_out / chars_in, 3) if chars_in else 0.0,
            # 膨胀必须说出来：简报里"任务逐字副本 + 产物指针"是固定成本，短上下文里
            #   它一定比原文长（自检样本 256 字，ratio 1.78）。这不是 bug，是适用边界——
            #   骗人的做法是只报 ratio 不报方向；诚实的做法是把 expanded 摆出来，
            #   让调用方知道"这段压根不该压，直接给原文"。
            "expanded": chars_out > chars_in,
            "budget": budget,
            "task_chars": task["chars"],
            "task_truncated": task["truncated"],
            "counts": {n: len(kept[n]) for n in ("plan", "criteria", "next", "done")},
            "dropped": dropped,
            # 两个数都报：artifacts 是渲染成几行，artifact_items 是原文里到底多少个产物。
            #   只报前者会让"折叠了一百条"看起来像"只有一条"，这正是折叠最容易被滥用成
            #   "悄悄少记"的地方，所以两个数并排摆着，对不上就是有问题。
            "artifacts": len(arts),
            "artifact_items": len(raw_arts),
            # 裁了几条度量指针必须报出来：允许裁是设计，偷偷裁是缺陷。
            "measure_dropped": measure_dropped,
        },
    }


# 简报的适用下限：低于这个入参规模，固定成本（任务逐字 + 产物指针）压不回来，
# 直接给原文更省。真实技术对话三十轮轻松几千上万字，那时压缩才有意义。
BRIEF_MIN_INPUT = 800


def should_brief(messages: Iterable[dict[str, Any]]) -> bool:
    """这段上下文值不值得压成简报（而不是干脆原样喂）。

    判据只有一条但够用：入参是否远大于简报的固定成本。不做花哨的成本模型——
    多一层估算就多一层出错的地方，而这里错了的代价是"压完反而更长"。
    """
    total = sum(len(str(m.get("content") or "")) for m in messages)
    return total >= BRIEF_MIN_INPUT


def render_brief(out: dict[str, Any]) -> str:
    """把简报渲染成可喂给模型的文本：任务在最前（先立方向），产物指针在最后。"""
    lines: list[str] = ["## 任务（逐字，未改写）"]
    task = out.get("task") or {}
    text = (task.get("text") or "").strip()
    lines.append(text if text else "（无 user 原话）")
    if task.get("truncated"):
        lines.append("…（已截尾，完整原文见 %s）" % ", ".join(task.get("refs") or []))

    for title, key in (("计划", "plan"), ("完成判据", "criteria"),
                       ("下一步", "next"), ("已完成", "done")):
        items = out.get(key) or []
        if not items:
            continue
        lines.append("## " + title)
        for it in items:
            lines.append("- %s  [%s]" % (it["text"], it["ref"]))

    arts = out.get("artifacts") or []
    if arts:
        lines.append("## 产物与指针（不参与预算，永不裁）")
        for a in arts:
            if a.get("folded"):
                # 折叠组只给"在哪、多少个、怎么回原文"，不给四十行路径。
                #   但必须把"折叠了几个"写出来，否则读的人会以为只有一个。
                refs = a.get("refs") or []
                shown = ", ".join(refs[:3])
                if len(refs) > 4:
                    shown += " … " + refs[-1]
                lines.append("- %s ×%d（同目录同类产物已折叠，组内逐条可回原文）← %s"
                             % (a["value"], a["count"], shown))
            else:
                lines.append("- %s ← %s" % (a["value"], a["ref"]))

    pt = out.get("pointers") or {}
    extras = [(n, r) for n, r in pt.items() if n != "task" and r]
    if extras:
        lines.append("## 回原文指针（含被预算裁掉的项）")
        for name, refs in extras:
            lines.append("- %s: %s" % (name, ", ".join(refs)))
    return "\n".join(lines)


def brief_check(messages: Iterable[dict[str, Any]], out: dict[str, Any]) -> list[str]:
    """简报自检：三件必须成立的事，返回问题清单，空清单才算过关。

    1) 任务必须**逐字**来自某条 user 原文（不是概括、不是复述、不是拼凑）；
    2) 原文里所有产物类实体，简报里必须都在（产物指针零丢失）；
    3) 这些产物必须真的出现在渲染结果里（索引里有、正文外，等于没交给模型）。

    第 1 条是防"悄悄改写方向"，第 2 条是防"压着压着把要用的东西丢了"。
    """
    msgs: list[dict[str, Any]] = [dict(m) for m in messages]
    for i, m in enumerate(msgs):
        m.setdefault("ref", "msg[%d]" % i)
        m.setdefault("role", "user")
        m.setdefault("content", "")

    problems: list[str] = []
    orig = {m["ref"]: str(m["content"]) for m in msgs}
    segs = (out.get("task") or {}).get("segments") or []
    if not segs:
        problems.append("任务缺失：没有可作任务原句的 user 消息")
    for s in segs:
        if s["text"] not in orig.get(s["ref"], ""):
            problems.append("任务非逐字：%r 不在 %s 原文里" % (s["text"][:24], s["ref"]))

    want_loc: set[str] = set()
    want_measure: set[str] = set()
    for m in msgs:
        for kind, vals in extract_entities(str(m["content"])).items():
            if kind in LOCATOR_KINDS:
                want_loc |= set(vals)
            elif kind in MEASURE_KINDS:
                want_measure |= set(vals)
    arts = out.get("artifacts") or []
    # 折叠组按**展开后的全集**核对：折叠只允许省渲染的字，不允许变成"少记几条"。
    #   这条是折叠功能的守门人——没有它，折叠就是把"零丢失"悄悄降级成"零丢失（抽样）"。
    have: set[str] = set()
    for a in arts:
        have |= set(a.get("folded") or [a["value"]])
    miss = sorted(want_loc - have)
    if miss:
        problems.append("定位类指针缺失（不可裁）：" + "、".join(miss))

    # 度量类允许裁，但只允许"裁了并且申报了"。申报数对不上，说明代码在偷偷少记。
    real_dropped = len(want_measure - have)
    claimed = int(out["stats"].get("measure_dropped", 0) or 0)
    if real_dropped != claimed:
        problems.append("度量指针裁量与申报不符：实际少 %d 条，stats 说 %d 条"
                        % (real_dropped, claimed))

    hay = render_brief(out)
    for a in arts:
        if a.get("folded"):
            if a["value"] not in hay:
                problems.append("折叠组未渲染：%s" % a["value"])
            elif ("×%d" % a["count"]) not in hay:
                problems.append("折叠组条数未标注：%s 应标 ×%d" % (a["value"], a["count"]))
            continue
        if a["value"] not in hay:
            problems.append("产物未渲染：%s" % a["value"])
    return problems


def brief_with_refs(
    messages: Iterable[dict[str, Any]],
    budget: int = 600,
    detail_budget: int = 1500,
) -> dict[str, Any]:
    """合体版：简报给方向，compact 给细节，两者共用同一批 ref，可互相对照回原文。

    这不是"两个结果拼一起"图省事——分开的两份产物最容易出现"简报说要做 A，
    细节表里只有 B 的路径"，调用方拿到手还得自己判断哪份可信。合成一份时，
    detail.refs 与 pointers 用的是同一套 ref 命名，能直接对齐。
    """
    msgs = [dict(m) for m in messages]
    b = brief(msgs, budget=budget)
    c = compact(msgs, budget=detail_budget)
    b["detail"] = {"entities": c["entities"], "refs": c["refs"], "stats": c["stats"]}
    return b


# ---------------------------------------------------------------------------
# 简报自检
# ---------------------------------------------------------------------------
_BRIEF_MSGS: list[dict[str, Any]] = [
    {"role": "user", "ref": "u1", "content":
     "这一步我们先把后端自己跑通，别急着接前端。"
     "必须保证路径和阈值一个都不丢，验收标准是自检全绿且召回缺失为 0。"},
    {"role": "assistant", "ref": "a1", "content":
     "计划：1. 先落 brief 模块  2. 再接调用方  3. 最后跑自检。"
     "证据在 docs/evidence/compact.raw.txt，退出码 0。"},
    {"role": "tool", "ref": "t1", "content":
     "跑完 selfcheck：24 道门禁 24 通过，耗时 397.3s，内存 12.4 GB。"},
]


def _make_big_msgs(rounds: int = 40) -> list[dict[str, Any]]:
    """造一段"像真的一样"的长上下文：方向句 + 大量无实体的叙述轮 + 有实体的实测轮。

    为什么要程序生成而不是手写：手写样本会不自觉地写成"每句都有信息"，那样的样本
    压起来一定漂亮，但真实对话里超过一半是"这一轮我们主要是核对了一下，比上次清楚些"
    这类没有实体的叙述。样本不真，压缩率就是自娱自乐。
    """
    msgs: list[dict[str, Any]] = [
        {"role": "user", "ref": "u0", "content":
         "我们接着把后端这条线做完：先把压缩模块补齐，再回头动前端。"
         "必须保证路径与阈值零丢失，验收标准是自检全绿且召回缺失为 0。"}]
    for i in range(rounds):
        msgs.append({"role": "assistant", "ref": "a%d" % i, "content":
                     "第 %d 轮复核：这一步主要是把叙述和细节分开看，过程比上一轮清楚一些，"
                     "整体节奏还可以，暂时没有发现新的阻塞点，等下一轮再确认。" % (i + 1)})
        msgs.append({"role": "tool", "ref": "t%d" % i, "content":
                     "实测 rc=0，耗时 %d.%ds，证据 docs/evidence/run-%d.raw.txt，"
                     "内存占用 %d.4 GB。" % (300 + i, i, i, 8 + i % 5)})
    return msgs


def _selftest_brief() -> int:
    rc = 0

    out = brief(_BRIEF_MSGS, budget=600)
    probs = brief_check(_BRIEF_MSGS, out)
    st = out["stats"]
    print("[%s] brief: 任务 %d 字 / plan=%d criteria=%d next=%d done=%d / 产物=%d / ratio=%.3f %s"
          % ("OK " if not probs else "FAIL", st["task_chars"],
             st["counts"]["plan"], st["counts"]["criteria"], st["counts"]["next"],
             st["counts"]["done"], st["artifacts"], st["ratio"],
             "" if not probs else "问题：" + "；".join(probs)))
    if probs:
        rc = 1

    # 用例 A（这条最要紧）：方向句不含实体，compact 会把它当叙述裁掉，brief 必须保住。
    #   若这条通过而 compact 那条也通过，说明两个模块的盲区互补，才谈得上"加进项目"。
    #   注意样本句必须长于预算——真实的"我们要干什么"本来就带定语和条件，一句三十来字；
    #   第一次写这条用例时句子只有 21 字，预算 30 一塞就进去了，断言"compact 会丢方向"
    #   当场翻红。翻红的不是判据，是我不严谨：**用例前提必须先被自己证伪一遍**。
    dir_only = [
        {"role": "user", "ref": "d1", "content":
         "这一步我们先把后端自己跑通再说，别急着接前端那边的事情，一步一步来。"},
        {"role": "assistant", "ref": "d2", "content":
         "实测 rc=0，证据 docs/evidence/brief.raw.txt，耗时 397.3s。"},
    ]
    flat = render(compact(dir_only, budget=20))
    if "别急着接前端" in flat:
        print("[FAIL] 用例失效：compact 在 budget=20 下竟保住了纯方向句，"
              "该用例证明不了 brief 的必要性（请调小预算或加长样本句）")
        rc = 1
    else:
        print("[OK ] compact 丢方向（budget=20 下方向句被裁），符合该用例前提")

    b2 = brief(dir_only, budget=600)
    if "别急着接前端" not in render_brief(b2):
        print("[FAIL] brief 没保住任务原句里的方向：" + render_brief(b2).replace("\n", " | "))
        rc = 1
    else:
        print("[OK ] brief 保住方向：任务原句逐字在简报首段")

    # 用例 B（反向，防自证）：把 task 改写成概括 → brief_check 必须翻红。
    fake = brief(dir_only, budget=600)
    fake["task"] = {"text": "让后端跑通", "segments": [{"text": "让后端跑通", "ref": "d1"}],
                    "refs": ["d1"], "truncated": False, "chars": 5}
    if not brief_check(dir_only, fake):
        print("[FAIL] 判据失效：任务被概括改写后 brief_check 仍然通过（自证式自检）")
        rc = 1
    else:
        print("[OK ] 判据能翻红：任务被改写后 brief_check 报错")

    # 用例 C（反向）：删掉一个产物指针 → 必须翻红。
    b3 = brief(_BRIEF_MSGS, budget=600)
    if b3["artifacts"]:
        victim = b3["artifacts"].pop()
        if not brief_check(_BRIEF_MSGS, b3):
            print("[FAIL] 判据失效：删掉产物 %s 后 brief_check 仍通过" % victim["value"])
            rc = 1
        else:
            print("[OK ] 判据能翻红：删掉产物 %s 后报缺失" % victim["value"])
    else:
        print("[FAIL] 抽不到任何产物指针，用例无法执行（样本实体太弱）")
        rc = 1

    # 用例 D：预算压到 60，产物指针数量必须不变（产物不吃预算）。
    tiny = brief(_BRIEF_MSGS, budget=60)
    big = brief(_BRIEF_MSGS, budget=20000)
    if tiny["stats"]["artifacts"] != big["stats"]["artifacts"]:
        print("[FAIL] 产物指针被预算裁掉了：budget=60 时 %d 个，budget=20000 时 %d 个"
              % (tiny["stats"]["artifacts"], big["stats"]["artifacts"]))
        rc = 1
    else:
        print("[OK ] 产物指针不受预算影响：两档预算下均为 %d 个"
              % tiny["stats"]["artifacts"])

    # 用例 E：合体版必须同时给得出方向与细节。
    mix = brief_with_refs(_BRIEF_MSGS, budget=600, detail_budget=2000)
    if "别急着接前端" not in render_brief(mix):
        print("[FAIL] brief_with_refs 丢了方向")
        rc = 1
    elif not mix["detail"]["entities"]:
        print("[FAIL] brief_with_refs 丢了细节实体表")
        rc = 1
    else:
        print("[OK ] brief_with_refs：方向在首段，细节实体 %d 条"
              % len(mix["detail"]["entities"]))

    # 用例 I（重复计费）：任务原文里那句硬约束，既在首段任务里、又命中 criteria 桶，
    #   如果不去重就是同一句占两份预算，而肉眼看不出来（简报读着还挺"全"）。
    rep = render_brief(brief(_BRIEF_MSGS, budget=600))
    dup = rep.count("必须保证路径和阈值一个都不丢")
    if dup != 1:
        print("[FAIL] 任务原句在简报里出现 %d 次，应恰好 1 次（桶内重复占预算）" % dup)
        rc = 1
    else:
        print("[OK ] 任务原句只出现 1 次，桶内未重复计费")

    # 用例 K（度量申报的守门人）：手动把申报数改错，必须翻红。
    #   度量允许裁，唯一的约束就是"裁了多少必须说多少"；这条约束自己也得被验证。
    lied = brief(_BRIEF_MSGS, budget=600)
    lied["stats"]["measure_dropped"] = lied["stats"]["measure_dropped"] + 1
    if brief_check(_BRIEF_MSGS, lied):
        print("[OK ] 度量裁量申报必须与实际一致：改一个数立刻翻红")
    else:
        print("[FAIL] 度量裁量申报可被随意改动而不被发现")
        rc = 1

    # 用例 F（诚实性）：小上下文里简报必然比原文长，必须 self-report 出来。
    #   没有这条，ratio 就变成了"只在好看的时候才被引用"的指标。
    small = brief(_BRIEF_MSGS, budget=600)
    if not small["stats"]["expanded"]:
        print("[FAIL] 小样本（%d 字）简报应膨胀却没标 expanded，指标可能被选择性引用"
              % small["stats"]["chars_in"])
        rc = 1
    elif should_brief(_BRIEF_MSGS):
        print("[FAIL] should_brief 对 %d 字的小上下文返回 True，阈值失效"
              % small["stats"]["chars_in"])
        rc = 1
    else:
        print("[OK ] 膨胀被如实标注：小样本 ratio=%.2f expanded=True，且 should_brief=False"
              "（该场景应直接给原文）" % small["stats"]["ratio"])

    # 用例 G（真实规模）：40 轮长上下文，压缩率要真的下来，且方向与判据都不能丢。
    big_msgs = _make_big_msgs(40)
    big = brief(big_msgs, budget=600)
    probs_big = brief_check(big_msgs, big)
    if not should_brief(big_msgs):
        print("[FAIL] should_brief 对 %d 字的长上下文返回 False" % big["stats"]["chars_in"])
        rc = 1
    if big["stats"]["ratio"] > 0.35:
        print("[FAIL] 长上下文压缩率不合格：ratio=%.3f（>0.35），简报没起到压缩作用"
              % big["stats"]["ratio"])
        rc = 1
    if probs_big:
        print("[FAIL] 长上下文简报自检未过：" + "；".join(probs_big))
        rc = 1
    if not probs_big and big["stats"]["ratio"] <= 0.35:
        groups = [a for a in big["artifacts"] if a.get("folded")]
        print("[OK ] 真实规模：in=%d out=%d ratio=%.3f 指针行=%d（原始 %d 条，折叠组 %d 个）"
              " 判据=%d 方向保持=是"
              % (big["stats"]["chars_in"], big["stats"]["chars_out"], big["stats"]["ratio"],
                 big["stats"]["artifacts"], big["stats"]["artifact_items"],
                 len(groups), big["stats"]["counts"]["criteria"]))

    # 用例 H（折叠的守门人）：从折叠组里偷掉一条，brief_check 必须翻红。
    #   否则"折叠"就成了把"零丢失"偷偷降级成"零丢失（抽样）"的后门。
    if any(a.get("folded") for a in big["artifacts"]):
        hacked = brief(big_msgs, budget=600)
        for a in hacked["artifacts"]:
            if a.get("folded"):
                stolen = a["folded"][-1]
                a["folded"] = a["folded"][:-1]
                a["count"] -= 1
                break
        if not brief_check(big_msgs, hacked):
            print("[FAIL] 折叠可被用来少记：偷掉 %s 后 brief_check 仍通过" % stolen)
            rc = 1
        else:
            print("[OK ] 折叠不能用来少记：偷掉 %s 后 brief_check 立刻报缺失" % stolen)
    else:
        print("[FAIL] 长样本未触发折叠，用例 H 无法执行（FOLD_MIN 或样本需调整）")
        rc = 1
    return rc


def verify_recall(messages: Iterable[dict[str, Any]], out: dict[str, Any]) -> list[str]:
    """细节召回自检：原文里的实体，压缩后是否还在。

    这是"不准失质量"的唯一可执行判据。返回缺失实体清单，空清单 = 没丢。
    """
    originals: dict[str, list[str]] = {}
    for m in messages:
        for kind, vals in extract_entities(str(m.get("content", ""))).items():
            for v in vals:
                originals.setdefault(v, [])
    haystack = render(out)
    return [v for v in originals if v not in haystack]


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
_SELFTEST_MSGS = [
    {"role": "user", "ref": "turn1", "content":
     "门禁 17 的 A7 在全量回归时红了，单跑是 25/25 全绿，很可能是机器忙导致的偶发。"
     "我们决定把锚点改成流里第一个非终态帧，尝试次数从 3 次提到 6 次。"},
    {"role": "assistant", "ref": "turn2", "content":
     "改完复跑了两次：A7 首连收到 3 帧，锚点=40，续传补到 seq=41。"
     "全量结果是 24 道门禁 24 通过，演练 5 项按预期，耗时 397.3s，退出码 0。"},
    {"role": "tool", "ref": "call-x", "content":
     "证据落盘 docs/evidence/regress-all.raw.txt；模型用 minicpm5-2b:f16，"
     "单轮耗时 129.9s，共 110 轮，内存占用 12.4 GB。"},
]


def _selftest() -> int:
    rc = 0
    for budget in (2000, 600, 120):
        out = compact(_SELFTEST_MSGS, budget=budget)
        missed = verify_recall(_SELFTEST_MSGS, out)
        st = out["stats"]
        tag = "OK " if not missed else "FAIL"
        print("[%s] budget=%-5d in=%-4d out=%-4d ratio=%.3f entities=%-3d promoted=%d missed=%s"
              % (tag, budget, st["chars_in"], st["chars_out"], st["ratio"],
                 st["entities"], st["promoted"], missed or "0"))
        if missed:
            rc = 1
            print("      缺失细节：" + "、".join(missed))

    # 反向用例：预算 120 时叙述必须被压掉，否则说明预算没生效（假绿保护）
    tight = compact(_SELFTEST_MSGS, budget=120)
    if tight["stats"]["ratio"] > 0.5:
        print("[FAIL] 预算未生效：ratio=%.3f 应显著小于 1" % tight["stats"]["ratio"])
        rc = 1
    else:
        print("[OK ] 预算生效：ratio=%.3f" % tight["stats"]["ratio"])

    # 反向用例：纯叙述（无实体）应被压掉，而含实体句必须保形
    plain = [{"role": "user", "ref": "p", "content":
              "今天我们讨论了很多关于架构的想法，气氛不错，想法也比较多。"}]
    p = compact(plain, budget=40)
    if p["entities"]:
        print("[FAIL] 纯叙述文本不该抽出实体：%s" % p["entities"])
        rc = 1
    else:
        print("[OK ] 纯叙述文本无实体，压缩后 ratio=%.3f" % p["stats"]["ratio"])

    # 简报侧自检与上面同源：brief 与 compact 是同一条链上的两个模块，
    #   分开跑两份 rc 容易出现"一边绿一边红却只看绿的那份"。
    rc |= _selftest_brief()
    return rc


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return _selftest()
    # 简报两种出口：--brief 直接给人看（渲染文本），--brief-json 给下游程序拼装。
    #   刻意不复用 --json 的开关：两者语义不同（一个给模型看方向，一个做细节对账），
    #   混成一个开关后调用方一定会传错预算。
    if "--brief" in argv or "--brief-json" in argv:
        data = json.load(sys.stdin)
        out = brief_with_refs(data, budget=int(_arg(argv, "--budget", 600) or 600))
        if "--brief-json" in argv:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print(render_brief(out))
        probs = brief_check(data, out)
        if probs:
            # 问题走 stderr 且退出码非 0：让 CI 能拦住"压完方向没了"这种静默退化。
            print("简报自检未通过：" + "；".join(probs), file=sys.stderr)
            return 1
        return 0
    if "--json" in argv:
        data = json.load(sys.stdin)
        print(json.dumps(compact(data, budget=int(_arg(argv, "--budget", 1500) or 1500)),
                         ensure_ascii=False, indent=2))
        return 0
    print("用法: python workers/_compact.py --selftest | --json [--budget N]"
          " | --brief [--brief-json] [--budget N]")
    return 0


def _arg(argv: list[str], name: str, default: Any = None) -> Any:
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
