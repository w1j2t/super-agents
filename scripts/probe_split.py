#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""probe_split.py —— 整包任务"先骨架、后填实"切分策略的 A/B 对照（冲刺二）

## 这道题要回答什么

2026-11-16 的 119 轮实测里，**占位符 21 条几乎全落在一类任务上**：P10 任务队列 9、
P9 微服务 6、P6 API 文档生成 4、P5/P8 各 1。它们是每个项目的第一阶段，措辞都是
"写一个完整的程序，要能直接运行，含 main"。而事后补救（`--decompose`，两轮）
在 28 条样本上只救回 3 条（10.7%）——因为第一轮模型已经把程序写飞了，
第二轮是在废墟上打补丁，而不是在一块干净地基上继续盖。

## 假设

对小模型，"一次交一整个程序"这件事本身超出了它的组织能力；但拆成
「先定契约 → 再逐件填 → 最后拼装」三段，每段只有一件事要操心，成功率应该更高。

## 做法

同一批整包任务、同一台机器、同一个模型，A/B **交错**跑（防时间漂移混入）：

| 臂 | 做法 | 请求数 |
|----|------|--------|
| A 现状 | 原样单轮，整包 | 1 |
| B 切分 | 段1 契约骨架 → 段2 按骨架填函数体 → 段3 补 main 拼装 | 3 |

判分口径**完全复用主账**（`soak_project.judge` / `usable_of`），不新造尺子：
若 B 臂好看是因为判据变松，那这次对照就白做了。

## 输出

- `docs/evidence/split-ab.jsonl` —— 逐次请求样本（两臂都记，可重算）
- `docs/evidence/split-ab.md`    —— 对照表：可用率 / 各段通过率 / 成本

## 用法

    python scripts/probe_split.py --reps 3
    python scripts/probe_split.py --reps 3 --report-only
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import probe_llm_soak as soak          # noqa: E402
import soak_project as SP              # noqa: E402

EVID = ROOT / "docs" / "evidence"
OUT_JSONL = EVID / "split-ab.jsonl"
OUT_MD = EVID / "split-ab.md"
HOLDER_MODE = False          # True = 靶子取自 camp-g 的 21 条真实失败样本


def holders():
    """靶子＝camp-g 长跑里真实产出占位符的那批任务（21 条）。

    为什么必须有这个模式：`targets()` 拿的是"六个整包型项目的首阶段"。那是干净的
    从头跑，A 臂本来就 75% 可用 —— 拿它验"救回率"等于让两个健康人比赛谁先病。
    真正的靶子是**已经失败过**的样本：模型在这 21 条上确实交了 `...` / 空壳，
    现在问的是"重跑一次"和"切三段"哪个能把它们救回来。

    `goal` 字段在 soak 记录里存的是**原始任务原话**，直接取来复用即可，
    不需要重写题目 —— 靶子和长跑时一模一样，这才叫对照。
    """
    p = EVID / "soak" / "soak-camp-g.jsonl"
    if not p.exists():
        return []
    seen = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("compile_source") != "placeholder":
            continue
        g = (r.get("goal") or "").strip()
        if not g:
            continue
        if g in seen:
            seen[g]["dup"] += 1
            continue
        seen[g] = {
            "project": r.get("project") or "?",
            "goal": g,
            "lang": SP._project_lang([g]),
            "round": r.get("round"),
            "dup": 0,
        }
    return list(seen.values())


def targets(holder=False):
    """靶子＝被 is_whole_package 判为整包型的项目首阶段任务。

    不硬编码项目名：判据与调用方（soak_project）用同一把尺子，
    以后再加整包型项目，这里自动跟上，不会出现"新题目没进对照"的假结论。
    """
    if holder:
        return holders()
    out = []
    for name, stages in SP.PROJECTS:
        g = stages[0]
        if SP.is_whole_package(g):
            out.append({"project": name, "goal": g, "lang": SP._project_lang([g])})
    return out


