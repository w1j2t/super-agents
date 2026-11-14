#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ideation-agent 执行脚本：按 SCAMPER 七维对一个主题做定向发散。

刻意不用随机词：随机发散产出的大多是噪声。SCAMPER 是七个"有方向的提问"，
每个方向都能落成具体动作，评审时可以说"这维不行"，而不是"感觉不对"。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _common as C  # noqa: E402

SCAMPER = [
    ("S 替代", "把这个环节换成更便宜/更快/更稳的另一种做法"),
    ("C 组合", "和另一个能力拼起来，能不能出现新用法"),
    ("A 借鉴", "其他行业解决同类问题时用的招，能不能搬过来"),
    ("M 放大", "把某个参数放大十倍，哪里先坏（暴露真实瓶颈）"),
    ("M 缩小", "砍到只剩一件事，用户还愿意用吗"),
    ("P 他用", "原本给 A 场景做的东西，B 场景能不能直接用"),
    ("E 反转", "把流程倒过来跑，或让被动方变成发起方"),
]


def work():
    g = C.goal()
    kw = C.keywords(g, 5)
    topic = C.topic(g, 24)
    ideas = [{"dimension": d, "prompt": f"围绕「{topic}」：{p}"} for d, p in SCAMPER]
    payload = {"ok": True, "topic": topic, "ideas": ideas,
               "screen_rule": "每条先问三件事：用户是否真的痛、能不能一周内做出最小版、失败代价多大"}
    summary = f"围绕「{topic}」给出 {len(ideas)} 个定向创意维度，每条都带可执行提问。"
    return summary, payload


if __name__ == "__main__":
    C.main(work)
