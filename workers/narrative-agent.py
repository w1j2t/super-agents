#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""narrative-agent 执行脚本：把"这个设定/剧情行不行"落成可逐条打勾的裁决表。

不做代笔（写整章剧情需要模型且容易自我感觉良好），做的是编辑最难、作者最不想做、
又最贵的三件事：**设定会不会自相矛盾、角色的动机链断没断、伏笔有没有埋就收**。
每一项都给"怎么判定它坏了"，因为"感觉不太对"不是能提交评审的意见。

一条贯穿全脚本的判断：叙事 bug 几乎从不是"写得不好看"，而是**信息没交代就要求读者接受**。
所以本脚本的核心动作是记账——把目标里出现的每个设定/动机/伏笔，记成"已交代/待交代"。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _common as C  # noqa: E402
import _topic_guard as TG  # noqa: E402

# 题材 → (题材名, 该题材最容易塌的那根梁)
GENRES = {
    "修仙": ("修仙 / 修真", "境界体系一旦被写崩，战力就没有可比性：必须一份境界表锁死每一境的能力边界"),
    "仙侠": ("仙侠", "门派立场与个人道心的冲突若无代价，正邪之分就只剩衣服颜色"),
    "奇幻": ("西方奇幻", "魔法代价缺失：能随意施法时，冲突只能靠更强的敌人推进"),
    "科幻": ("科幻", "技术设定当装饰：设定若不影响人的选择，它就不该存在"),
    "悬疑": ("悬疑推理", "线索不公平：真凶依赖读者看不到的信息，等于欺骗"),
    "恐怖": ("恐怖", "恐惧源过早具象化，未知被解释完，张力随之消失"),
    "武侠": ("武侠", "武学体系无上限也无下限，越写越像法术"),
    "都市": ("都市", "现实规则被随手破坏（钱的来源、权责关系），可信度先塌"),
    "历史": ("历史向", "时代器物/称谓/制度错位，一个错字就能让读者出戏"),
    "恋爱": ("情感向", "关系推进靠误会硬拖，人物智商为剧情服务"),
}

# 设定自洽三问：每一问都指向一种典型的"自相矛盾"。
CANON_QUESTIONS = [
    ("与旧设定是否冲突", "这条新设定会推翻此前哪一条已公开的规则？把它点出来，能改旧的就别加例外"),
    ("约束了谁的行为", "设定若对任何人的选择都没有约束力，它就只是背景板，写进正文也会被读者跳过"),
    ("读者凭什么知道", "通过什么事件/对话/物件让读者得知？「旁白直接说」最省事也最廉价"),
]

# 动机链四段：断任何一段，角色的关键选择都会显得是作者的安排。
MOTIVE_CHAIN = [
    ("想要什么", "具体到可失败的目标（拿到某物/阻止某人/守住某处），「想变强」不算"),
    ("为什么是现在", "触发事件是什么？没有触发，行动就是作者按下的开关"),
    ("愿意付什么代价", "代价决定人物厚度；无代价的决心不构成选择"),
    ("失败会失去什么", "把失败后果写清，读者的紧张感才有落点"),
]

# 伏笔台账：三态。缺"埋设位置"就是未埋先收，缺"回收位置"就是断头线。
FORESHADOW_STATES = [
    ("已埋未收", "埋设位置已知、回收位置待定 —— 可接受，但必须登记预期回收窗口"),
    ("已埋已收", "两端都明确 —— 健康；回收时须让读者能自己回忆起来，别靠旁白复述"),
    ("未埋先收", "回收处出现读者从未见过的信息 —— 这是硬伤，等同于欺骗读者"),
    ("埋了不收", "埋设后全书未回收 —— 读者会当成作者忘了，除非刻意留白且有第二部承诺"),
]

# 设定漂移信号：这些词后面跟的，往往是作者事后补的设定（"其实""原来"最典型）。
# 伏笔四态的判定词表（两个维度：说过埋没有 × 抱怨的是收不回还是凭空冒）
PLANT_CUES = ("埋", "铺垫", "开头", "前文", "早在", "第几章", "立了", "立过")
RECALL_CUES = ("没回收", "未回收", "没收", "没收回", "忘了收", "没圆", "断头", "悬着", "没落地")
ORPHAN_CUES = ("凭空", "突然出现", "前面没", "没交代过", "第一次出现", "没提过")
PAYOFF_CUES = ("回收了", "已回收", "收尾了", "呼应上了", "收了")

DRIFT_MARKERS = ["其实", "原来", "一直", "从来", "早就", "没想到", "突然发现", "竟是"]

# 节奏：信息释放的检查点。缺任何一类，读者会在中段失去推进力。
PACING_BEATS = [
    ("钩子", "开篇 300 字内必须有一个未解问题（不是背景介绍）"),
    ("第一次兑现", "读者第一次得到「确实如此」的确认，位置不宜超过全篇 1/5"),
    ("中点反转", "推翻读者的既有判断，且反转的信息必须在前文埋过"),
    ("最低点", "主角失去最重要的一样东西，此时才允许用「成长」解决"),
    ("收束", "主线问题给出明确答案；留白只能留给支线"),
]

