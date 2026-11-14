#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""data-agent 执行脚本：把"看下这个指标 / 分析为什么跌"的目标，落成口径卡与归因树。

数据分析最常见的错不是算错，而是**两个数根本不是一回事**：一个按自然日一个按财务日、
一个去重一个不去重、一个含试用一个不含。所以先逼出指标三要素（定义式 / 口径 / 数据源），
再给归因维度树与数据质量六查，最后列出必须先去确认的问题。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _common as C  # noqa: E402

# 指标定义三要素：缺任何一个，两个部门的口径必然打架。
DEFINITION = [
    ("定义式", "分子是什么、分母是什么、按什么粒度聚合（用户/订单/会话）"),
    ("口径", "时间窗（自然日/滚动7日/财务月）、人群（新老/含试用）、去重规则（日去重还是周期去重）"),
    ("数据源", "埋点 / 业务库 / 日志 / 第三方回传；同一指标多源时必须声明以哪个为准"),
]

# 归因维度树：从上往下逐层拆，每层只回答一个问题，避免"一上来就看明细"。
ATTRIBUTION_TREE = [
    ("时间", "是趋势性下滑还是某一天断崖？断崖优先查发布与数据管道"),
    ("渠道", "各渠道同比例下滑＝大盘问题；单渠道下滑＝渠道侧问题"),
    ("版本/机型", "按版本与设备型号切，定位是否为兼容性或灰度问题"),
    ("人群", "新老用户分开看：新用户跌是获客，老用户跌是产品"),
    ("漏斗节点", "把转化拆成曝光→点击→进入→完成，找掉得最狠的那一格"),
]

# 数据质量六查：归因之前先做，否则可能是在解释一个假象。
QUALITY_CHECKS = [
    ("缺失", "关键字段空值率是否突增（埋点漏报通常先表现为空值）"),
    ("重复", "是否存在重复上报（版更后常见双发）"),
    ("口径漂移", "上游是否改过统计逻辑或时区（改口径不改历史数据会长出假拐点）"),
    ("时区", "统计日期是否统一按同一时区切分"),
    ("单位", "元/千元、秒/毫秒、百分比/千分比是否一致"),
    ("埋点丢失", "事件量是否同步下跌（事件量跌＝采集问题，不是业务问题）"),
]

# 结论强度：数据类结论必须标注可信度，不能把相关性写成因果。
CONFIDENCE_RULES = [
    "只给相关性就说相关性，不说「导致」；因果需要对照或实验证据",
    "样本量不足或区间过短时，结论标注「不足以判定」",
    "任何百分比都要带基数（0.3% 可能是 3/1000，也可能是 30/10000）",
]


def work():
    g = C.goal()
    low = g.lower()
    kw = C.keywords(g, 8)
    topic = C.topic(g) or "未命名数据事项"

    metric_hits = [m for m in ("转化率", "留存", "活跃", "收入", "订单", "客单价",
                               "耗时", "延迟", "成功率", "成本", "毛利")
                   if m in low] or ["未识别（需先指明具体指标名）"]
    mode = "归因分析" if any(w in low for w in ("为什么", "下跌", "下滑", "异常", "波动", "归因")) else "口径与报表"

    asks = [s.strip() for s in C.split_sentences(g, by_comma=True) if len(s.strip()) >= 2][:6]

    payload = {
        "ok": True,
        "topic": topic,
        "keywords": kw,
        "mode": mode,
        "metrics": metric_hits,
        "definition_card": [{"element": e, "must_specify": d} for e, d in DEFINITION],
        "attribution_tree": [
            {"layer": i, "dimension": d, "question": q} for i, (d, q) in enumerate(ATTRIBUTION_TREE, 1)
        ],
        "quality_checks": [{"check": c, "signal": s} for c, s in QUALITY_CHECKS],
        "confidence_rules": CONFIDENCE_RULES,
        "pending_inputs": [{"no": i, "ask": a, "owner": "数据方/需求方"} for i, a in enumerate(asks, 1)],
        "rule": "口径未确认前不出结论；先做数据质量六查再谈归因，避免解释一个假拐点",
    }
    summary = (
        f"《{topic}》{mode}：指标 {len(metric_hits)} 个、口径三要素 {len(DEFINITION)} 项、"
        f"归因 {len(ATTRIBUTION_TREE)} 层、数据质量 {len(QUALITY_CHECKS)} 查、待确认输入 {len(asks)} 项。"
    )
    return summary, payload


if __name__ == "__main__":
    C.main(work)
