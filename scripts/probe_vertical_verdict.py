#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_vertical_verdict.py —— 垂直 agent「专属判据」探针（门禁 21）。

为什么需要这一道，而门禁 16/20 不够：
  16 号守的是**切词**，20 号守的是**取主题**。两者都只看"文字处理得干不干净"。
  但三个垂直 worker（narrative / market / qa）共用同一套信封（topic / keywords /
  open_subtasks / rule），于是出现一种非常隐蔽的退化：**切词漂亮、主题也对，结论却是
  通用骨架的空壳**——每个域都吐一样的东西，只是换了标题。
  这一道专门盯"这个域到底有没有它自己的判断"。

四组断言（分组的理由是它们各自的失效方式不同）：
  A 结构齐全   本域该有的判据表是否为声明的那几条。少一条 = 骨架被削。
  B 该抓就抓   喂真实抱怨，本域最该点出来的那句有没有点到。
               A 组保证"表在"，B 组保证"表被用上了"——只有 A 会漏掉"表是死的"。
  C 跨域隔离   拿别域的目标喂它，本域**不该有结论的地方必须没有结论**。
               这是垂直化真正成立的判据：能答自己的，也要能不答别人的。
               共用骨架最大的风险不是答错，是对什么都答。
  D 主题可读   三域各取一个真实目标，主题必须精确等于人工设定的那个词。
               20 号门禁用例集覆盖不到这三句（它们正是本轮实测抓出来的），
               在这里做精确断言，等于给 20 号补上一块用例。

退出码：0 = 全部通过；1 = 有 failed（逐条打印原因）。

用法：
  python scripts/probe_vertical_verdict.py
  python scripts/probe_vertical_verdict.py --verbose
  python scripts/probe_vertical_verdict.py --json out.json
  python scripts/probe_vertical_verdict.py --drill     # 负向演练：确认本门禁真的会红
