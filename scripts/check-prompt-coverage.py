#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check-prompt-coverage.py —— 门禁 12：首个提示词 → 六部分文档的逐条覆盖对齐。

为什么需要这一道：
  其余门禁证的是"代码能跑、文档没漂移"，但证不了**"老板当初要的，是否真的都落到了文档里"**。
  一份漂移为零、跑得飞快的实现，也可能整段漏掉"防幻觉"或"上下文策略"——因为没人逐字对过。
  本道门禁把首提示词拆成可检索的术语清单，逐条去对应文档里找出处；找不到即红。

它检查什么（全部只读，不改任何文件）：
  A. 需求基线 docs/00-prompt-original.md 存在且信息量达标（>500 字）
  B. 六部分各自文档存在，且原文点名术语必须能在该文档中找到
  C. 能力清单：源码里每一个 CapabilitySpec.id 都必须出现在 docs/02（生成物漏项即红）
  D. 敏捷锚点：01 的公理表 A1~A6 与文档导航表仍在

设计原则：
  - **只做"有没有"，不做"写得好不好"**。文风是人的事，遗漏是机器的事。
  - 术语用宽松包含匹配（原文是中文自然语言，硬匹配标点会假红），但每个术语都打印命中行号，
    人一眼能判断"是真讲清楚了，还是蹭了个词"。

用法：python scripts/check-prompt-coverage.py [--verbose]
退出码：0 = 全覆盖；1 = 有遗漏（逐条列出文件与术语）
"""

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SRC = ROOT / "src"

# 原文（首个提示词）逐部分点名术语 → 必须出现的文档
# 术语写的是"老板的原话"，不是我们后来起的名字；这样才不会自己跟自己对答案。
REQUIRED = [
    (
        "第 1 部分 总体定位",
        "01-overview.md",
        ["总体定位", "全自动化", "安全化", "正确化", "全可视化", "可控化", "全面化", "自进化", "规划"],
    ),
    (
        "第 2 部分 能力清单",
        "02-capability-catalog.md",
        ["能力清单", "触发条件", "权限边界", "可观测信号", "降级", "原文 25 条", "逐条对齐"],
    ),
    (
        "第 3 部分 分工协作",
        "03-division-of-labor.md",
        ["定义职责", "定义编排", "按任务模型调度", "按任务 agents 调配", "权力分配"],
    ),
    (
        "第 4 部分 交互规范",
        "04-interaction-spec.md",
        ["统一交互风格", "对话共享记忆", "上下文策略"],
    ),
    (
        "第 5 部分 边界处理",
        "05-boundary-handling.md",
        ["信息不足", "超出能力", "幻觉", "合规", "循环报错", "安全降级", "交互风格"],
    ),
    (
        "第 6 部分 验收标准",
        "06-acceptance-criteria.md",
        ["正确性", "可观测性", "可控性", "成本", "自净化", "验收口径"],
    ),
]

# 源码里每个能力 id 都必须在 docs/02 出现（生成物漏项检查）
CAP_SRC = SRC / "domain" / "capability.rs"


def read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def find_line(text: str, term: str) -> int:
    """返回术语首次出现的行号（1-based），找不到返回 0。"""
    idx = text.find(term)
    if idx < 0:
        return 0
    return text.count("\n", 0, idx) + 1


def capability_ids() -> list:
    if not CAP_SRC.exists():
        return []
    text = read(CAP_SRC)
    return re.findall(r'id:\s*"([a-z0-9_]+)"', text)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true", help="打印每个术语的命中行号")
    args = ap.parse_args()

    fails = []
    rows = []

    # ---- A. 需求基线 ----
    baseline = DOCS / "00-prompt-original.md"
    if not baseline.exists():
        fails.append("缺少需求基线 docs/00-prompt-original.md（没有基线就谈不上覆盖）")
    else:
        n = len(read(baseline))
        if n < 500:
            fails.append("docs/00-prompt-original.md 过短（%d 字），疑似没把首提示词冻结完整" % n)
        rows.append(("需求基线", "docs/00-prompt-original.md", "%d 字" % n, "OK" if n >= 500 else "FAIL"))

    # ---- B. 六部分逐条术语 ----
    for part, fname, terms in REQUIRED:
        p = DOCS / fname
        if not p.exists():
            fails.append("%s：找不到文档 docs/%s" % (part, fname))
            rows.append((part, fname, "-", "MISSING"))
            continue
        text = read(p)
        missing = []
        for t in terms:
            ln = find_line(text, t)
            if ln == 0:
                missing.append(t)
            elif args.verbose:
                print("  [hit] %s :: %s @ line %d" % (fname, t, ln))
        if missing:
            fails.append("%s：docs/%s 缺术语 %s" % (part, fname, "、".join(missing)))
            rows.append((part, fname, "%d/%d" % (len(terms) - len(missing), len(terms)), "FAIL"))
        else:
            rows.append((part, fname, "%d/%d" % (len(terms), len(terms)), "OK"))

    # ---- C. 能力 id 全覆盖 ----
    ids = capability_ids()
    cat = DOCS / "02-capability-catalog.md"
    if ids and cat.exists():
        ctext = read(cat)
        lost = [i for i in ids if i not in ctext]
        if lost:
            fails.append("能力清单未覆盖源码中的 %d 个能力 id：%s（重跑 gen-spec-docs.py）"
                         % (len(lost), "、".join(lost)))
            rows.append(("能力 id 覆盖", "02-capability-catalog.md", "%d/%d" % (len(ids) - len(lost), len(ids)), "FAIL"))
        else:
            rows.append(("能力 id 覆盖", "02-capability-catalog.md", "%d/%d" % (len(ids), len(ids)), "OK"))
    elif not ids:
        fails.append("无法从 src/domain/capability.rs 提取能力 id（路径或写法变了）")

    # ---- D. 敏捷锚点：设计公理与文档导航仍在 ----
    ov = DOCS / "01-overview.md"
    if ov.exists():
        otext = read(ov)
        lost = [a for a in ("A1", "A2", "A3", "A4", "A5", "A6") if a not in otext]
        if lost:
            fails.append("01-overview.md 丢失设计公理锚点：%s" % "、".join(lost))
            rows.append(("设计公理 A1~A6", "01-overview.md", "%d/6" % (6 - len(lost)), "FAIL"))
        else:
            rows.append(("设计公理 A1~A6", "01-overview.md", "6/6", "OK"))

    # ---- 收口 ----
    print("首个提示词覆盖对齐（门禁 12）")
    print("-" * 72)
    for part, fname, ratio, verdict in rows:
        print("  [%-7s] %-26s %-28s %s" % (verdict, part, fname, ratio))
    print("-" * 72)
    if fails:
        print("结论：未通过（%d 处）" % len(fails))
        for f in fails:
            print("  × %s" % f)
        return 1
    print("结论：通过 —— 六部分文档对首个提示词逐条有落点，能力 id 全覆盖，设计公理锚点在位")
    return 0


if __name__ == "__main__":
    sys.exit(main())