def _brief(j, step, elapsed):
    return {
        "step": step,
        "status": j.get("status"),
        "code_chars": j.get("code_chars"),
        "compiles": j.get("compiles"),
        "compiles_raw": j.get("compiles_raw"),
        "delivered": j.get("delivered_code"),
        "usable": SP.usable_of(j),
        "reason": SP.failure_reason(j),
        "tokens": j.get("tokens"),
        "elapsed": round(elapsed, 1),
    }


def run_arm_a(base, tgt, rep, tag, timeout):
    """A 臂：现状——一次要整个程序。"""
    t0 = time.time()
    row = soak.run_task(base, tgt["goal"], None, "%s-a-r%d" % (tag, rep), timeout=timeout)
    j = SP.judge(row)
    return _brief(j, 1, time.time() - t0), j


def run_arm_b(base, tgt, rep, tag, timeout):
    """B 臂：三段接力。任一段交不出可用骨架就跳到降粒度兜底，并如实记原因。"""
    g = tgt["goal"]
    lang = tgt["lang"]
    steps = []
    t_all = time.time()

    # 段1 只要骨架。它交不出骨架（比如又背了一遍 hello-world 模板），
    # 就不硬着头皮往下走——那等于在空地基上盖三楼，只会白烧两轮。
    t0 = time.time()
    r1 = soak.run_task(base, SP.split_goal(g, 1, "", lang), None,
                       "%s-b1-r%d" % (tag, rep), timeout=timeout)
    j1 = SP.judge(r1)
    code1 = SP._prev_code(j1)
    steps.append(_brief(j1, 1, time.time() - t0))

    if not SP._skel_ok(code1, lang):
        # 兜底：降粒度，只要一个核心函数（复用 compose 的最小分支）。
        t0 = time.time()
        r3 = soak.run_task(base, SP.decompose_goal(g, "", lang), None,
                           "%s-bf-r%d" % (tag, rep), timeout=timeout)
        j3 = SP.judge(r3)
        steps.append(_brief(j3, 3, time.time() - t0))
        return steps, None, j3, "fallback", round(time.time() - t_all, 1)

    # 段2 在骨架内填函数体。禁止改签名/加函数/写 main，把"组织结构"剥离出去。
    t0 = time.time()
    r2 = soak.run_task(base, SP.split_goal(g, 2, code1, lang), None,
                       "%s-b2-r%d" % (tag, rep), timeout=timeout)
    j2 = SP.judge(r2)
    code2 = SP._prev_code(j2)
    steps.append(_brief(j2, 2, time.time() - t0))

    # 段3 拼装。段2 没拿到代码时，拿骨架去拼装（至少结构还在，别让它从零开始）。
    t0 = time.time()
    r3 = soak.run_task(base, SP.split_goal(g, 3, code2 or code1, lang), None,
                       "%s-b3-r%d" % (tag, rep), timeout=timeout)
    j3 = SP.judge(r3)
    steps.append(_brief(j3, 3, time.time() - t0))
    return steps, j2, j3, "three-step", round(time.time() - t_all, 1)