def work():
    g = C.goal()
    low = g.lower()
    kw = C.keywords(g, 8)
    # 主题走守卫：实测 `_common.topic` 会给出「主线」这类维度词，标题会像跑偏了。
    topic = TG.pick(g, kw, "未命名叙事单元", domain="narrative")

    hit = []
    for k, (name, risk) in GENRES.items():
        if k in low and name not in [h[0] for h in hit]:
            hit.append((name, risk))
    if not hit:
        hit = [("未识别（需先确认题材：修仙 / 奇幻 / 科幻 / 悬疑 …）",
                "题材不明时设定尺度无从校准——先定题材与基调，再谈自洽")]

    # 设定漂移：把命中的信号词连同它所在的分句一起摘出来，让人知道是哪一句可疑。
    clauses = [s.strip() for s in C.split_sentences(g, by_comma=True) if s.strip()]
    drift = []
    for cl in clauses:
        for m in DRIFT_MARKERS:
            if m in cl and cl not in [d["clause"] for d in drift]:
                drift.append({"clause": C.clip(cl, 60), "marker": m,
                              "why": "事后补设定高发句式：确认它是否推翻前文规则，还是只补细节"})
                break

    # 动机链：目标里出现"为什么/动机"却没有"因为/代价"，直接点出来。
    motive_gap = []
    if any(w in g for w in ("为什么", "动机", "转变", "黑化", "背叛")):
        if not any(w in g for w in ("因为", "代价", "失去", "换")):
            motive_gap.append("目标问了动机，但没给代价或因果——这类改法最后会变成"
                              "「他本来就这样」，人物扁平化就从这里开始")
    if any(w in g for w in ("反派", "BOSS", "boss")):
        motive_gap.append("反派动机必须能自证：「他认为自己在做好事」比「他天生邪恶」耐用十倍")

    # 伏笔：原实现只问"提没提埋设"，于是「伏笔也没回收」被答成「未埋先收」——
    # 结论方向恰好相反：作者抱怨的是没收线，不是没埋伏。四态必须由**两个维度**
    # 决定：说过埋没有（PLANT）、抱怨的是收不回还是凭空冒（RECALL / ORPHAN）。
    foreshadow = []
    # 触发条件不能只认"伏笔"二字：作者常直接抱怨"凭空变强、前面没交代过"，
    # 那已经是"未埋先收"的完整证据了，不需要他再说一次"伏笔"。
    if any(w in g for w in ("伏笔", "铺垫", "线头", "扣子")) or any(w in g for w in ORPHAN_CUES):
        plant = any(w in g for w in PLANT_CUES)
        recall = any(w in g for w in RECALL_CUES)
        orphan = any(w in g for w in ORPHAN_CUES)
        payoff = any(w in g for w in PAYOFF_CUES)
        if orphan:
            foreshadow.append({"state": "未埋先收（硬伤）",
                               "action": "先补埋设：回收处出现读者从未见过的信息，等同于欺骗读者"})
        if payoff:
            foreshadow.append({"state": "已埋已收",
                               "action": "回读一次埋设处：读者应当能自己想起来，别靠旁白复述"})
        if recall and not payoff:
            # 抱怨"没收"= 默认埋过（否则他抱怨的会是"凭空冒出"）
            foreshadow.append({
                "state": "已埋未收（说是埋了没收）" if plant else "埋了不收（默认已埋）",
                "action": "定回收窗口：哪一章收、由谁触发、收的时候读者已经知道多少",
            })
        elif plant and not recall and not payoff:
            foreshadow.append({"state": "已埋未收", "action": "登记埋设位置与预期回收窗口"})

    # 目标里列出的具体待办（逗号/顿号分隔），逐条转成可交付项。
    tasks = [s.strip() for s in C.split_sentences(g, by_comma=True) if len(s.strip()) >= 2][:8]

    payload = {
        "ok": True,
        "topic": topic,
        "keywords": kw,
        "genre": [{"name": n, "load_bearing_beam": r} for n, r in hit],
        "canon_questions": [{"question": q, "how_to_answer": a} for q, a in CANON_QUESTIONS],
        "motive_chain": [{"link": l, "requirement": r} for l, r in MOTIVE_CHAIN],
        "foreshadow_states": [{"state": s, "meaning": m} for s, m in FORESHADOW_STATES],
        "drift_suspects": drift,
        "motive_gaps": motive_gap,
        "foreshadow_actions": foreshadow,
        "pacing_beats": [{"beat": b, "checkpoint": c} for b, c in PACING_BEATS],
        "open_subtasks": [{"no": i, "task": t, "need": "定稿：与哪条旧设定对齐 + 读者获知方式"}
                          for i, t in enumerate(tasks, 1)],
        "rule": "任何新设定必须写清「它推翻了谁 / 约束了谁 / 读者怎么知道」三行，缺一行就不进正文",
    }
    summary = (
        f"《{topic}》按 {hit[0][0]} 口径体检：设定自洽 {len(CANON_QUESTIONS)} 问、"
        f"动机链 {len(MOTIVE_CHAIN)} 段、伏笔 {len(FORESHADOW_STATES)} 态台账、"
        f"节奏检查点 {len(PACING_BEATS)} 个；可疑设定漂移 {len(drift)} 处"
        f"{'、动机缺口 ' + str(len(motive_gap)) + ' 条' if motive_gap else ''}"
        f"{'、伏笔预警 ' + str(len(foreshadow)) + ' 条' if foreshadow else ''}。"
    )
    return summary, payload


if __name__ == "__main__":
    C.main(work)
