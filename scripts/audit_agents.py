#!/usr/bin/env python3
"""agent 花名册审计：把"该不该单独立册"从拍脑袋变成算出来的结论。

为什么要这个脚本：
    扩容时最容易犯的错是"复制粘贴改名"——加一个 task_types 与别人完全一样的 agent，
    路由胜负就由表序决定，从此路由结果不可解释。收缩时反过来，凭感觉删会误伤。
    判据必须能机械跑，否则每轮都要重新吵一遍。

审计项（对应 docs/86 的 A1~A4，外加两条自查）：
    A1f 形式：task_types 组合是否是他人组合的子集（完全相同也算子集）
    A1s 实质：keywords 与同 task_type 的他人是否有交集（撞车＝一句话有两个主人）
    A2/A3  产出物与判据：doc_refs 是否存在、是否被多个 agent 共用（共用＝没有独立知识库）
    A4     独立知识库：docs/kb/ 下是否有只属于它的文件
    S1     空壳自查：vk / skills / keywords 是否齐备
    S2     权限自查：只读画像 vs 声明的沙箱是否自相矛盾

用法：
    python scripts/audit_agents.py            # 表格 + 结论
    python scripts/audit_agents.py --json     # 机器可读
    python scripts/audit_agents.py --vacant   # 只看还空着的 task_types 组合（扩容选域用）
退出码：0 = 无"应裁撤"结论；1 = 有 agent 命中硬性不合格（提示归并，不等于脚本跑错）。
注意：本脚本只读源码，不改任何东西，也**不会**替你删 agent——删是人的决定，脚本只给依据。
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_RS = os.path.join(ROOT, "src", "domain", "agent.rs")

TASK_TYPES = ["Coding", "Writing", "Research", "Planning", "Ideation", "Collaboration"]

HARD_FAIL = ["A1f", "A1s", "A2", "A4"]  # 命中任一条即建议归并（A3 无法机械化，靠人判断）


def _balanced_slice(text: str, start: int) -> str:
    """从 start（指向 'AgentSpec {' 的 '{'）开始按花括号配平切出一个完整结构体。"""
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]


def _str_list(block: str, field: str) -> list[str]:
    """取 `field: &[ ... ]` 里的字符串字面量（允许跨行、允许 const 引用）。"""
    m = re.search(rf"{field}:\s*&\[(.*?)\]", block, re.S)
    if not m:
        return []
    return re.findall(r'"([^"]*)"', m.group(1))


def _task_types(block: str) -> list[str]:
    m = re.search(r"task_types:\s*(ALL_TASK_TYPES|&\[[^\]]*\])", block, re.S)
    if not m:
        return []
    if m.group(1) == "ALL_TASK_TYPES":
        return list(TASK_TYPES)
    return re.findall(r"TaskType::(\w+)", m.group(1))


def _scalar(block: str, field: str) -> str:
    m = re.search(rf'{field}:\s*"([^"]*)"', block)
    return m.group(1) if m else ""


def load_agents() -> list[dict]:
    text = open(AGENT_RS, encoding="utf-8").read()
    # 只扫 AGENTS 常量内部，避开 AgentSpec 结构体定义与测试
    i = text.index("pub const AGENTS")
    j = text.index("\n];", i)
    body = text[i:j]
    agents = []
    for m in re.finditer(r"AgentSpec\s*\{", body):
        block = _balanced_slice(body, m.end() - 1)
        aid = _scalar(block, "id")
        if not aid:
            continue
        agents.append(
            {
                "id": aid,
                "name": _scalar(block, "name"),
                "domain": _scalar(block, "domain"),
                "task_types": _task_types(block),
                "keywords": _str_list(block, "keywords"),
                "vk": _str_list(block, "vertical_knowledge"),
                "doc_refs": _str_list(block, "doc_refs"),
                "skills": _str_list(block, "skills"),
                "sandbox": _scalar(block, "sandbox_profile"),
            }
        )
    return agents


def layer(a: dict) -> str:
    """分层：super=总控；combo=垂直域（多任务类型组合）；flat=通用单职能；guard=审核（不承接任务类型）。

    为什么必须分层再比 A1f：
        第一版判据只问"组合是不是别人的子集"，结果把通用层全判死了——`research-agent` 只有
        `[Research]`，而 `legal-agent` 是 `[Writing, Research]`，自然是它的子集；照此逻辑
        `planner`/`ideation` 也全得归并。可事实上垂直域**刻意**覆盖通用域的任务类型，
        这是"垂直优先"的设计意图，不是抢活。真正该比的是同一层内部：通用之间互不重合、
        垂直之间互不重合。判据的颗粒度错了，结论就会反过来把所有好东西删掉。
    """
    if a["id"] == "super-agent":
        return "super"
    if not a["task_types"]:
        return "guard"
    return "combo" if len(a["task_types"]) >= 2 else "flat"


def audit(agents: list[dict]) -> list[dict]:
    # doc_refs 被谁引用：共用＝没有独立知识库
    ref_owners: dict[str, list[str]] = {}
    for a in agents:
        for r in a["doc_refs"]:
            ref_owners.setdefault(r, []).append(a["id"])

    out = []
    for a in agents:
        flags, notes = [], []

        # A1f 形式重合：只在**同一层**内比（总控豁免：它刻意覆盖全集）
        if layer(a) in ("flat", "combo"):
            for b in agents:
                if b["id"] == a["id"] or layer(b) != layer(a):
                    continue
                if set(a["task_types"]) == set(b["task_types"]):
                    flags.append("A1f")
                    notes.append(f"task_types 与同层的 {b['id']} 完全相同")
                    break
                if set(a["task_types"]).issubset(set(b["task_types"])):
                    flags.append("A1f")
                    notes.append(f"task_types 是同层 {b['id']} 的子集")
                    break

        # A1s 实质撞车：同 task_type 内 keywords 交集
        for b in agents:
            if b["id"] == a["id"]:
                continue
            if not (set(a["task_types"]) & set(b["task_types"])):
                continue
            inter = set(k.lower() for k in a["keywords"]) & set(k.lower() for k in b["keywords"])
            if inter:
                flags.append("A1s")
                notes.append(f"与 {b['id']} 关键词撞车：{'/'.join(sorted(inter))}")

        # A2/A4 独立知识库（总控豁免：它的产出是调度结构，不是领域文档，逼它建 kb 只会逼出填充物）
        if layer(a) == "super":
            pass
        elif not a["doc_refs"]:
            flags.append("A2")
            notes.append("没有 doc_refs（无可评审的独立产出依据）")
        else:
            for r in a["doc_refs"]:
                p = os.path.join(ROOT, r.replace("/", os.sep))
                if not os.path.exists(p):
                    flags.append("A2")
                    notes.append(f"doc_refs 指向不存在的文件：{r}")
            own_kb = [r for r in a["doc_refs"] if r.startswith("docs/kb/") and ref_owners.get(r) == [a["id"]]]
            if not own_kb:
                flags.append("A4")
                notes.append("docs/kb/ 下无专属文件（借用他人知识库或未建立）")

        # S1 空壳
        if not a["task_types"] and a["id"] != "guard-agent":
            flags.append("S1")
            notes.append("没有 task_types，只能靠关键词被叫到")
        for f in ("vk", "keywords", "skills"):
            if not a[f]:
                flags.append("S1")
                notes.append(f"{f} 为空")

        # S2 画像自相矛盾
        if "readonly" in a["sandbox"] and a["id"] not in ("guard-agent",):
            notes.append(f"沙箱 {a['sandbox']} 为只读画像（请确认是否刻意）")

        hard = [f for f in flags if f in HARD_FAIL]
        out.append({**a, "flags": sorted(set(flags)), "notes": notes, "verdict": "建议归并" if hard else "可立册"})
    return out


def vacant_combos(agents: list[dict]) -> list[tuple]:
    taken = {frozenset(a["task_types"]) for a in agents if a["id"] != "super-agent"}
    free = []
    for n in (2, 3):
        for combo in itertools.combinations(TASK_TYPES, n):
            if frozenset(combo) not in taken:
                free.append(combo)
    return free


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--vacant", action="store_true")
    args = ap.parse_args()

    agents = load_agents()
    rows = audit(agents)

    if args.vacant:
        print("尚未占用的 task_types 组合（扩容选域时优先从这里挑，天然过 A1f）：")
        for combo in vacant_combos(rows):
            print("   ", " + ".join(combo))
        return 0

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0 if all(r["verdict"] == "可立册" for r in rows) else 1

    print(f"花名册共 {len(rows)} 个 agent\n")
    w = max(len(r["id"]) for r in rows) + 1
    for r in rows:
        mark = "  " if r["verdict"] == "可立册" else "!!"
        print(f"{mark} {r['id']:<{w}} {r['verdict']:<8} [{layer(r):<5}] types={len(r['task_types'])} kw={len(r['keywords']):<2} "
              f"vk={len(r['vk'])} kb={sum(1 for x in r['doc_refs'] if x.startswith('docs/kb/'))} {r['sandbox']}")
        for n in r["notes"]:
            print(f"     - {n}")

    bad = [r for r in rows if r["verdict"] != "可立册"]
    print()
    if bad:
        print(f"命中硬性不合格 {len(bad)} 个：" + "、".join(r["id"] for r in bad))
        print("（A1f/A1s/A2/A4 任一命中即建议归并；A3 独立判据无法机械判定，需人复核其 summary 是否写了明确的「不做」）")
        return 1
    print("全部通过形式审计（A3 仍需人复核：「这个域做对和做错能不能分开」脚本判不了）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
