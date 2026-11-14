#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""market-agent 执行脚本：把"这个产品/功能怎么定位、跟谁比、卖多少钱"落成可核对的矩阵。

不做销量预测（任何不带概率区间的预测都是猜测），做的是三件**结论能被别人当场反驳**的事：
竞品矩阵填没填满、定位声明能不能一句话自证、定价每一档有没有涨价的理由。
外加一件最容易漏的：**每条数据的采集时点**——没时点的市场数据等于没有数据。

本脚本的一条硬规矩：凡是目标里出现的数字，若没有单位或没有时点，一律进"证据缺口"，
不允许它悄悄变成结论的一部分。
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _common as C  # noqa: E402
import _topic_guard as TG  # noqa: E402

# 竞品矩阵维度：任一格空着，矩阵就退化成"我觉得"。
MATRIX_DIMS = [
    ("目标用户", "具体到可被找到的人群特征（不是「所有人」）；写不清就说明产品还没定"),
    ("核心卖点", "用户凭什么换过来？一句话说不清＝没有卖点"),
    ("价格带", "与竞品的价差是否被卖点解释；无解释的价差只会带来「凭什么贵」"),
    ("渠道", "用户在哪第一次见到它（应用商店 / 社群 / 内容平台 / 线下）"),
    ("可替代方案", "包括不做（用户继续用 Excel / 继续忍着），这是最常见的竞品"),
]

# 定位声明三问：三问都答得上才算定位成立。
POSITIONING = [
    ("为谁", "一个可被指认的群体；「年轻人」不算，范围太宽等于没定位"),
    ("解决什么问题", "用户此刻用什么凑合着解决？凑合方案就是你的真竞品"),
    ("凭什么相信你", "可验证的证据（数据 / 试用 / 案例），不是形容词"),
]

# 定价阶梯：每档必须有不靠降价就能成立的理由。
PRICING_LADDER = [
    ("入门档", "把「愿意试」的门槛降到接近零；它的任务是获客，不是赚钱"),
    ("主力档", "80% 用户应落在这里；若主力档卖不动，问题通常在定位而不在价格"),
    ("尊享档", "存在的意义是让主力档显得划算，并对重度用户收足价值"),
    ("升级理由", "每档之间必须有功能或额度上的硬差别；只靠「更多次数」很快见顶"),
]

# 渠道评估维度：四个都要打分，只看流量会买到假用户。
CHANNEL_DIMS = [
    ("获客成本", "单个有效用户成本，注意区分「下载」与「留存到第 7 天」"),
    ("人群匹配度", "渠道人群与目标用户的重合度，比流量总量重要一个量级"),
    ("可复现性", "同样的投放能不能再来一次；不可复现的渠道只能当一次性红利"),
    ("回本周期", "多久能收回获客成本；算不出就别放大投放"),
]

# 证据缺口检测：这些词单独出现＝无意义强度词，必须换成数字。
VAGUE_WORDS = ["很多", "大量", "大多数", "领先", "第一", "顶级", "火爆", "蓝海", "巨大"]

# 竞品名必须是"名字"，不是"要求"。第一版正则 `对标\s*(2~12字)` 直接把
# 「对标两款竞品做个市场分析」整句当成竞品名，产出一眼假。现在分两路取：
#   ① 书名号/引号里的内容——最可靠，人写名字时常加引号；
#   ② 「对标/对比/相比/竞品是」之后，先按分隔符切断，再剥数量词与动词，最后过主题守卫。
_DOMAIN_DIM_WORDS = {w for ws in TG.DOMAINS.values() for w in ws}

QUANTIFIER_PREFIXES = ("两款", "三款", "两个", "三个", "几家", "这家", "那家", "某", "同类", "其他")


def _norm(name: str) -> str:
    """归一化：去掉引号书名号外壳与虚词尾巴，否则「《暗黑》」与「暗黑」会被当成两个竞品。"""
    return name.strip().strip("《》「」“”\"'()（）·，,。；;的 　")


