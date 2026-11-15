#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_codegen_modes.py —— 小模型代码生成"约束方式"对比实验

## 为什么要先做这个实验再改 agent

第一版改造里，我给 2B 模型套了 json_schema 强约束，结果它把 `code` 字段填成了
字面量 `...` —— 三个点，`compile("...")` 还是合法的（Python 的 Ellipsis），
于是"语法检查通过"。这是假绿的第二种形态：结构合法、内容空洞。

所以不能凭直觉选约束方式，要拿数据说话。本脚本对同一批任务跑两种模式：

    A schema  —— response_format=json_schema，字段强约束（结构最稳）
    B free    —— 不加 schema，让它自由写代码，事后用``` 代码块抽取
    C free+   —— 自由写，但提示词里明确"从第一行代码开始，不要解释"

比较四个量：拿到代码的字符数、completion_tokens、语法是否通过、是否只是占位符。

判定"占位符"不能靠肉眼：用 `substantive()` 查——必须同时有函数/结构定义、
有实质语句、且长度过线。**三个点、pass、TODO 都算没写**。

用法：
    python scripts/probe_codegen_modes.py --runs 3
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "workers"))

import _llm as L  # noqa: E402

TASKS = [
    ("py-csv", "写一个 Python 脚本：把多行 JSON 日志按小时聚合成统计表，输出 CSV。要可直接运行，含 main。"),
    ("py-test", "为日志聚合脚本写 pytest 测试代码：空文件、坏行跳过、时区跨越、字段缺失。"),
    ("rs-struct", "用 Rust 定义存档系统的数据结构：角色属性、背包、任务进度、场景位置，要求可版本升级。给出完整结构体定义。"),
]

SCHEMA = {
    "type": "object",
    "properties": {
        "language": {"type": "string", "enum": ["python", "rust"]},
        "filename": {"type": "string"},
        "code": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": ["language", "filename", "code", "notes"],
    "additionalProperties": False,
}

# A：schema 模式（沿用第一版）
P_SCHEMA_SYS = ("你是代码生成器。只输出一个 JSON 对象，不要任何解释、不要 Markdown 包裹、"
                "不要输出 JSON 以外的任何字符。")
P_SCHEMA = ("任务：{goal}\n\n要求：\n1. 写出完整可用实现，不要伪代码，不要 TODO。\n"
            "2. 只输出 JSON，字段 language/filename/code/notes，code 是完整代码正文。\n")

# B：自由模式（不加约束，事后抽代码块）
P_FREE_SYS = "你是资深程序员。直接写出可以运行的代码。"
P_FREE = "任务：{goal}\n\n请写出完整的代码实现。"

# C：自由 + 明确"从第一行开始"
P_FREE2_SYS = ("你是资深程序员。只输出代码本身，不要解释、不要客套、不要分步骤讲思路。"
               "从第一行代码开始写，写完即止。")
P_FREE2 = ("任务：{goal}\n\n"
           "要求：完整可运行、字段写全、不要占位符、不要 TODO。"
           "用 ``` 包住代码。")

CODE_RE = re.compile(r"```(python|py|rust|rs)?\s*\n(.*?)```", re.S | re.I)
PLACEHOLDER = {"...", "…", "pass", "todo", "tbd", "<complete code>", "<完整代码>", "code"}


def extract(text: str, lang: str) -> str:
    """抽最长代码块；没有代码块就把全文当代码（自由模式下模型常常直接吐代码）。"""
    blocks = [(m.group(1) or "", m.group(2)) for m in CODE_RE.finditer(text or "")]
    if blocks:
        blocks.sort(key=lambda x: len(x[1]), reverse=True)
        return blocks[0][1].strip()
    return (text or "").strip()


