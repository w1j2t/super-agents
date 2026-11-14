#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""super-agent 执行脚本：把一句目标拆成「目标 / 约束 / 交付物 / 验收」四段。

这是确定性规则版实现，不是大模型。价值在于：把用户的自然语言目标
压成可被后续步骤引用的结构化骨架，且结果**可复现**（同输入必同输出），
方便回归测试。接入模型后端后，这个骨架仍可作为 prompt 的插槽。
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _common as C  # noqa: E402

CONSTRAINT_HINTS = ("不要", "不能", "必须", "只能", "不超过", "禁止", "限制", "预算", "期限")
DELIVER_HINTS = (
    "报告", "文档", "代码", "脚本", "表格", "方案", "清单", "ppt", "图", "页面", "接口",
    "工具", "摘要", "总结", "提纲", "流程", "规范", "手册", "模板", "邮件",
)


def work():
    g = C.goal()
    if not g:
        return "目标为空：没有可拆解的内容。", {"ok": False, "reason": "empty_goal"}

    sents = C.split_sentences(g)
    constraints = [s for s in sents if any(h in s for h in CONSTRAINT_HINTS)]
    delivers = [s for s in sents if any(h in s.lower() for h in DELIVER_HINTS)]
    kw = C.keywords(g, 8)

    payload = {
        "ok": True,
        "goal_chars": len(g),
        "sentences": len(sents),
        "keywords": kw,
        "constraints": constraints[:5],
        "deliverables": delivers[:5],
        "task_type": C.task_type(),
    }
    print_summary = (
        f"目标解析完成：{len(sents)} 个句子、{len(kw)} 个关键词；"
        f"识别约束 {len(constraints)} 条、交付物线索 {len(delivers)} 条。"
    )
    return print_summary, payload


if __name__ == "__main__":
    C.main(work)
