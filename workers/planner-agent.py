#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""planner-agent 执行脚本：把目标拆成 WBS（阶段 + 里程碑 + 依赖）。

规则：按句读切子任务；每个子任务给出一个可验收的"完成判据"。
明确区分"能在本机验证的"与"需要人拍板的"，避免计划里混进无法验证的项。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _common as C  # noqa: E402

BUILD_VERBS = ("写", "做", "实现", "搭", "生成", "开发", "设计", "建")
CHECK_VERBS = ("测", "验", "检查", "核对", "跑", "审计", "评审")
DECIDE_VERBS = ("选", "定", "确认", "拍板", "决定")


def phase_of(sentence: str) -> str:
    if any(v in sentence for v in DECIDE_VERBS):
        return "决策"
    if any(v in sentence for v in CHECK_VERBS):
        return "验证"
    if any(v in sentence for v in BUILD_VERBS):
        return "构建"
    return "澄清"


def work():
    g = C.goal()
    sents = C.split_sentences(g, by_comma=True) or [g]
    items = []
    for i, s in enumerate(sents[:12], 1):
        ph = phase_of(s)
        items.append(
            {
                "no": i,
                "phase": ph,
                "task": C.clip(s, 80),
                "done_when": {
                    "构建": "产出物落盘，且能用一条命令复现",
                    "验证": "有可粘贴的运行输出，0 失败",
                    "决策": "有明确结论与放弃项记录",
                    "澄清": "问题已向需求方确认并记录答案",
                }[ph],
            }
        )
    auto = sum(1 for x in items if x["phase"] != "决策")
    payload = {"ok": True, "steps": len(items), "auto_verifiable": auto, "items": items}
    summary = (
        f"已拆出 {len(items)} 个子任务，其中 {auto} 项可自动验收、"
        f"{len(items)-auto} 项需要人拍板。"
    )
    return summary, payload


if __name__ == "__main__":
    C.main(work)
