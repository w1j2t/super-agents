#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""finance-agent 执行脚本：把"报销 / 对账 / 预算"的目标，落成可核对的单据链与口径卡。

财务类任务出错几乎都不是"算错"，而是**口径没说清**：期间算哪天到哪天、含不含税、
按开票还是按付款确认。所以这里先逼出三个口径问题，再给五单一致的核对链，
最后列出必须由人确认的缺失项——不猜金额，不编数字。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _common as C  # noqa: E402

# 口径三问：任何一个没答，下面的数字都不具备可比性。
CALIBER = [
    ("期间", "确认区间是「费用发生日」还是「开票日」还是「付款日」？三者能差出一个季度"),
    ("主体", "确认是哪个公司/项目部口径？跨主体合并要先去内部交易"),
    ("税", "金额是含税还是不含税？税率与征收率分别是多少？"),
]

# 五单一致：单据链缺哪一环，就在哪一环扯皮。
CHAIN = [
    ("合同/订单", "金额、单价、数量、付款条件与后续单据是否逐项对得上"),
    ("验收/签收", "有没有验收单？没有验收就付款是内控缺口，不是效率问题"),
    ("发票", "抬头、税号、品名、税率、金额与合同是否一致；专票普票是否匹配抵扣需求"),
    ("付款", "付款日、付款方式、收款账户与合同约定账户是否一致（防诈骗重点）"),
    ("入账", "科目、成本中心、期间归属是否正确；跨期费用是否已做待摊/预提"),
]

# 异常信号：命中任意一条，先把单据停下来，而不是先算总额。
ANOMALY = [
    ("重复报销", "同一发票号/同一金额+同一日期出现在两条记录里"),
    ("金额跳变", "同类费用单笔金额较历史均值偏离超过 30%（需说明原因）"),
    ("跨期", "费用发生在上一期间却在本期入账，且无预提记录"),
    ("缺验收", "付款已完成但链上没有验收/签收记录"),
    ("拆分规避", "同一供应商连续多笔刚好低于审批阈值"),
    ("账户变更", "收款账户与合同约定不一致，或临近付款才变更"),
]

# 汇总维度：先定维度再汇总，否则汇总出来的表没人能用。
DIMENSIONS = ["项目/成本中心", "费用类别", "供应商", "期间（月/季）", "付款状态"]


def work():
    g = C.goal()
    low = g.lower()
    kw = C.keywords(g, 8)
    topic = C.topic(g) or "未命名财务事项"

    scope = [name for name in ("报销", "发票", "对账", "预算", "成本", "账目", "税务", "现金流")
             if name in low]

    # 目标里逐条列出的要求，转成"待补数据/待确认"清单，避免财务任务靠脑补推进。
    asks = [s.strip() for s in C.split_sentences(g, by_comma=True) if len(s.strip()) >= 2][:8]

    payload = {
        "ok": True,
        "topic": topic,
        "keywords": kw,
        "scope": scope or ["未识别（需确认本次是报销、对账、预算还是成本核算）"],
        "caliber_questions": [{"item": a, "why": b} for a, b in CALIBER],
        "document_chain": [{"doc": d, "check": c} for d, c in CHAIN],
        "anomaly_signals": [{"signal": s, "trigger": t} for s, t in ANOMALY],
        "summary_dimensions": DIMENSIONS,
        "pending_inputs": [{"no": i, "ask": a, "owner": "业务方/财务"} for i, a in enumerate(asks, 1)],
        "rule": "金额一律标注币种与含税口径；无法确认的数字写「待补」，不得估算后当结论",
    }
    summary = (
        f"《{topic}》：口径待确认 {len(CALIBER)} 项、单据链 {len(CHAIN)} 环、"
        f"异常信号 {len(ANOMALY)} 条、汇总维度 {len(DIMENSIONS)} 个、待补输入 {len(asks)} 项。"
    )
    return summary, payload


if __name__ == "__main__":
    C.main(work)