def report():
    """从 jsonl 重算对照表。所有数字都从原始样本现算，不存"已算好的结论"。"""
    rows = []
    if OUT_JSONL.exists():
        with open(OUT_JSONL, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    if not rows:
        print("[split] 没有样本，先跑一遍")
        return 1

    a = [r for r in rows if r["arm"] == "A"]
    b = [r for r in rows if r["arm"] == "B"]

    def _verdict(r):
        """从样本里的逐段记录现算结论，不信 jsonl 里存的那个字符串。

        为什么不信存量：跑这批样本的进程加载的是修 `_usable_flag` 之前的代码，
        它写进文件的 verdict 全是 still-bad。数据本身（每段的 usable）是对的，
        所以结论可以重算——不能因为跑的时候代码有病，就把数据一起作废。
        """
        if r.get("path") == "fallback":
            return "fallback-ok" if r["final"]["usable"] else "fallback-bad"
        s3 = next((s for s in r["steps"] if s["step"] == 3), None)
        s2 = next((s for s in r["steps"] if s["step"] == 2), None)
        if SP._usable_flag(s3):
            return "rescued"
        if SP._usable_flag(s2):
            return "partial"
        return "still-bad"

    a_ok = sum(1 for r in a if r["final"]["usable"])
    b_ok = sum(1 for r in b if r["final"]["usable"])
    b_full = sum(1 for r in b if _verdict(r) == "rescued")
    b_part = sum(1 for r in b if _verdict(r) == "partial")
    fb = sum(1 for r in b if r["path"] == "fallback")
    # 三段接力真正走完的样本，与走兜底的样本分开算——混在一起会把
    # 「降粒度补救」的功劳记到「切分策略」头上。
    t3 = [r for r in b if r["path"] == "three-step"]
    t3_ok = sum(1 for r in t3 if r["final"]["usable"])

    def _pct(x, n):
        return "%.1f%%" % (100.0 * x / n) if n else "-"

    def _avg(vals):
        vals = [v for v in vals if isinstance(v, (int, float))]
        return round(sum(vals) / len(vals), 1) if vals else None

    # 各段通过率：段1 出骨架、段2 出可编译、段3 出可用
    seg = {}
    for st in (1, 2, 3):
        got = [s for r in b for s in r["steps"] if s["step"] == st]
        seg[st] = {
            "n": len(got),
            "code": sum(1 for s in got if (s["code_chars"] or 0) > 0),
            "comp": sum(1 for s in got if s["compiles"] is True),
            "usable": sum(1 for s in got if s["usable"]),
            "chars": _avg([s["code_chars"] or 0 for s in got]),
            "secs": _avg([s["elapsed"] for s in got]),
        }

    s = {
        "samples": len(rows), "A_n": len(a), "B_n": len(b),
        "A_usable": a_ok, "A_rate": _pct(a_ok, len(a)),
        "B_usable": b_ok, "B_rate": _pct(b_ok, len(b)),
        "B_full": b_full, "B_partial": b_part, "B_fallback": fb,
        "t3_n": len(t3), "t3_ok": t3_ok, "t3_rate": _pct(t3_ok, len(t3)),
        "A_secs": _avg([r["elapsed"] for r in a]),
        "B_secs": _avg([r["elapsed"] for r in b]),
        "A_tokens": _avg([r["final"]["tokens"] for r in a]),
        "B_tokens": _avg([sum(x["tokens"] or 0 for x in r["steps"]) for r in b]),
        "A_reasons": {}, "B_reasons": {}, "seg": seg,
    }
    for r in a:
        k = r["final"]["reason"]
        s["A_reasons"][k] = s["A_reasons"].get(k, 0) + 1
    for r in b:
        k = r["final"]["reason"]
        s["B_reasons"][k] = s["B_reasons"].get(k, 0) + 1

    _h = HOLDER_MODE
    lines = [
        "# " + ("占位符失败样本救回 A/B 对照（split-ab-holder）" if _h
                else "整包任务切分策略 A/B 对照（split-ab）"),
        "",
        "> 由 `scripts/probe_split.py` 生成。判分口径与主账一致（`soak_project.judge`），"
        "未新造尺子。样本 %d 次请求。" % len(rows),
        "",
        ("> 靶子＝camp-g 长跑里 `compile_source=placeholder` 的真实失败样本（%d 条，"
         "原任务原话逐字复用），问的是**已经失败过的题，重跑和切分谁救得回来**。"
         % len({r.get("target") for r in rows}) if _h else ""),
        "",
        "## 一、结论先行",
        "",
        "| 臂 | 做法 | 样本 | 可用交付 | 可用率 | 平均墙钟 | 平均 token |",
        "|----|------|------|----------|--------|----------|------------|",
        "| A | 单轮整包（现状） | %d | %d | **%s** | %ss | %s |"
        % (len(a), a_ok, s["A_rate"], s["A_secs"], s["A_tokens"]),
        "| B | 三段接力（切分） | %d | %d | **%s** | %ss | %s |"
        % (len(b), b_ok, s["B_rate"], s["B_secs"], s["B_tokens"]),
        "",
        "B 臂内部台阶：完整救回 %d 条、半截救回 %d 条、走降粒度兜底 %d 条。"
        % (b_full, b_part, fb),
        "",
        "**只看三段接力真正走完的样本**（剔除降粒度兜底）：%d 条里可用 %d 条（%s）。"
        % (len(t3), t3_ok, s["t3_rate"]),
        "",
        "## 二、切分三段的逐段表现",
        "",
        "| 段 | 目标 | 请求 | 有代码 | 可编译 | 可用 | 平均字符 | 平均墙钟 |",
        "|----|------|------|--------|--------|------|----------|----------|",
    ]
    seg_name = {1: "契约骨架", 2: "按骨架填实", 3: "补 main 拼装"}
    for st in (1, 2, 3):
        d = seg[st]
        lines.append("| %d | %s | %d | %d | %d | %d | %s | %ss |"
                     % (st, seg_name[st], d["n"], d["code"], d["comp"], d["usable"],
                        d["chars"], d["secs"]))
    lines += [
        "",
        "## 三、失败归因",
        "",
        "| 臂 | " + " | ".join(sorted(set(list(s["A_reasons"]) + list(s["B_reasons"])))) + " |",
        "|----|" + "----|" * len(set(list(s["A_reasons"]) + list(s["B_reasons"]))),
        "| A | " + " | ".join(str(s["A_reasons"].get(k, 0))
                              for k in sorted(set(list(s["A_reasons"]) + list(s["B_reasons"])))) + " |",
        "| B | " + " | ".join(str(s["B_reasons"].get(k, 0))
                              for k in sorted(set(list(s["A_reasons"]) + list(s["B_reasons"])))) + " |",
        "",
        "## 四、怎么读这张表",
        "",
        "1. **别只看可用率**。B 臂贵三倍，若可用率提升不到 2 倍，就该老实说"
        "「切分不划算」——成本也是结论的一部分。",
        "2. **半截救回（partial）单独看**：段3 没拼成但段2 已经能 import，"
        "说明切分方向对、只是最后一步还差火候，那是可以继续调的空间，"
        "不能和「完全没用」混成一个数字。",
        "3. **段1 通过率是关键闸门**：如果段1（只出骨架）都交不出两个函数签名，"
        "那问题不在切分粒度，而在这个模型连「描述结构」都做不到，"
        "后续两段自然全塌——这时该换模型，不是继续切。",
        "",
        "原始逐次样本见 `split-ab.jsonl`。",
    ]
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(s, ensure_ascii=False, indent=2))
    print("报告：%s" % OUT_MD)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default=None)
    ap.add_argument("--port", type=int, default=8193)
    ap.add_argument("--reps", type=int, default=3, help="每个靶子每臂重复次数")
    ap.add_argument("--only", default="", help="只跑名称含该子串的项目（调试用）")
    ap.add_argument("--llm-base", default="os.environ")
    ap.add_argument("--model", default=None)
    ap.add_argument("--task-timeout", type=int, default=600)
    ap.add_argument("--max-tokens", type=int, default=2400)
    ap.add_argument("--tag", default="split-ab")
    ap.add_argument("--holder", action="store_true",
                    help="靶子换成 camp-g 里真实产出占位符的那 21 条失败样本")
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()

    # holder 模式下输出另立一套文件，别把"从头跑"和"救火跑"两份数据混在一个账本里
    global OUT_JSONL, OUT_MD, HOLDER_MODE
    HOLDER_MODE = bool(args.holder)
    if HOLDER_MODE:
        OUT_JSONL = EVID / "split-ab-holder.jsonl"
        OUT_MD = EVID / "split-ab-holder.md"
        args.reps = 1          # 每条失败样本就是一条，不重复跑
        args.tag = "split-holder"

    if args.report_only:
        return report()

    import os
    llm_base = (os.environ.get("SA_LLM_BASE", "http://127.0.0.1:11434/v1")
                if args.llm_base == "os.environ" else args.llm_base)
    model = args.model or os.environ.get("SA_LLM_MODEL", "minicpm5-2b:f16")

    tg = targets(args.holder)
    if args.only:
        tg = [t for t in tg if args.only in t["project"]]
    if not tg:
        print("[split] 没有靶子，检查 is_whole_package 判据或 camp-g 记录")
        return 2

    if not soak.probe_model_ready(llm_base, model):
        print("[split] 模型不可用：确认 ollama 已起且 %s 存在" % model)
        return 3

    exe = soak.resolve_exe(args.exe)
    data_dir = pathlib.Path(tempfile.mkdtemp(prefix="sa_split_"))
    runner_dir = soak.prepare_data(data_dir, keep=True)
    env_extra = {"SA_LLM_BASE": llm_base, "SA_LLM_MODEL": model,
                 "SA_LLM_TIMEOUT": "300", "SA_LLM_MAX_TOKENS": str(args.max_tokens)}

    print("[split] exe=%s" % exe)
    print("[split] model=%s base=%s" % (model, llm_base))
    print("[split] 靶子 %d 个 × 每臂 %d 次 = A %d 次 + B 最多 %d 次请求"
          % (len(tg), args.reps, len(tg) * args.reps, len(tg) * args.reps * 3))
    for t in tg:
        print("        - %s（%s）" % (t["project"], t["lang"]))

    proc = soak.launch(exe, args.port, data_dir, runner_dir, env_extra)
    base = "http://127.0.0.1:%d" % args.port
    t_start = time.time()
    try:
        if not soak.wait_ready(base, budget=120.0):
            print("[split] 服务没起来，看 %s" % (data_dir / "server.log"))
            return 2
        users = ["split_p0", "split_p1"]
        for u in users:
            soak.ensure_account(base, u)

        n = 0
        total = len(tg) * args.reps * 2
        for t in tg:
            for rep in range(args.reps):
                for arm in ("A", "B"):
                    # 账号轮转：每个账号自带配额，别把限流误记成功能缺陷
                    soak.set_active(users[n % len(users)])
                    n += 1
                    t0 = time.time()
                    if arm == "A":
                        final, _ = run_arm_a(base, t, rep, args.tag, args.task_timeout)
                        rec = {"arm": "A", "target": t["project"], "lang": t["lang"],
                               "rep": rep, "path": "single", "verdict": None,
                               "steps": [final], "final": final,
                               "elapsed": round(time.time() - t0, 1)}
                    else:
                        steps, j2, j3, path, el = run_arm_b(base, t, rep, args.tag,
                                                            args.task_timeout)
                        rec = {"arm": "B", "target": t["project"], "lang": t["lang"],
                               "rep": rep, "path": path,
                               "verdict": SP.split_verdict(j2, j3),
                               "steps": steps, "final": steps[-1],
                               "elapsed": el}
                    with open(OUT_JSONL, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        fh.flush()
                    print("[%2d/%d] %-12s %s r%d %-10s code=%-5s comp=%-5s usable=%-5s %.0fs(总 %.0fs)"
                          % (n, total, t["project"], arm, rep, rec["path"],
                             rec["final"]["code_chars"] or "-",
                             rec["final"]["compiles"], rec["final"]["usable"],
                             rec["elapsed"], time.time() - t_start), flush=True)
    finally:
        soak.stop(proc)

    return report()


if __name__ == "__main__":
    sys.exit(main())
