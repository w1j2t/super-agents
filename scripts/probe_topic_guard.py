#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_topic_guard.py —— 垂直 agent 主题取词质量门禁。

为什么不并进 probe_worker_text.py：
  门禁 16 只测 `_common.py` 的公共分词，它刻意**不判 topic**（topic 是原文切片，判脏会
  制造假阳性）。而 narrative / market / qa 三个垂直 worker 的产出质量都压在 topic 上——
  "在聊什么"错了，后面所有检查项都对着错的对象在问。两件事判据不同、红的原因不同，
  混在一个探针里会让"公共分词回归"和"垂直取词回归"互相盖住，所以单独立一道。

⚠ 本版的由来（别退回上一版写法）：
  第一版探针自己手写了一张域词表喂给 `pick()`，结果**全绿，而真实 worker 产出是脏的**——
  因为 worker 传的是 `_common.keywords()` 的分词结果，跟手写词表根本不是一回事：
      「这段剧情的主线推进太慢…」→ 分词给出 `段剧情`（量词残留）
      「上线前跑一遍冒烟…」    → 分词给出 `上线`（动词短语）
      「世界观设定跟旧设定冲突了…」→ 分词给出空表，只能吐兜底串
  门禁的输入必须与线上调用逐字一致，否则绿的是探针自己，不是产品。所以现在：
      · 关键词来源 = `_common.keywords(goal, 8)`，与三个 worker 完全同源
      · 域对象词表 = `_topic_guard.DOMAINS`，单一事实源，worker 与探针共用
  改这道门禁时，先确认这两条没被改回"探针自带一份词表"。

判据两级 + 三条格式红线：
  contain_any —— 至少命中一个域内对象词。用「任一」而不是「全部」：题目常同时提到多个
                 对象，取词只会挑一个，要求全含等于要求它抄全句（上一版就错在这，
                 把正确的 `暗黑破坏神` 判成红）。
  forbid      —— 结构/视角词不得单独当主题（主线/分析/建议），来自真实事故现场。
  格式        —— 非空、不超长、不整句回显、不是半句话。

负向演练 `--drill-no-guard`：
  取词换回裸 `_common.topic()`，跑同一批用例，**必须出现红**。若裸实现也全绿，说明用例
  太软、这道门禁拦不住任何退化，演练本身判失败。

退出码（沿用门禁 6 的四分法）：
  0 全过 / 演练按预期变红
  1 断言失败 / 演练没能变红
  2 脚本自身配置错（用例表写错）
  3 环境不具备（worker 文件缺失），记 SKIP，**不记 PASS**

用法：
  python scripts/probe_topic_guard.py
  python scripts/probe_topic_guard.py --verbose
  python scripts/probe_topic_guard.py --json out.json
  python scripts/probe_topic_guard.py --drill-no-guard