def _extract_competitors(goal: str) -> list[str]:
    out: list[str] = []

    def add(raw: str) -> None:
        name = _norm(raw)
        # 域维度词不是名字：`对标两款竞品` 会被剥成「竞品」，它描述的是"要填哪一栏"，
        # 不是"跟谁比"。放进 competitors 会让矩阵出现一行没有对手的空维度。
        if name in _DOMAIN_DIM_WORDS:
            return
        if TG.acceptable(name) and name not in out:
            out.append(name)

    for pat in (r"《([^》]{2,16})》", r"「([^」]{2,16})」", r"“([^”]{2,16})”"):
        for m in re.finditer(pat, goal):
            add(m.group(1))

    for m in re.finditer(r"(?:对比|对标|相比|竞品是|比起)\s*([^，,。；;、\s]{1,24})", goal):
        frag = m.group(1)
        # 只按"引出结论"的词切断（做/的/怎么…）。
        # **不能按"和/与"切**：那是并列竞品的连接词，把它们当断点会漏掉一半对手。
        # 实测「对标幻兽帕鲁和戴森球」原先只收到「幻兽帕鲁」——漏抓比抓错更危险，
        # 因为矩阵会拿一个残缺的对手名单去比。
        frag = re.split(r"做|的|怎么|如何|要", frag)[0]
        for part in re.split(r"和|与|及|、|/|跟|,", frag):
            part = part.strip()
            for q in QUANTIFIER_PREFIXES:
                if part.startswith(q):
                    part = part[len(q):]
                    break
            if part:
                add(part)
    return out[:6]


def _numbers_without_context(goal: str):
    """把目标里的数字逐个检查：缺单位或缺乏时点的，收进缺口清单。

    为什么必须做：市场类目标里「某产品月销 3 万」这种句子，缺的往往正是「哪个月」。
    三个月后这条数据会被人当成现状引用，错误就此固化。
    """
    gaps = []
    has_date = bool(re.search(r"(20\d{2}|本月|上月|今年|去年|季度|Q[1-4]|月|周)", goal))
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(万|亿|千|%|元|块|人|单|个|次|天)?", goal):
        num, unit = m.group(1), m.group(2)
        if len(num) <= 1 and not unit:
            continue  # 「5 个功能」这类序号式数字不追
        if not unit and not has_date:
            gaps.append({"fragment": C.clip(m.group(0), 20),
                         "missing": "单位与时点（哪个口径、哪段时间）"})
        elif not has_date:
            gaps.append({"fragment": C.clip(m.group(0), 20),
                         "missing": "时点（这条数据是哪一期）"})
    return gaps[:6]


def work():
    g = C.goal()
    kw = C.keywords(g, 8)
    # 主题走守卫：实测 `_common.topic` 会给出整句「标两款竞品做个市场分析」，带动词且过长。
    topic = TG.pick(g, kw, "未命名市场对象", domain="market")

    competitors = _extract_competitors(g)

    vague = [{"word": w, "replace_with": "可核实的具体数字或来源"} for w in VAGUE_WORDS if w in g]
    gaps = _numbers_without_context(g)

    # 渠道：目标提到渠道词才展开，否则只给维度表，避免输出一堆无用结论。
    mention_channels = [w for w in ("应用商店", "社群", "内容平台", "线下", "投放", "私域", "直播")
                        if w in g]

    tasks = [s.strip() for s in C.split_sentences(g, by_comma=True) if len(s.strip()) >= 2][:8]

    payload = {
        "ok": True,
        "topic": topic,
        "keywords": kw,
        "competitors": competitors,
        "matrix_dims": [{"dim": d, "how_to_fill": h} for d, h in MATRIX_DIMS],
        "positioning": [{"question": q, "standard": s} for q, s in POSITIONING],
        "pricing_ladder": [{"tier": t, "job": j} for t, j in PRICING_LADDER],
        "channel_dims": [{"dim": d, "note": n} for d, n in CHANNEL_DIMS],
        "channels_mentioned": mention_channels,
        "vague_claims": vague,
        "evidence_gaps": gaps,
        "open_subtasks": [{"no": i, "task": t, "need": "定稿：结论一句话 + 支撑来源 + 采集时点"}
                          for i, t in enumerate(tasks, 1)],
        "rule": "每条结论后面必须跟「来源 + 采集时点」；给不出来就降级为待验证假设，不进结论段",
    }
    summary = (
        f"《{topic}》市场体检：竞品矩阵 {len(MATRIX_DIMS)} 维、定位 {len(POSITIONING)} 问、"
        f"定价 {len(PRICING_LADDER)} 层、渠道 {len(CHANNEL_DIMS)} 维；"
        f"点名竞品 {len(competitors)} 个、空洞断言 {len(vague)} 处、证据缺口 {len(gaps)} 处。"
    )
    return summary, payload


if __name__ == "__main__":
    C.main(work)
