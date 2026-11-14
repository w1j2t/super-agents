#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""legal-agent 执行脚本：把"看下这份合同/协议"的目标，落成条款体检表。

合同风险的分布很集中：不是"写得好不好看"，而是**该有的条款有没有、有的条款天平
偏哪边**。所以这里按「必备条款 → 缺失即风险」和「高危句式 → 一眼识别」两张表走，
再给出可谈判的底线建议。不做法律意见，只做体检清单与定位。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _common as C  # noqa: E402

# 必备条款：缺一条就是一个敞口。level 表示缺失时的风险等级。
CLAUSES = [
    ("标的与范围", "高", "做什么、不做什么、边界在哪；范围写不清＝后续全部争议的源头"),
    ("价款与支付", "高", "金额、税率、付款节点、逾期利息；缺逾期利息＝对方拖着不给也没成本"),
    ("交付与验收", "高", "交付物形态、验收标准、验收期限、逾期未验收视为通过？"),
    ("违约责任", "高", "违约金比例与计算基数、是否封顶、是否与实际损失挂钩"),
    ("解除与终止", "中", "什么情形可解除、解除后已履行部分怎么结算"),
    ("保密", "中", "保密范围、期限、违约后果；单向保密 vs 双向保密要先看清"),
    ("知识产权归属", "高", "成果归谁、原有权属声明、二次开发与商用授权范围"),
    ("数据与个人信息", "高", "是否涉及个人信息、处理目的是否写明、是否需单独同意"),
    ("不可抗力", "低", "范围、通知义务、后果（免责还是延迟履行）"),
    ("争议解决", "中", "诉讼还是仲裁、管辖地在哪；管辖地对维权成本影响极大"),
    ("转让与分包", "低", "能否转让、能否分包、分包责任归属"),
]

# 高危句式：这些写法本身不违法，但天平明显偏向一方，谈判时要盯住。
RED_FLAGS = [
    ("单方变更权", "「甲方有权随时调整……」——对方可单方改核心条件，己方无对价保护"),
    ("无限责任", "「赔偿由此造成的一切损失」未见封顶——敞口不可测算，无法投保也无法预算"),
    ("视为条款", "「未在 X 日内提出异议视为认可」——流程依赖，一旦漏看即失权"),
    ("自动续期", "「期满自动续展一年」——忘记书面终止就等于被动续约"),
    ("排他与竞业", "「合作期内不得与同类第三方合作」——需核算对自身业务的实际限制范围"),
    ("验收主观化", "「以甲方满意为准」——验收标准不可量化，回款节点形同虚设"),
    ("管辖不利", "约定在对方所在地法院/仲裁委——异地维权的时间与费用成本常高于标的额"),
]


def work():
    g = C.goal()
    low = g.lower()
    kw = C.keywords(g, 8)
    topic = C.topic(g) or "未命名合同事项"

    kind = "未识别（需确认是采购合同 / 服务协议 / 保密协议 / 劳动或合作协议）"
    for k, v in {
        "采购": "采购合同", "服务": "服务协议", "保密": "保密协议（NDA）",
        "劳动": "劳动合同", "合作": "合作协议", "外包": "外包/委托开发合同",
        "租赁": "租赁合同", "股权": "股权/投资协议",
    }.items():
        if k in low:
            kind = v
            break

    # 目标里点名要看的条款，优先排到最前面——按用户关注点排序，而不是按模板顺序。
    asked = [s.strip() for s in C.split_sentences(g, by_comma=True) if len(s.strip()) >= 2][:6]
    focus = [c for c in CLAUSES if any(c[0][:2] in a or c[0][:3] in a for a in asked)]
    rest = [c for c in CLAUSES if c not in focus]

    payload = {
        "ok": True,
        "topic": topic,
        "keywords": kw,
        "contract_kind": kind,
        "clause_checklist": [
            {"clause": c, "risk_if_missing": lv, "why": why, "priority": "用户点名" if c in [f[0] for f in focus] else "常规"}
            for c, lv, why in focus + rest
        ],
        "red_flags": [{"pattern": p, "risk": r} for p, r in RED_FLAGS],
        "user_focus": asked,
        "negotiation_baseline": [
            "违约与赔偿责任必须可测算：给出计算基数与封顶值",
            "验收标准必须可量化：写清指标、方法与期限",
            "管辖地尽量落在己方所在地，退让时至少争取中立地",
        ],
        "rule": "只做条款定位与风险分级，不出具法律意见；重大金额或排他条款须交执业律师复核",
    }
    summary = (
        f"《{topic}》按{kind}体检：条款 {len(CLAUSES)} 项（用户点名 {len(focus)} 项优先）、"
        f"高危句式 {len(RED_FLAGS)} 类、谈判底线 {len(payload['negotiation_baseline'])} 条。"
    )
    return summary, payload


if __name__ == "__main__":
    C.main(work)
