#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""research-agent 执行脚本：把目标转成可执行的检索问题清单与取证要求。

重点是"可被证伪"：每条问题都要写清什么样的答案算答上了、
什么样的来源不能用。避免检索任务写成"随便搜搜然后摘抄"。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _common as C  # noqa: E402

BAD_SOURCES = ["无署名聚合站", "AI 生成的内容农场", "无日期的二手转述", "论坛单一用户口述"]


def work():
    g = C.goal()
    kw = C.keywords(g, 6) or ["主题"]
    base = kw[0]
    questions = [
        f"{base} 的官方定义与最新口径是什么（含生效日期）",
        f"{base} 的主流实现/方案有哪几类，各自代价与适用边界",
        f"{base} 常见失败模式与踩坑记录（要具体报错或案例）",
        f"{base} 相关数据的最新数值、单位与统计口径",
    ]
    payload = {
        "ok": True,
        "keywords": kw,
        "questions": [
            {"no": i, "question": q, "evidence_required": "官方文档 / 一手数据 / 可复现实验"}
            for i, q in enumerate(questions, 1)
        ],
        "reject_sources": BAD_SOURCES,
        "rule": "查不到就写「未能从可靠来源确认」，不得用记忆补全数字与日期",
    }
    summary = f"已生成 {len(questions)} 条可证伪检索问题，并附取证与排除来源要求。"
    return summary, payload


if __name__ == "__main__":
    C.main(work)
