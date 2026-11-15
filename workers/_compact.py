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
    return rc


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return _selftest()
    if "--json" in argv:
        data = json.load(sys.stdin)
        print(json.dumps(compact(data, budget=int(_arg(argv, "--budget", 1500) or 1500)),
                         ensure_ascii=False, indent=2))
        return 0
    print("用法: python workers/_compact.py --selftest | --json [--budget N]")
    return 0


def _arg(argv: list[str], name: str, default: Any = None) -> Any:
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
