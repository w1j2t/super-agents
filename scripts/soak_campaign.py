#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
soak_campaign.py —— 「千轮实测战役」的合并看板。

为什么需要它：soak_project.py 一次只跑一批（一个 tag 一个 jsonl），批次之间各说各话。
而"这个项目到底好不好用"这种问题不能靠单批回答：
  · 只跑过 12 轮 smoke 就下结论 -> 样本不够，遇不到边界；
  · 只跑过一个语言 -> 把"模型不会 Rust"误判成"框架不行"；
  · 只跑过第一遍迭代 -> 看不到"在已有实现上继续改"时上下文是否还撑得住。

所以这里把多批数据合起来，并且分层看：语言 x 项目 x 阶段 x 迭代轮次。
它只做统计与判定，不产生数据，也不修改任何 jsonl。

用法：
  python scripts/soak_campaign.py
  python scripts/soak_campaign.py --target 1000 --check-target
  python scripts/soak_campaign.py --include camp       # 只算指定前缀的批次
  python scripts/soak_campaign.py --json               # 只要机器可读摘要

退出码：0 = 正常；2 = 指定 --check-target 但未达目标轮数。
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import pathlib
import statistics
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
EVID = ROOT / "docs" / "evidence" / "soak"


def load_rows(include: str = "", pattern: str = "*.jsonl"):
    """读全部批次，返回 [(tag, row), ...]。

    去重键用 (tag, round) 而不是整行哈希：续跑时同一轮可能被重跑并留下两行，
    整行哈希会当成两个样本，直接虚增样本量——那是自欺。
    """
    out, seen = [], set()
    for f in sorted(glob.glob(str(EVID / pattern))):
        tag = pathlib.Path(f).stem
        if tag.startswith("soak-"):
            tag = tag[len("soak-"):]
        if include and not tag.startswith(include):
            continue
        for line in io.open(f, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            key = (tag, row.get("round"))
            if key in seen:
                continue
            seen.add(key)
            out.append((tag, row))
    return out


def pct(a, b):
    return "%.1f%%" % (100.0 * a / b) if b else "-"


def med(seq, nd=1):
    seq = [x for x in seq if isinstance(x, (int, float))]
    return round(statistics.median(seq), nd) if seq else None


def pq(seq, p, nd=1):
    seq = sorted(x for x in seq if isinstance(x, (int, float)))
    if not seq:
        return None
    k = (len(seq) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(seq) - 1)
    return round(seq[lo] + (seq[hi] - seq[lo]) * (k - lo), nd)


def lang_of(row):
    lg = (row.get("code_lang") or "").lower()
    if lg in ("python", "py"):
        return "python"
    if lg in ("rust", "rs"):
        return "rust"
    return lg or "无代码/未知"


def group(rows, keyfn):
    g = {}
    for r in rows:
        g.setdefault(keyfn(r), []).append(r)
    return g


def layer_stats(rows):
    """一组样本的关键读数。

    "有代码"「能编译」「达交付标准」是三件事，合成一个数就定位不到问题在哪一层。
    """
    n = len(rows)
    return {
        "n": n,
        "done": sum(1 for r in rows if r.get("done")),
        "has_code": sum(1 for r in rows if (r.get("code_chars") or 0) > 0),
        "compiles": sum(1 for r in rows if r.get("compiles")),
        "delivered": sum(1 for r in rows if r.get("delivered_code")),
        "multi_agent": sum(1 for r in rows if (r.get("agents") or 0) > 1),
        "handoff": sum(int(r.get("handoff") or 0) for r in rows),
        "retries": sum(int(r.get("retries") or 0) for r in rows),
        "tokens": sum(int(r.get("tokens") or 0) for r in rows),
        "elapsed_p50": med([r.get("elapsed") for r in rows]),
        "elapsed_p95": pq([r.get("elapsed") for r in rows], 0.95),
        "code_chars_p50": med([r.get("code_chars") for r in rows], 0),
    }


def table(headers, rows):
    out = ["| " + " | ".join(headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def verdict(rows, target):
    """三句分开站得住的结论。

    坑：把"模型写不出 Rust"说成"框架不可靠"，是最容易犯也最误导人的归因错误。
    所以结论必须分层：框架层（有没有跑通、有没有协作）、产出层（有没有代码、能不能编译）、
    迭代层（跨 cycle 是否退化）。任何一层不成立，都不能拿另一层的话术去糊。
    """
    s = layer_stats(rows)
    n = s["n"] or 1
    fw, pd_, it = [], [], []

    if s["done"] >= n:
        fw.append("全部 %d 轮 request 均以 done 收尾（无卡死、无超时堆积）" % n)
    else:
        fw.append("%d/%d 轮 done，%d 轮未收尾（需看 http/status 是否 timeout）"
                  % (s["done"], n, n - s["done"]))
    fw.append("多 agent 协作占比 %s（%d/%d 轮参与 agent 数 >1），累计交接 %d 次"
              % (pct(s["multi_agent"], n), s["multi_agent"], n, s["handoff"]))
    if s["retries"]:
        fw.append("累计重试 %d 次，重试率 %.2f 次/轮" % (s["retries"], s["retries"] / n))

    pd_.append("有代码产出 %s，能编译 %s，达交付标准 %s"
               % (pct(s["has_code"], n), pct(s["compiles"], n), pct(s["delivered"], n)))
    pd_.append("代码字符数 P50 = %s，端到端耗时 P50 %ss / P95 %ss，tokens 合计 %d"
               % (s["code_chars_p50"], s["elapsed_p50"], s["elapsed_p95"], s["tokens"]))

    cycles = group(rows, lambda r: r.get("cycle") or 1)
    for c in sorted(cycles):
        cs = layer_stats(cycles[c])
        it.append("cycle %d：%d 轮，达交付 %s，编译 %s"
                  % (c, cs["n"], pct(cs["delivered"], cs["n"]), pct(cs["compiles"], cs["n"])))
    if len(cycles) <= 1:
        it.append("（目前只有 1 代迭代数据，跨代退化无法判定——需 cycle>1 的批次）")

    lines = ["### 框架层（系统能不能跑）"] + ["- " + x for x in fw]
    lines += ["", "### 产出层（模型交出了什么）"] + ["- " + x for x in pd_]
    lines += ["", "### 迭代层（第二代是否更差）"] + ["- " + x for x in it]
    lines += ["", "### 达标情况", "- 目标 %d 轮，实际去重后 %d 轮，差 %d 轮"
              % (target, s["n"], max(0, target - s["n"]))]
    return "\n".join(lines), s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=1000)
    ap.add_argument("--check-target", action="store_true")
    ap.add_argument("--include", default="", help="只统计 tag 以此开头的批次")
    ap.add_argument("--pattern", default="*.jsonl")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    pairs = load_rows(a.include, a.pattern)
    rows = [r for _, r in pairs]
    if not rows:
        print("没有找到任何批次数据：%s" % EVID)
        return 2 if a.check_target else 0

    s = layer_stats(rows)
    tags = sorted({t for t, _ in pairs})
    if a.json:
        print(json.dumps({"rows": s, "tags": tags, "target": a.target}, ensure_ascii=False))
        return 0 if (not a.check_target or s["n"] >= a.target) else 2

    print("# 千轮实测战役 · 合并看板")
    print()
    print("- 数据目录：`docs/evidence/soak/`")
    print("- 批次(%d)：%s" % (len(tags), ", ".join(tags)))
    print("- 去重后样本：**%d 轮**（按 tag+round 去重，续跑不重复计数）" % s["n"])
    print()

    print("## 总览")
    print(table(["指标", "值"], [
        ["done", "%d/%d (%s)" % (s["done"], s["n"], pct(s["done"], s["n"]))],
        ["有代码产出", "%d (%s)" % (s["has_code"], pct(s["has_code"], s["n"]))],
        ["能编译", "%d (%s)" % (s["compiles"], pct(s["compiles"], s["n"]))],
        ["达交付标准", "%d (%s)" % (s["delivered"], pct(s["delivered"], s["n"]))],
        ["多 agent 轮次", "%d (%s)" % (s["multi_agent"], pct(s["multi_agent"], s["n"]))],
        ["累计交接次数", s["handoff"]],
        ["累计重试次数", s["retries"]],
        ["tokens 合计", s["tokens"]],
        ["耗时 P50 / P95", "%ss / %ss" % (s["elapsed_p50"], s["elapsed_p95"])],
        ["代码字符 P50", s["code_chars_p50"]],
    ]))
    print()

    for title, keyfn in (("按语言", lang_of),
                         ("按项目", lambda r: r.get("project") or "?"),
                         ("按阶段", lambda r: r.get("stage") or "?"),
                         ("按迭代轮次", lambda r: "cycle %s" % (r.get("cycle") or 1))):
        g = group(rows, keyfn)
        rr = []
        # 历史批次里 stage 既是字符串也有纯数字（早期批次写的是 stage=1/2/3），
        # 直接 sorted 会 TypeError；这里统一按字符串排序，且输出前再 str() 一次。
        for k in sorted(g, key=lambda x: str(x)):
            cs = layer_stats(g[k])
            rr.append([str(k), cs["n"], pct(cs["done"], cs["n"]), pct(cs["has_code"], cs["n"]),
                       pct(cs["compiles"], cs["n"]), pct(cs["delivered"], cs["n"]),
                       cs["handoff"], cs["elapsed_p50"]])
        print("## " + title)
        print(table(["分组", "轮数", "done", "有代码", "编译", "达交付", "交接", "P50(s)"], rr))
        print()

    txt, _ = verdict(rows, a.target)
    print("## 结论")
    print(txt)

    if a.check_target and s["n"] < a.target:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