"""

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKERS = pathlib.Path(os.environ.get("SA_WORKERS_DIR") or (ROOT / "workers"))

# 三句真实目标，全部来自本轮（2026-11-14）实跑，不是编的例句。
GOAL_NARRATIVE = "万炉第三章反派的动机转换太突然，跟前面立的设定冲突了，伏笔也没回收"
GOAL_MARKET = "对标幻兽帕鲁和戴森球做个市场分析，很多用户说好玩，再给个定价建议"
GOAL_QA = "这批接口改完要验收，页面要流畅、不能崩，改了接口契约"

# C 组隔离用的"纯外域"目标：刻意不含本域任何一个线索词。
GOAL_PURE_NARRATIVE = "这段剧情里反派凭空变强，前面完全没交代过，读者会觉得是硬加的"
GOAL_PURE_QA = "回归用例要覆盖边界值和异常路径，缺陷按级别分开"
GOAL_PURE_MARKET = "竞品矩阵按目标用户和价格带填，渠道按获客成本排"


def call(worker, goal, timeout=120):
    """起一个 worker 子进程，取回 [[SA]] 那行 JSON。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["SA_GOAL"] = goal
    try:
        r = subprocess.run([sys.executable, str(WORKERS / worker)], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", env=env,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "超时"
    lines = [l for l in (r.stdout or "").splitlines() if l.startswith("[[SA]]")]
    if not lines:
        return None, "无 [[SA]] 输出（rc=%d）%s" % (r.returncode, (r.stderr or "")[-200:])
    try:
        return json.loads(lines[-1][6:]), ""
    except json.JSONDecodeError as exc:
        return None, "[[SA]] 不是合法 JSON：%s" % exc


# ---------------------------------------------------------------- 断言
def check(structure, name, ok, detail=""):
    return dict(group=structure, name=name, ok=bool(ok), detail=detail)


def group_a(d):
    """A 结构齐全：本域判据表是否一条不少。"""
    nar, mar, qa = d["narrative"], d["market"], d["qa"]
    out = []

    cq = nar.get("canon_questions") or []
    out.append(check("A", "narrative 设定自洽三问", len(cq) == 3,
                     "实得 %d 条" % len(cq)))
    out.append(check("A", "narrative 动机链四段", len(nar.get("motive_chain") or []) == 4,
                     "实得 %d 段" % len(nar.get("motive_chain") or [])))
    states = [s.get("state") for s in (nar.get("foreshadow_states") or [])]
    want = {"已埋未收", "已埋已收", "未埋先收", "埋了不收"}
    out.append(check("A", "narrative 伏笔四态", want.issubset(set(states)),
                     "缺 %s" % (want - set(states)) if not want.issubset(set(states)) else "四态齐"))

    out.append(check("A", "market 竞品矩阵五维", len(mar.get("matrix_dims") or []) == 5,
                     "实得 %d 维" % len(mar.get("matrix_dims") or [])))
    out.append(check("A", "market 定位三问", len(mar.get("positioning") or []) == 3,
                     "实得 %d 问" % len(mar.get("positioning") or [])))
    out.append(check("A", "market 定价阶梯 ≥3 档", len(mar.get("pricing_ladder") or []) >= 3,
                     "实得 %d 档" % len(mar.get("pricing_ladder") or [])))

    tables = qa.get("case_tables") or []
    out.append(check("A", "qa 用例三张表都有内容",
                     len(tables) == 3 and all(t.get("items") for t in tables),
                     "实得 %d 张" % len(tables)))
    out.append(check("A", "qa 缺陷四级", len(qa.get("defect_levels") or []) == 4,
                     "实得 %d 级" % len(qa.get("defect_levels") or [])))
    vague = qa.get("metrics_from_vague") or []
    out.append(check("A", "qa 模糊词全部转成指标", len(vague) >= 3,
                     "实得 %d 条（目标含流畅/稳定/好用 三个模糊词）" % len(vague)))
    return out


def group_b(d):
    """B 该抓就抓：喂真实抱怨，最该点出来的那句有没有点到。"""
    out = []
    acts = d["narrative"].get("foreshadow_actions") or []
    out.append(check("B", "narrative 抱怨「没回收」要判收线不改判成没埋",
                     any("不收" in (a.get("state") or "") or "未收" in (a.get("state") or "")
                         for a in acts),
                     "实得 %s" % [a.get("state") for a in acts]))
    out.append(check("B", "narrative 抱怨「凭空变强」要判未埋先收",
                     any("未埋先收" in (a.get("state") or "")
                         for a in (d["narrative_orphan"].get("foreshadow_actions") or [])),
                     "实得 %s" % [a.get("state") for a in
                                 (d["narrative_orphan"].get("foreshadow_actions") or [])]))
    gaps = d["narrative"].get("motive_gaps") or []
    out.append(check("B", "narrative 反派目标要追问动机自证", any("自证" in g for g in gaps),
                     "实得 %d 条" % len(gaps)))

    vague = [v.get("word") for v in (d["market"].get("vague_claims") or [])]
    out.append(check("B", "market 「很多」要抓成空洞断言", "很多" in vague, "实得 %s" % vague))
    comp = d["market"].get("competitors") or []
    out.append(check("B", "market 并列竞品一个都不能漏",
                     "幻兽帕鲁" in comp and "戴森球" in comp, "实得 %s" % comp))

    scope = d["qa"].get("regression_scope") or []
    out.append(check("B", "qa 「改了接口契约」要扩到调用方回归",
                     any("调用方" in json.dumps(s, ensure_ascii=False) for s in scope),
                     "实得 %s" % [s.get("changed") for s in scope]))
    return out


def group_c(d):
    """C 跨域隔离：本域不该有结论的地方必须没有结论。"""
    out = []
    comp = d["market_on_narrative"].get("competitors") or []
    out.append(check("C", "market 收到纯叙事目标不编竞品", not comp, "实得 %s" % comp))

    vague = d["market_on_qa"].get("vague_claims") or []
    out.append(check("C", "market 收到验收目标不编空洞断言", not vague,
                     "实得 %s" % [v.get("word") for v in vague]))

    metrics = d["qa_on_narrative"].get("metrics_from_vague") or []
    out.append(check("C", "qa 收到叙事目标不硬转指标", not metrics,
                     "实得 %s" % [m.get("vague_word") for m in metrics]))

    scope = d["qa_on_market"].get("regression_scope") or []
    out.append(check("C", "qa 收到市场目标不编回归范围", not scope,
                     "实得 %s" % [s.get("changed") for s in scope]))

    acts = d["narrative_on_market"].get("foreshadow_actions") or []
    out.append(check("C", "narrative 收到市场目标不编伏笔台账", not acts,
                     "实得 %s" % [a.get("state") for a in acts]))

    gaps = d["narrative_on_qa"].get("motive_gaps") or []
    out.append(check("C", "narrative 收到验收目标不编动机缺口", not gaps, "实得 %d 条" % len(gaps)))
    return out


def group_d(d):
    """D 主题可读：三域主题必须精确等于人工设定的词。"""
    want = [("narrative", GOAL_NARRATIVE, "万炉"),
            ("market", GOAL_MARKET, "幻兽帕鲁"),
            ("qa", GOAL_QA, "接口契约")]
    out = []
    for key, _goal, expect in want:
        got = d[key].get("topic")
        out.append(check("D", "%s 主题精确=%s" % (key, expect), got == expect, "实得 %r" % got))
    return out


def collect():
    d = {}
    errors = []
    plan = {
        "narrative": ("narrative-agent.py", GOAL_NARRATIVE),
        "narrative_orphan": ("narrative-agent.py", GOAL_PURE_NARRATIVE),
        "market": ("market-agent.py", GOAL_MARKET),
        "qa": ("qa-agent.py", GOAL_QA + "，页面要流畅、要稳定、要好用"),
        "market_on_narrative": ("market-agent.py", GOAL_PURE_NARRATIVE),
        "market_on_qa": ("market-agent.py", GOAL_PURE_QA),
        "qa_on_narrative": ("qa-agent.py", GOAL_PURE_NARRATIVE),
        "qa_on_market": ("qa-agent.py", GOAL_PURE_MARKET),
        "narrative_on_market": ("narrative-agent.py", GOAL_PURE_MARKET),
        "narrative_on_qa": ("narrative-agent.py", GOAL_PURE_QA),
    }
    for key, (worker, goal) in plan.items():
        payload, err = call(worker, goal)
        if payload is None:
            errors.append("%s（%s）：%s" % (key, worker, err))
            d[key] = {}
        else:
            d[key] = payload
    return d, errors


def run_all():
    d, errors = collect()
    results = []
    if errors:
        for e in errors:
            results.append(check("X", "worker 调用失败", False, e))
        return results
    results += group_a(d)
    results += group_b(d)
    results += group_c(d)
    results += group_d(d)
    return results


# ---------------------------------------------------------------- 负向演练
def drill():
    """把复核用的副本改坏一处，确认门禁会红。

    改的是 narrative 的抱怨词表：把它清空，「伏笔也没回收」就不再被判成"埋了不收"，
    B 组第一条必须立刻变红。若这一跑还是全绿，说明 B 组断言是恒绿的——那才是真问题。
    """
    global WORKERS
    tmp = ROOT / "target" / "drill" / "workers-flat"
    if tmp.exists():
        pathguard.safe_rmtree(tmp, base=ROOT / "target" / "drill",
                              allow_names=("workers-flat",), what="垂直演练目录",
                              warn_only=True)
    tmp.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(WORKERS, tmp)

    target = tmp / "narrative-agent.py"
    src = target.read_text(encoding="utf-8")
    broken = src.replace('RECALL_CUES = ("没回收", "未回收", "没收", "没收回", "忘了收", '
                         '"没圆", "断头", "悬着", "没落地")', 'RECALL_CUES = ()')
    if broken == src:
        print("[DRILL] 未能改动副本（RECALL_CUES 原文不匹配），演练作废")
        return 2
    target.write_text(broken, encoding="utf-8")

    keep = WORKERS
    WORKERS = tmp
    try:
        results = run_all()
    finally:
        WORKERS = keep

    red = [r for r in results if not r["ok"]]
    print("[DRILL] 副本=%s  改了 RECALL_CUES=()" % tmp)
    print("[DRILL] 结果：%d 项中 %d 项变红" % (len(results), len(red)))
    for r in red:
        print("        - [%s] %s :: %s" % (r["group"], r["name"], r["detail"]))
    b1 = [r for r in results if r["name"].startswith("narrative 抱怨「没回收」")]
    if b1 and not b1[0]["ok"]:
        print("[DRILL] 目标断言已按预期变红，门禁不是恒绿。")
        return 0
    print("[DRILL] 目标断言**没有**变红 —— 这道门禁在这条上是假绿，需修断言。")
    return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="把结构化结果另存到该路径")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--drill", action="store_true", help="负向演练：改坏副本，确认门禁会红")
    args = ap.parse_args()

    if args.drill:
        return drill()

    results = run_all()
    failed = [r for r in results if not r["ok"]]

    print("=" * 78)
    for i, r in enumerate(results, 1):
        mark = "PASS" if r["ok"] else "FAIL"
        line = "[%s] %02d [%s] %s" % (mark, i, r["group"], r["name"])
        if args.verbose or not r["ok"]:
            line += "  -> %s" % r["detail"]
        print(line)
    print("=" * 78)
    ga = [r for r in results if r["group"] != "X"]
    by_group = {}
    for r in ga:
        g = by_group.setdefault(r["group"], [0, 0])
        g[0] += 1
        if r["ok"]:
            g[1] += 1
    print("分组：%s" % " | ".join("%s %d/%d" % (k, v[1], v[0]) for k, v in sorted(by_group.items())))
    print("合计 %d 项 | 通过 %d | 失败 %d"
          % (len(results), len(results) - len(failed), len(failed)))
    if failed:
        print("结论：垂直域判据有缺口——每一条都指向「某域少了自家判断」或「乱答别域」。")
    else:
        print("结论：三域专属判据齐全、该抓的都抓到、跨域不乱答、主题精确。")

    if args.json:
        outp = pathlib.Path(args.json)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print("结构化结果写入 %s" % outp)

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