"""

import argparse
import importlib.util
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKERS = ROOT / "workers"
GUARD_PATH = WORKERS / "_topic_guard.py"
COMMON_PATH = WORKERS / "_common.py"

RC_OK, RC_FAIL, RC_CONFIG, RC_SKIP = 0, 1, 2, 3

MAX_TOPIC_LEN = 20
MAX_CLAUSE_LEN = 8

# 外部标准，刻意不从 _topic_guard 里 import 这几个常量：
# 探针若复用实现的判据常量，实现改坏了探针也跟着变瞎，门禁就失去意义。
# 唯一共用的是 DOMAINS（域对象词表）——那是业务声明，必须与线上同一份。
DIMENSION_WORDS_EXT = {"主线", "支线", "设定", "节奏", "叙事", "定位", "分析",
                       "评估", "优化", "建议", "方案", "思路", "方向", "问题", "情况"}
CLAUSE_FUNCTION_CHARS = "的了是把被让给都也就还又才和与及跟或"
INDICATOR_CHARS = "这那该本上下每各其此"
CLAUSE_TAIL_CHARS = "完好通了过掉成着遍次"
TREND_TAIL_CHARS = "线面上后前中外内"
QUERY_WORDS = ("哪个", "哪些", "哪种", "什么", "怎么", "怎样", "如何", "多少", "是否")

FALLBACKS = {
    "narrative": "未命名叙事单元",
    "market": "未命名市场对象",
    "qa": "未命名待测对象",
    "guard": "未命名目标",
}


def _load(name, path):
    """按文件路径加载 workers/ 下的模块。

    `_topic_guard.py` 第一行就是 `import _common as C`，它假定同目录在 sys.path 上。
    探针从 scripts/ 跑，必须先把 workers/ 垫进去，否则守卫一加载就 ModuleNotFoundError。
    """
    if str(WORKERS) not in sys.path:
        sys.path.insert(0, str(WORKERS))
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None:
        raise ImportError(f"无法加载 {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- 用例
# 目标语句全部取自三个 worker 真实处理过的任务（实跑原文）。关键词**不在这里写**，
# 由 `_common.keywords()` 现场生成，保证与线上调用同源。
CASES = [
    # ---- narrative：叙事 / 世界观 ----
    dict(worker="narrative", domain="narrative",
         goal="帮我理一下万炉第三章的伏笔，反派动机是不是站得住",
         contain_any=["万炉"],
         note="有作品专名时必须取到专名；「人物」「动机」是维度侧的词，顶不掉专名"),
    dict(worker="narrative", domain="narrative",
         goal="这段剧情的主线推进太慢，人物动机也说不通",
         contain_any=["剧情", "动机", "人物"],
         note="分词在这里会切出 `段剧情`——量词残留，是本探针第三轮抓到的真缺陷"),
    dict(worker="narrative", domain="narrative",
         goal="世界观设定跟旧设定冲突了，帮我校一遍",
         contain_any=["世界观"],
         note="分词在这里给出**空表**，只能靠域对象词表兜住；上一版会直接吐兜底串"),

    # ---- market：市场 / 竞品 / 定价 ----
    dict(worker="market", domain="market",
         goal="对标两款竞品做个市场分析，再给个定价建议",
         contain_any=["竞品", "竞对", "定价"],
         note="分词只给出 `分析`/`建议`，两个都是视角词，必须由域表补 `竞品`"),
    dict(worker="market", domain="market",
         goal="这个产品投放选哪个渠道，回本周期怎么算",
         contain_any=["渠道", "投放"],
         note="「这个…」开头的目标是半句话高发区"),
    dict(worker="market", domain="market",
         goal="《暗黑破坏神》和《流放之路》的定价对比",
         contain_any=["暗黑破坏神", "流放之路", "定价"],
         note="书名号专名优先；上一版在这里要求必须含「定价」，把正确结果判成了红"),

    # ---- qa：测试 / 验收 ----
    dict(worker="qa", domain="qa",
         goal="这批接口改完，帮我列验收标准和回归范围",
         contain_any=["验收", "回归", "接口"],
         note="`这批接口改完` 是子句不是词——第二轮抓到的真缺陷"),
    dict(worker="qa", domain="qa",
         goal="上线前跑一遍冒烟，缺陷按级别分一下",
         contain_any=["冒烟", "缺陷"],
         note="分词第一个词是 `上线`（动词短语），必须被挡掉"),
    dict(worker="qa", domain="qa",
         goal="梳理万炉第三章的剧情",
         contain_any=["万炉", "剧情"],
         note="越界目标也要取对对象：问的是叙事，不是测试"),

    # ---- 对抗样例：只要求"不出丑"，不要求命中域内词 ----
    dict(worker="guard", domain=None, goal="批一下",
         contain_any=[], expect_fallback=True, note="只有引导动词，必须走兜底而不是吐「批」"),
    dict(worker="guard", domain=None,
         goal="把上周所有改动都过一遍然后按重要性排个序再给我一份完整报告",
         contain_any=[], max_len=MAX_TOPIC_LEN, note="超长整句不得原样回显"),
    dict(worker="guard", domain=None, goal="",
         contain_any=[], expect_fallback=True, note="空目标必须有兜底，不能吐空串"),
]


def looks_like_sentence(t: str) -> str:
    """独立判「这是半句话」。与实现分开写，实现改坏了这里仍然抓得到。返回原因或空串。"""
    if re.search(r"[，。、；：！？,;:!?]", t):
        return "串内带标点"
    if len(t) >= 4 and t[0] in INDICATOR_CHARS:
        return "以指示/量词开头"
    if len(t) > MAX_CLAUSE_LEN and any(ch in t for ch in CLAUSE_FUNCTION_CHARS):
        return "超长且含虚词"
    if len(t) >= 3 and t[-1] in CLAUSE_TAIL_CHARS:
        return "以补语字收尾"
    if any(w in t for w in QUERY_WORDS):
        return "含疑问词"
    if len(t) == 2 and t[-1] in TREND_TAIL_CHARS:
        return "两字且趋向字收尾"


def run_case(case, pick):
    """返回 (ok, detail)。pick 可注入，负向演练时换成裸实现。"""
    goal = case["goal"]
    fb = FALLBACKS[case["worker"]]
    topic = pick(goal, fb, case.get("domain"))
    problems = []

    if not topic or not topic.strip():
        return False, "topic 为空"

    limit = case.get("max_len", MAX_TOPIC_LEN)
    if len(topic) > limit:
        problems.append(f"超长 {len(topic)} 字（上限 {limit}）：{topic}")
    if goal.strip() and topic.strip() == goal.strip():
        problems.append(f"整句回显：{topic}")

    is_fallback = topic.strip() == fb
    if case.get("expect_fallback") and not is_fallback:
        problems.append(f"应走兜底「{fb}」，实际={topic}")

    if not is_fallback:
        wants = case.get("contain_any") or []
        if wants and not any(w in topic for w in wants):
            problems.append(f"未命中任一对象词 {wants}：{topic}")
        if topic.strip() in DIMENSION_WORDS_EXT:
            problems.append(f"结构/视角词被单独当成主题：{topic}")
        why = looks_like_sentence(topic)
        if why:
            problems.append(f"半句话（{why}）：{topic}")

    return (not problems), ("；".join(problems) if problems else topic)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None, help="另存结构化结果")
    ap.add_argument("--verbose", action="store_true", help="打印每例实际取词")
    ap.add_argument("--drill-no-guard", action="store_true",
                    help="负向演练：改用裸 _common.topic()，期望本探针变红")
    args = ap.parse_args()

    if not GUARD_PATH.exists() or not COMMON_PATH.exists():
        print(f"[SKIP] 缺文件：{GUARD_PATH} 或 {COMMON_PATH}")
        return RC_SKIP

    # 配置自检：`contain_any` 是**同义词族**，要求整族都出现在目标里不合理（`竞对` 只是
    # `竞品` 的别写）。真正要防的是"整族都不在目标里"——那说明用例表写串行了。
    for c in CASES:
        wants = c.get("contain_any") or []
        if wants and not any(sub in c["goal"] for sub in wants):
            print(f"[CONFIG] 用例对象词 {wants} 无一出现在目标里：{c['goal']}")
            return RC_CONFIG

    guard = _load("sa_topic_guard", GUARD_PATH)
    common = _load("sa_worker_common", COMMON_PATH)

    # 域词表必须能在守卫里查到，否则是接线错了
    for c in CASES:
        d = c.get("domain")
        if d and d not in guard.DOMAINS:
            print(f"[CONFIG] DOMAINS 里没有域「{d}」")
            return RC_CONFIG

    if args.drill_no_guard:
        def pick(goal, fb, domain=None):
            got = common.topic(goal) if goal.strip() else ""
            return got or fb
        print("=== 负向演练：取词换回裸 _common.topic()（守卫缺席时的旧行为）===")
    else:
        def pick(goal, fb, domain=None):
            # 与三个 worker 逐字一致：关键词来自公共分词，域对象词表来自守卫常量
            kws = common.keywords(goal, 8) if goal.strip() else []
            return guard.pick(goal, kws, fb, domain=domain)

    results, failed = [], 0
    for c in CASES:
        ok, detail = run_case(c, pick)
        # 守卫自己的 acceptable() 是"能不能当主题"的最终关卡；兜底串是人工写的，免检
        if ok and detail.strip() not in FALLBACKS.values():
            if not guard.acceptable(detail):
                ok, detail = False, f"未通过 acceptable()：{detail}"
        failed += 0 if ok else 1
        results.append(dict(worker=c["worker"], domain=c.get("domain"), goal=c["goal"],
                            topic=detail, ok=ok, note=c.get("note", "")))
        line = f"[{'PASS' if ok else 'FAIL'}] {c['worker']:<9} {c['goal'][:26]:<28}"
        if args.verbose or not ok:
            line += f" → {detail}"
        print(line)
        if not ok and c.get("note"):
            print(f"        判据意图：{c['note']}")

    total = len(CASES)
    print(f"\n选题用例 {total} 条 | 通过 {total - failed} | 失败 {failed}")

    if args.json:
        pathlib.Path(args.json).write_text(
            json.dumps(dict(total=total, failed=failed, drill=args.drill_no_guard,
                            results=results), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结构化结果：{args.json}")

    if args.drill_no_guard:
        if failed > 0:
            print(f"演练按预期变红：{failed}/{total} 条在无守卫时失败 → 本探针具备变红能力")
            return RC_OK
        print("演练失败：拿掉守卫后仍然全绿，说明用例太软、这道门禁拦不住退化")
        return RC_FAIL

    if failed:
        print("结论：有断言失败，见上方 FAIL 行")
        return RC_FAIL
    print("结论：全部通过")
    return RC_OK


if __name__ == "__main__":
    sys.exit(main())