def substantive(code: str, lang: str) -> tuple[bool, str]:
    """判"是不是真写了东西"。占位符、空壳、只有 import 的都算没写。"""
    c = (code or "").strip()
    if not c:
        return False, "空"
    if c.lower().strip("` \n.") in PLACEHOLDER:
        return False, "占位符 %r" % c[:40]
    if len(c) < 80:
        return False, "过短 %d 字符" % len(c)
    has_def = bool(re.search(r"^\s*(def |class |fn |struct |impl |enum )", c, re.M))
    if not has_def:
        return False, "无函数/结构定义"
    # 去掉注释与空行后，实质语句行数
    body = [ln for ln in c.splitlines()
            if ln.strip() and not ln.strip().startswith(("#", "//"))]
    if len(body) < 6:
        return False, "实质行数 %d" % len(body)
    return True, "ok(%d 行)" % len(body)


def syntax(code: str, lang: str) -> tuple[bool | None, str]:
    if lang == "py":
        try:
            compile(code, "<probe>", "exec")
            return True, "ok"
        except SyntaxError as exc:
            return False, "%s:L%s" % (exc.msg, exc.lineno)
        except Exception as exc:                    # noqa: BLE001
            return False, type(exc).__name__
    return None, "skip(rust 由主脚本查)"


def run_one(mode: str, goal: str, lang: str):
    if mode == "A-schema":
        res = L.chat(P_SCHEMA.format(goal=goal), system=P_SCHEMA_SYS,
                     max_tokens=3200, temperature=0.2, schema=SCHEMA)
        raw = res.get("content") or ""
        obj, why = L.parse_json_object(raw) if res.get("ok") else (None, res.get("error"))
        code = (obj or {}).get("code") or ""
    elif mode == "B-free":
        res = L.chat(P_FREE.format(goal=goal), system=P_FREE_SYS,
                     max_tokens=3200, temperature=0.2)
        code = extract(res.get("content") or "", lang)
    else:
        res = L.chat(P_FREE2.format(goal=goal), system=P_FREE2_SYS,
                     max_tokens=3200, temperature=0.2)
        code = extract(res.get("content") or "", lang)

    ok, why_sub = substantive(code, lang)
    syn, syn_d = syntax(code, lang) if lang == "py" else (None, "skip")
    return {
        "mode": mode,
        "code_chars": len(code),
        "ok_call": res.get("ok"),
        "finish": res.get("finish"),
        "completion_tokens": res.get("completion_tokens"),
        "reasoning_chars": res.get("reasoning_chars"),
        "elapsed_ms": res.get("elapsed_ms"),
        "substantive": ok,
        "substantive_why": why_sub,
        "syntax": syn,
        "syntax_detail": syn_d,
        "head": code[:80].replace("\n", "\\n"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--out", default=str(ROOT / "docs" / "evidence" / "soak" / "codegen-modes.jsonl"))
    args = ap.parse_args()

    if not L.enabled():
        print("需先设置 SA_LLM_BASE / SA_LLM_MODEL")
        return 3
    print("model=%s" % L.describe().get("model"))

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    with open(out, "a", encoding="utf-8") as fh:
        for run in range(args.runs):
            for tid, goal in TASKS:
                lang = "rust" if tid.startswith("rs") else "py"
                for mode in ("A-schema", "B-free", "C-free+"):
                    r = run_one(mode, goal, lang)
                    r.update({"task": tid, "run": run + 1, "lang": lang})
                    rows.append(r)
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                    fh.flush()
                    print("%-10s %-9s chars=%-5d tok=%-5s subst=%-5s syn=%-5s %s"
                          % (tid, mode, r["code_chars"], r["completion_tokens"],
                             r["substantive"], r["syntax"], r["substantive_why"]), flush=True)

    print("\n=== 按模式汇总 ===")
    for mode in ("A-schema", "B-free", "C-free+"):
        sub = [r for r in rows if r["mode"] == mode]
        if not sub:
            continue
        print("%-9s 实质产出 %d/%d  平均字符 %.0f  平均 tokens %.0f"
              % (mode, sum(1 for r in sub if r["substantive"]), len(sub),
                 sum(r["code_chars"] for r in sub) / len(sub),
                 sum(r["completion_tokens"] or 0 for r in sub) / len(sub)))
    print("明细：%s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
