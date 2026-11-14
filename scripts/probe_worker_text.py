#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_worker_text.py —— workers/_common.py 分词质量对照探针。

为什么单独做一个探针，而不是写进 cargo test：
  `_common.py` 是 12 个 Python worker 的公共入口（topic / keywords / tokens /
  split_sentences 被 9 个 worker 直接调用）。它跑在**子进程**里，Rust 侧的单测
  根本触不到——Rust 只能看到 worker 最后打印的 [[SA]] 那行 JSON，看不到中间
  "把「本地小模型」切成了「入本地小」"。所以这一层必须有自己的探针。

判据（分两级，刻意区分"丢词"和"排错序"）：
  must  —— 这些词**必须**出现在 tokens 里。缺了 = 真词被切丢（loss），红。
  top   —— 这些词必须进 keywords 前 8。tokens 有但被挤出前 8 = 排序问题，红。
  dirty —— 这些**绝不能**出现在 tokens / keywords 里。全部来自真实事故现场。
           注意：**不检查 topic**。topic 是原文的直接切片（"评估三台机器…"原样回显时
           天然包含 "台机器能"），拿它判脏会制造假阳性——第一版探针就栽在这上面，
           25 例报了 23 例红，其中一半是 topic 回显造成的。
  topic —— 主题必须包含的子串（可选）。

另有一条独立的格式判据（所有用例通用，不写在单例里）：
  topic 长度 ≤ 20 字。实测 "评估三台机器能不能跑起来这个游戏" 会被**整句**当成主题
  回显——`topic()` 只剥 _LEAD_VERBS 里登记过的引导动词，"评估"不在其中，于是没剥。

退出码：0 = 全部通过；1 = 有 failed（逐条打印）。**不做自适应放宽**：
一旦某条红了就修 `_common.py` 或调整该条的期望，绝不悄悄改判据。

用法：
  python scripts/probe_worker_text.py
  python scripts/probe_worker_text.py --json out.json     # 另存结构化结果
  python scripts/probe_worker_text.py --verbose           # 打印每例实际输出
