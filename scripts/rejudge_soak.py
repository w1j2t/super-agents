# -*- coding: utf-8 -*-
"""rejudge_soak.py —— 拿历史样本的 judge_evidence 重跑当前判据，量化"改判据改掉了多少"。

为什么必须有这个工具
--------------------
判分器本身也会写错，而且写错的后果比功能 bug 恶劣得多：功能错了看得见，判据错了
只是**数字悄悄偏**，然后所有基于数字的决策（要不要换模型、要不要改提示词、要不要
转前端）全跟着偏。

本台在 2026-11-16 就抓到两条系统性偏差，都被这一个工具量化出来了：

  ① Rust 编译检查漏了 `--crate-type lib`，rustc 按可执行程序编译、强制要 main，
     而任务大多是"写函数/结构体"的库形态 —— 合法库代码被 E0601 全判死。
     后果：Rust 通过率 2.0% 里混着一大块评测台自己制造的错误，"2B 完全不会
     Rust"这个结论被放大。
  ② `pass` / `todo!()` / `print('Hello World')` 是合法语法，编译必过，
     但它们没有任何实现 —— 只问"能编译吗"的判据会把它记成"编译通过"。

修完判据之后必须回答一个问题：**新旧读数差多少，差在哪几条**。没有这个答案，
报表就从"证据"退化成"我记得改过"。

用法
----
  python scripts/rejudge_soak.py                    # 扫全部批次
  python scripts/rejudge_soak.py --tag camp-g       # 只看某批
  python scripts/rejudge_soak.py --json             # 机器可读
  python scripts/rejudge_soak.py --write            # 落 docs/evidence/judge-replay.md

退出码：0 = 正常（无论是否有迁移）；2 = 没有任何可重放的样本。
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
EVID = ROOT / "docs" / "evidence" / "soak"
OUT_MD = ROOT / "docs" / "evidence" / "judge-replay.md"

sys.path.insert(0, str(HERE))
import soak_project as S  # noqa: E402  重放必须用**同一份**判据实现，不许另写一套


def load_samples(tag: str = ""):
    """读全部批次的样本行。返回 (tag, row) 列表，已去重。"""
    out, seen = [], set()
    pattern = "*.jsonl"
    for f in sorted(glob.glob(str(EVID / pattern))):
        t = pathlib.Path(f).stem
        if t.startswith("soak-"):
            t = t[len("soak-"):]
        # -retry / -decomp 是实验账，不混进主口径重放
        if t.endswith("-retry") or t.endswith("-decomp"):
            continue
        if tag and t != tag:
            continue
        for line in io.open(f, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            key = (t, row.get("round"))
            if key in seen:
                continue
            seen.add(key)
            out.append((t, row))
    return out


LANG_ALIAS = {"python": "py", "py": "py", "python3": "py", "py3": "py",
              "rust": "rs", "rs": "rs", "rslib": "rs"}


def norm_lang(raw):
    """把历史样本里五花八门的语言标签归一到 py / rs，并如实交代来源。

    为什么必须归一：同一个评测台，新批次写 `code_lang="py"`，老批次写
    `code_lang="python"` 或 `"rust"`。不归一会把同一门语言劈成两行统计
    （"py 630 条 90.5% / python 110 条 61.8%"其实就是同一件事的两半），
    让人误读成两种不同的能力。
    """
    if not raw:
        return "py", "default-py"
    k = str(raw).strip().lower()
    return LANG_ALIAS.get(k, k), "declared"


def replay_one(row):
    """对一行历史样本重跑判据，返回迁移记录；不可重放时返回 None。"""
    ev = row.get("judge_evidence") or {}
    code = ev.get("code_full") or ""
    if not code:
        return None
    # 证据存的是截断后的代码（头尾拼接 + 中略标记），拿它送编译器必然语法错，
    # 那种"错"是重放自己造的，不能拿来判案。这类样本只能标 skipped 等重跑。
    if ev.get("code_truncated"):
        return {"skipped": "代码被截断存证"}
    # 语言标签的真身在顶层 `code_lang`（历史批次还写成 python/rust 全名）。
    # 这里曾经只读 `ev.get("lang")`——而 `judge_evidence` **从来没有** lang 字段，
    # 于是每一行都落到 `or "py"`：全部 Rust 样本被送进 py_compile，
    # 必然语法错，被记成"仍然失败"。这是量具第三次翻车，前两次是 `...` 当实现、
    # GBK 吞子进程输出，形态一样：**错的不是被测系统，是读它的那只手**。
    raw_lang = ev.get("lang") or row.get("code_lang") or row.get("lang")
    lang, lang_src = norm_lang(raw_lang)
    if lang_src == "default-py" and not raw_lang:
        lang_src = "missing-lang"      # 缺字段要能被看见，不能悄悄当 py
    elif row.get("code_lang") and not ev.get("lang"):
        lang_src = "top-level"         # 从顶层补到的，另行标注
    old = row.get("compiles")
    old_src = row.get("compile_source") or ""

    raw_ok, cdetail = S.compile_check(lang, code)
    new, new_src = raw_ok, "local"
    reason = ""
    if S.is_placeholder(code):
        new, new_src, reason = None, "placeholder", "占位符"
    elif lang_src == "fallback-py":
        new, new_src, reason = None, "lang-guessed", "语言靠兜底猜"
    else:
        sub_ok, sub_why = S.is_substantive(code, lang)
        if raw_ok is True and not sub_ok:
            new, new_src, reason = None, "hollow", sub_why
    return {
        "tag": row.get("_tag"), "round": row.get("round"), "lang": lang,
        "old": old, "new": new, "old_src": old_src, "new_src": new_src,
        "raw_ok": raw_ok, "reason": reason, "detail": cdetail,
        "chars": len(code),
    }


def summarize(recs):
    """把迁移记录压成可读的矩阵。

    三张表回答三个不同的问题，缺一张就会把不同性质的变化混成一笔：
      A 旧 False -> 新 True   什么被我们误杀了（判据太严 / 前提错）
      B 旧 True  -> 新 None   什么被虚高了（语法合法但没内容）
      C 分布                   两套口径下 concluded / unverified 各多少
    """
    res = {"n": 0, "skipped": 0, "rescued": [], "hollowed": [], "still_fail": [],
           "still_ok": [], "old_unknown": [], "by_reason": {}}
    for r in recs:
        if r.get("skipped"):
            res["skipped"] += 1
            continue
        res["n"] += 1
        o, n = r["old"], r["new"]
        if o is False and n is True:
            res["rescued"].append(r)
        elif o is True and n is None:
            res["hollowed"].append(r)
            # 原因分布只统计"这一次真被剔除掉"的那批，不能统计所有带 reason 的记录。
            # 踩过：早期把 reason 无条件计数，于是 placeholder 记成 122 条，
            # 而表三里"虚高剔除"只有 71 条 —— 两张表自相矛盾，读者会以为报表算错了。
            # 差的 51 条其实是"旧判本来就没通过（old=None）"的占位符，
            # 它们从来没有被算进旧报表的通过数，因此谈不上"被剔除"。
            k = (r.get("reason") or "未注明").split(":")[0]
            res["by_reason"][k] = res["by_reason"].get(k, 0) + 1
        elif o is False and n is not True:
            res["still_fail"].append(r)
        elif o is True and n is True:
            res["still_ok"].append(r)
        else:
            # old=None：旧报表当年就没给出编译结论，这类既不算误杀也不算虚高。
            res["old_unknown"].append(r)
    return res


def old_pct(rows, key="compiles"):
    den = [r for r in rows if r.get(key) is not None]
    num = [r for r in den if r.get(key) is True]
    return (len(num), len(den), round(100.0 * len(num) / len(den), 1) if den else 0.0)


def render(res, recs, tag=""):
    lines = []
    a = lines.append
    a("# 判据重放报告（judge replay）")
    a("")
    a("> 由 `scripts/rejudge_soak.py` 生成。用**当前**判据重跑历史样本存下的代码，"
      "量化判据修复带来的读数变化。")
    a("")
    a("范围：%s ｜ 可重放 %d 条 ｜ 因代码截断存证跳过 %d 条"
      % (tag or "全部批次", res["n"], res["skipped"]))
    a("")
    a("## 一、误杀被纠正（旧判失败 → 新判通过）")
    a("")
    a("这批是**评测台自己判错的**：代码本身合法，是检查方式错了。")
    a("")
    if res["rescued"]:
        a("| 批次 | 轮次 | 语言 | 代码字符 | 旧错因（首行） |")
        a("| --- | --- | --- | --- | --- |")
        for r in res["rescued"][:40]:
            a("| %s | %s | %s | %d | %s |"
              % (r["tag"], r["round"], r["lang"], r["chars"],
                 (r["detail"] or "")[:60].replace("|", "/")))
        if len(res["rescued"]) > 40:
            a("")
            a("（另有 %d 条同类，未逐条列出）" % (len(res["rescued"]) - 40))
    else:
        a("无。")
    a("")
    a("## 二、虚高被剔除（旧判通过 → 新判无法计入）")
    a("")
    a("这批是**语法合法但没有实现**的：`pass` / `todo!()` / `...` / 玩具代码。")
    a("")
    if res["hollowed"]:
        a("| 批次 | 轮次 | 语言 | 代码字符 | 剔除原因 |")
        a("| --- | --- | --- | --- | --- |")
        for r in res["hollowed"][:40]:
            a("| %s | %s | %s | %d | %s |"
              % (r["tag"], r["round"], r["lang"], r["chars"],
                 (r["reason"] or "").replace("|", "/")))
        if len(res["hollowed"]) > 40:
            a("")
            a("（另有 %d 条同类，未逐条列出）" % (len(res["hollowed"]) - 40))
    else:
        a("无。")
    a("")
    a("## 三、按语言看修正量")
    a("")
    a("| 语言 | 可重放 | 误杀纠正 | 虚高剔除 | 仍失败 | 仍通过 | 旧也未判 |")
    a("| --- | --- | --- | --- | --- | --- | --- |")
    # 遍历**实际出现**的语言，而不是写死 ("py", "rs")：
    # 写死时若某行语言没被认出，它会从表里整行消失，六列之和校验也发现不了
    # （校验只管行内，管不了漏行）。这是"报告只列想看的那几行"的隐性失真。
    langs = sorted({r.get("lang") for r in recs if not r.get("skipped") and r.get("lang")})
    for lg in langs:
        sub = [r for r in recs if not r.get("skipped") and r.get("lang") == lg]
        if not sub:
            continue
        # 六列之和必须等于"可重放"——踩过：早先只有四列，py 行加起来 581 而非 602，
        # 差 21 条是 old=None（旧报表当年没给结论），没有列承接它们，读者只能自己猜。
        a("| %s | %d | %d | %d | %d | %d | %d |"
          % (lg, len(sub),
             sum(1 for r in sub if r["old"] is False and r["new"] is True),
             sum(1 for r in sub if r["old"] is True and r["new"] is None),
             sum(1 for r in sub if r["old"] is False and r["new"] is not True),
             sum(1 for r in sub if r["old"] is True and r["new"] is True),
             sum(1 for r in sub if r["old"] is None)))
    a("")
    a("## 四、虚高剔除的原因分布")
    a("")
    a("> 只统计上一节里**真被剔除**（旧判通过 → 新判无法计入）的那批，"
      "与表三「虚高剔除」列严格对齐；旧判本来就没通过的占位符不在其中。")
    if res["by_reason"]:
        for k, v in sorted(res["by_reason"].items(), key=lambda kv: -kv[1]):
            a("- %s：%d 条" % (k, v))
    else:
        a("- 无")
    a("")
    a("## 五、怎么读这份报告")
    a("")
    a("- 「误杀纠正」为正数说明**旧报表低估了模型**；「虚高剔除」为正数说明旧报表高估了。")
    a("- 两者可以同时存在且方向相反 —— 那不是自相矛盾，是两种错误同时发生。")
    a("- 本报告只重放**能拿到完整代码**的样本。代码被截断存证的样本（`code_truncated`）"
      "一律跳过，因为截断后再送编译器必然报错，那种错是重放自己造的，不能拿来判案。"
      "要覆盖它们只能重跑真实任务。")
    a("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="", help="只重放某一批次（如 camp-g）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--write", action="store_true", help="落 docs/evidence/judge-replay.md")
    args = ap.parse_args()

    samples = load_samples(args.tag)
    if not samples:
        print("没有样本可重放（%s）" % (EVID / "*.jsonl"))
        return 2
    recs = []
    for t, row in samples:
        row = dict(row)
        row["_tag"] = t
        r = replay_one(row)
        if r is None:
            continue
        r["tag"] = t
        recs.append(r)
    if not recs:
        print("样本里没有可用的 judge_evidence（老批次早于该字段）")
        return 2

    res = summarize(recs)
    if args.json:
        print(json.dumps({
            "n": res["n"], "skipped": res["skipped"],
            "rescued": len(res["rescued"]),
            "hollowed": len(res["hollowed"]),
            "still_fail": len(res["still_fail"]),
            "still_ok": len(res["still_ok"]),
            "by_reason": res["by_reason"],
        }, ensure_ascii=False, indent=2))
    else:
        print("可重放 %d 条 / 跳过 %d 条" % (res["n"], res["skipped"]))
        print("误杀纠正(旧False→新True): %d" % len(res["rescued"]))
        print("虚高剔除(旧True→新None) : %d" % len(res["hollowed"]))
        print("仍失败: %d ｜ 仍通过: %d" % (len(res["still_fail"]), len(res["still_ok"])))
        print("剔除原因: %s" % (res["by_reason"] or "无"))

    if args.write:
        OUT_MD.parent.mkdir(parents=True, exist_ok=True)
        OUT_MD.write_text(render(res, recs, args.tag), encoding="utf-8")
        print("报告：%s" % OUT_MD)
    return 0


if __name__ == "__main__":
    sys.exit(main())