"""

import argparse
import importlib.util
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
# 白名单变量，仅用于负向演练：把被测文件指向一份故意改坏的副本，确认本门禁真的会红。
# 平时不设置，永远测项目里的正本。
TARGET = pathlib.Path(os.environ.get("SA_COMMON_PY") or (ROOT / "workers" / "_common.py"))


def load_common():
    """按文件路径加载 _common.py（workers 下没有 __init__.py，不当包导入）。"""
    spec = importlib.util.spec_from_file_location("sa_worker_common", TARGET)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- 用例
# 全部取自这个项目真实跑过的目标语句，不是编的例句。
CASES = [
    # ---- 文档 / 计划类 ----
    dict(goal="帮我写一份把大模型引入本地小模型的试点报告，不要引入外部依赖",
         must=["试点", "大模型", "本地", "小模型"], top=["试点"],
         dirty=["入本地小", "地小模型", "一份把"]),
    dict(goal="把接口说明重写成中文，控制在 800 字以内",
         must=["接口", "说明", "中文", "800"], top=["接口"],
         dirty=["字以", "800字以"]),
    dict(goal="把上周的会议纪要汇总成周报，发给团队",
         must=["会议", "纪要", "周报"], top=["纪要"],
         dirty=["汇总成", "成周报"]),
    dict(goal="更新文档里的接口说明，字段名要和源码一致",
         must=["接口", "字段"], top=["接口"],
         dirty=["字段名要", "名要和"]),
    dict(goal="规划多 agents 集群的下一步，先做任务分发再做产物回传",
         must=["集群", "任务"], top=["集群"],
         dirty=["规划多", "的多"]),
    # ---- 文件 / 数据类 ----
    dict(goal="整理一下手机里的照片，按天归档到子文件夹",
         must=["照片", "归档"], top=["照片"],
         dirty=["天归档", "的子文件"]),
    dict(goal="给订单表加一个主键，顺便完成数据库迁移",
         must=["订单", "主键", "迁移"], top=["主键"],
         dirty=["单表加", "个主键顺"]),
    dict(goal="把本周的报销单汇总一下，按项目分类",
         must=["报销", "项目"], top=["报销"],
         dirty=["本周的报", "单汇总"]),
    dict(goal="每天九点提醒我备份一下工程目录",
         must=["备份", "工程", "目录"], top=["备份"],
         dirty=["每天九点提", "醒我备份"]),
    # ---- 排障 / 系统类 ----
    dict(goal="分析这次蓝屏的转储文件，找出最可能的驱动问题",
         must=["蓝屏", "转储", "文件", "驱动"], top=["蓝屏"],
         dirty=["次蓝屏", "可能的"]),
    dict(goal="排查高并发下的超时问题",
         must=["并发", "超时"], top=["超时"],
         dirty=["发下", "发下的"]),
    dict(goal="查一下 minicpm5-2b 在本机的显存占用",
         must=["显存"], top=["显存"],
         dirty=["m5-2", "cp m5"]),
    dict(goal="确认审计日志真的落盘了，不是只在内存里",
         must=["审计", "日志", "内存"], top=["审计"],
         dirty=["志真的落", "落盘了不"]),
    dict(goal="通过 Remote-SSH 连到集群，重启一下推理服务",
         must=["集群", "服务", "推理"], top=["集群"],
         dirty=["ssh 连到", "下推理服"]),
    # ---- 评估 / 对比类 ----
    dict(goal="评估三台机器能不能跑起来这个游戏",
         must=["评估", "机器", "游戏"], top=["机器"],
         dirty=["台机器能", "跑起来这"]),
    dict(goal="对比 2B 与 14B 模型在本机 CPU 上的耗时",
         must=["模型", "耗时"], top=["耗时"],
         dirty=["比 2B", "本机 cpu 上"]),
    dict(goal="挑三个候选方案，列出优缺点",
         must=["候选", "方案"], top=["方案"],
         dirty=["三个候选", "个候选"]),
    # ---- 编码 / 交付类 ----
    dict(goal="写一个清理脚本，不要动线上数据，只清本地缓存",
         must=["脚本", "缓存"], top=["脚本"],
         dirty=["不要动线", "清本地缓"]),
    dict(goal="这个方案不能覆盖历史数据，需要补一个回填脚本",
         must=["方案", "脚本", "回填"], top=["方案"],
         dirty=["不能覆盖历", "盖历史"]),
    dict(goal="给 SSE 加断点续传，避免断线后丢事件",
         must=["断点", "续传", "事件"], top=["断点"],
         dirty=["sse 加断", "线后丢"]),
    dict(goal="统一两个接口的字段口径，避免前端反复改",
         must=["接口", "字段", "口径"], top=["字段"],
         dirty=["个接口的", "免前端"]),
    dict(goal="先跑一遍冒烟测试；再跑全量门禁；最后把证据归档",
         must=["冒烟", "门禁", "证据"], top=["冒烟"],
         dirty=["跑一遍冒", "量门禁"]),
    # ---- 长期项目方向（游戏 / 语音 / 集群）----
    dict(goal="用 Godot 做一个 3D 修仙游戏的原型，先搭地形",
         must=["游戏", "原型", "地形"], top=["游戏"],
         dirty=["个 3d 修", "d 修仙"]),
    dict(goal="搭一个本地离线语音输入工具，识别中英文",
         must=["离线", "语音", "工具"], top=["语音"],
         dirty=["个本地离", "线语音"]),
    dict(goal="给我搞一个能把 PDF 转成图片的小工具",
         must=["图片", "工具"], top=["工具"],
         dirty=["搞一个", "个能把"]),
]


def run_case(mod, case):
    goal = case["goal"]
    kws = mod.keywords(goal, 8)
    toks = mod.tokens(goal)
    tp = mod.topic(goal)
    sents = mod.split_sentences(goal, by_comma=True)

    loss, rank, dirty, topic_bad = [], [], [], []

    def covered(w, bag):
        """词 w 是否被 bag 覆盖：相等、w 是某个长词的一部分、或某个短词是 w 的一部分。

        为什么要这么宽：keywords 有子串去重（长词吸收短词），所以「试点」在 tokens
        里存在、在 keywords 里由「试点报告」代表——这是**设计**，不是丢词。旧判据把
        它报成"丢失"，导致 25 例里一半红是这种假阳性（把设计当 bug 修会越修越错）。
        """
        return any(w == t or w in t or t in w for t in bag)

    for w in case.get("must", []):
        if not covered(w, toks):
            loss.append(w)
    for w in case.get("top", []):
        if covered(w, kws):
            continue
        if covered(w, toks):
            rank.append(w)
        else:
            loss.append("%s(连 tokens 都没有)" % w)

    # 只在真正被切出来的产物里判脏；topic 是原文切片，判它等于判原文。
    produced = kws + toks
    for w in case.get("dirty", []):
        if w in produced:
            dirty.append(w)

    expect_topic = case.get("topic")
    if expect_topic and expect_topic not in tp:
        topic_bad.append("主题缺 %r（实际 %r）" % (expect_topic, tp))
    if len(tp) > 20:
        topic_bad.append("主题超过 20 字，等于把整句复读（%r）" % tp)

    problems = []
    problems += ["丢词 must=%r（tokens 里没有）" % w for w in loss]
    problems += ["排序 top=%r（在 tokens 里、但被挤出 keywords 前 8）" % w for w in rank]
    problems += ["脏词 dirty=%r" % w for w in dirty]
    problems += topic_bad

    return dict(goal=goal, keywords=kws, tokens=toks, topic=tp, sentences=sents,
                loss=loss, rank=rank, dirty=dirty, problems=problems, ok=not problems)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="把结构化结果另存到该路径")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    mod = load_common()
    results = [run_case(mod, c) for c in CASES]
    failed = [r for r in results if not r["ok"]]

    if args.verbose:
        for i, r in enumerate(results, 1):
            print("[%02d] %s" % (i, r["goal"]))
            print("     keywords: %s" % r["keywords"])
            print("     topic   : %r" % r["topic"])
            if r["problems"]:
                for p in r["problems"]:
                    print("     !! %s" % p)
        print()

    print("=" * 74)
    for i, r in enumerate(results, 1):
        if r["ok"]:
            print("[OK  ] %02d %s  -> %s" % (i, r["goal"][:34], " / ".join(r["keywords"][:5])))
        else:
            print("[FAIL] %02d %s" % (i, r["goal"][:34]))
            for p in r["problems"]:
                print("        - %s" % p)
    print("=" * 74)
    print("合计 %d 例 | 通过 %d | 失败 %d" % (len(results), len(results) - len(failed), len(failed)))
    print("结论：%s" % ("分词输出无脏词、must 全在" if not failed else "存在真词丢失或脏词，需修 _common.py"))

    if args.json:
        outp = pathlib.Path(args.json)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print("结构化结果写入 %s" % outp)

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
