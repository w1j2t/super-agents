#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主题守卫：给垂直 worker 挑一个**能写进标题**的主题词。

为什么单独成文件、而不是改 `_common.topic`：
    `_common` 被 12 个 worker 共用，且门禁 16（`probe_worker_text.py`）正盯着它。
    为了新 worker 的标题好看去动公共分词，风险远大于收益——所以本模块只**包一层**，
    在调用侧做过滤，不碰公共实现。

它解决的具体毛病（都是实测出来的，不是想象）：
    "梳理万炉第三章的世界观设定，检查伏笔…"  → `_common.topic` 给出「主线」（维度词，不是主题）
    "对标两款竞品做个市场分析…"              → 给出整句「标两款竞品做个市场分析」（太长且带动词）
    "这批接口改了数据字段和并发逻辑…"        → 给出「批接口改」（单字动词开头的碎片）

    第二轮补的两类（门禁 20 `probe_topic_guard.py` 抓出来的，上一版 `acceptable()` 放过了）：
    "这批接口改完，帮我列验收标准和回归范围"  → 给出「这批接口改完」（**子句**，不是词）
    "这段剧情的主线推进太慢，人物动机也说不通" → 给出「这段剧情的主线推进太慢」（同上）
    这两条暴露的是同一个洞：`_clean` 只剥首尾标点，`_common.topic` 在逗号处切断后留下的
    半句话内部不带标点，于是"长度够、不是维度词、不以单字动词开头"三条全过——**词形检查
    拦不住句法形态**。所以本版把「是不是一句话」立成独立判据（`_looks_like_clause`），
    与"是不是一个词"分开判。

判定顺序（先信专名，再信实词，最后才认栽）：
    1. 书名号《…》——作品名/产品名最可靠，直接采用
    1b. 「名字 + 第N章/回/幕」——先定位章节号，再从它左边由近及远剥动词与量词
    2. `_common.topic` 的结果，过词形 + 句法两道清洗；进不去就按标点切子句重试
    3. 关键词——**按它在目标句里出现的先后**取第一个合格的（不是按登记顺序）
    4. 兜底串（形如「未命名叙事单元」，宁可朴素也不要碎片）
"""
from __future__ import annotations

import re

import _common as C

# 维度词：它们描述的是"从哪个角度看"，不是"看的是什么"。当成主题会让人以为产出跑偏。
DIMENSION_WORDS = {
    "主线", "支线", "设定", "剧情", "世界观", "伏笔", "节奏", "叙事",
    "市场", "竞品", "定位", "定价", "渠道", "分析",
    "测试", "用例", "验收", "缺陷", "回归", "质量",
}

# 功能性词：出现即说明这段文本是"要求"而非"对象"。
FUNCTION_WORDS = (
    "做个", "帮我", "给我", "怎么", "如何", "是否", "一下", "能不能", "需要",
    "梳理", "检查", "优化", "分析", "评估", "看看", "整理", "设计一个",
)

# 以单字动词开头的串基本都是切词碎片（「批接口改」「改一下」「查磁盘」）。
LEAD_VERB_CHARS = "批改查写做搞弄把将给让对看要想"

# 虚词：出现在一个短串里，说明它是半句话而不是名词对象。
CLAUSE_FUNCTION_CHARS = "的了是把被让给都也就还又才"

# 动量/数量短语：把"一遍""两款"这类东西剥掉，剩下的才是对象。
MEASURE_PREFIXES = (
    "这两款", "那两款", "这款", "那款", "两款", "三款", "几款",
    "这批", "那批", "该批", "本批", "这批", "一批", "整批",
    "这个", "那个", "该", "本", "此", "每个", "各个", "每个",
    "上周", "本周", "下周", "上月", "本月", "上次", "本次", "下一次",
    "一下", "一遍", "一次", "一点", "一些",
)

# 指示词开头 + 有长度，基本是"这批…/这个…"式的半句话。
INDICATOR_CHARS = "这那该本上下每各其此"

MIN_LEN, MAX_LEN = 2, 16
MAX_CLAUSE_LEN = 8          # 超过这个长度还带虚词，就不当词看


def _clean(s: str) -> str:
    return s.strip().strip("《》「」【】()（）,，。.:：;；!！?？、 　")


def _strip_measure_prefix(text: str) -> str:
    """剥掉开头的量词/指示短语：「这批接口改完」→「接口改完」。长词优先。"""
    changed = True
    while changed and len(text) > 2:
        changed = False
        for m in sorted(MEASURE_PREFIXES, key=len, reverse=True):
            if text.startswith(m) and len(text) - len(m) >= 2:
                text = text[len(m):]
                changed = True
                break
    return text


# 动词前缀：中文目标句里动词与专名之间没有空格，只能靠这张表剥。
# 长词放前面（"梳理"要先于"梳"）——否则剥掉一个字的动词会留下另一个碎片。
VERB_PREFIXES = (
    "帮我", "给我", "梳理", "检查", "看看", "优化", "设计", "分析", "评估", "整理",
    "修改", "补充", "完善", "润色", "重写", "测试", "确认", "核对", "规划", "安排",
    "理一下", "看一下", "过一遍", "弄一下",
    "了", "写", "改", "查", "看", "做", "弄", "搞", "读", "审", "理", "过",
)


def _strip_verb_prefix(text: str) -> str:
    """反复剥掉开头的动词与量词，直到剥不动为止（至少留 2 个字，别把名字剥没）。"""
    changed = True
    while changed and len(text) > 2:
        changed = False
        for v in sorted(VERB_PREFIXES, key=len, reverse=True):
            if text.startswith(v) and len(text) - len(v) >= 2:
                text = text[len(v):]
                changed = True
                break
        if not changed:
            stripped = _strip_measure_prefix(text)
            if stripped != text:
                text = stripped
                changed = True
    return _strip_measure_prefix(text)


def _looks_like_clause(t: str) -> bool:
    """这是不是"一句话"而不是"一个词"？

    三条各自独立，命中任一即判为句子：
      1. 内部还带标点——`_common.topic` 偶尔切不干净，整句原样带逗号回来
      2. 以指示/量词开头且有一定长度——「这批接口改完」「这个产品投放选哪个渠道」
      3. 超长且含虚词——「这段剧情的主线推进太慢」
    刻意不在这里判长度上限，那是 `acceptable()` 的事，两个判据各管一层。
    """
    if re.search(r"[，。、；：！？,;:!?]", t):
        return True
    if len(t) >= 4 and t[0] in INDICATOR_CHARS:
        return True
    if len(t) > MAX_CLAUSE_LEN and any(ch in t for ch in CLAUSE_FUNCTION_CHARS):
        return True
    return False


def acceptable(token: str) -> bool:
    """一个 token 能不能当主题？词形四条 + 句法一条，任一条不过就弃。"""
    t = _clean(token)
    if not (MIN_LEN <= len(t) <= MAX_LEN):
        return False
    if t in DIMENSION_WORDS:
        return False
    if any(w in t for w in FUNCTION_WORDS):
        return False
    # 单字动词开头：极少数合法的专名（"查令十字街"）会误伤，但碎片率高得多，先挡
    if len(t) <= 4 and t[0] in LEAD_VERB_CHARS:
        return False
    # 全是标点/数字的不算
    if not re.search(r"[\u4e00-\u9fa5A-Za-z]", t):
        return False
    # 最后一道：是个词，不是半句话
    if _looks_like_clause(t):
        return False
    return True


def _try(candidate: str) -> str | None:
    """清洗 → 剥前缀 → 再判。返回可用主题或 None。"""
    c = _clean(candidate)
    if acceptable(c):
        return c
    stripped = _strip_verb_prefix(c)
    if stripped != c and acceptable(stripped):
        return stripped
    return None


def pick(goal: str, keywords: list[str] | None = None, fallback: str = "未命名目标") -> str:
    """按"专名 → 公共主题 → 关键词 → 兜底"的顺序取一个可用主题。"""
    goal = goal or ""

    # 1) 书名号：跨题材最可靠的专名信号
    for m in re.finditer(r"《([^》]{2,20})》", goal):
        got = _try(m.group(1))
        if got:
            return got

    # 1b) 「名字 + 第N章/回/幕」：叙事类目标里最常见的写法，剥掉章节号就是作品名。
    #
    #     为什么不用「左取 2~4 字」的正则：正则只会从长到短回溯，于是「梳理万炉第三章」
    #     第一次匹配就吃成「梳理万炉」（4 字恰好后面跟着「第」）。方向反了 —— 应当先定位
    #     「第N章」，再从它左边**由近及远**剥动词前缀，剥完剩下的才是名字。
    #     这样「万炉」（2 字）不被截断，「焚天炉」（3 字）也不被多切。
    for m in re.finditer(r"第[一二三四五六七八九十百\d]+[章节回幕]", goal):
        tail = goal[max(0, m.start() - 8):m.start()]
        tail = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]+", "|", tail).split("|")[-1]  # 只留紧邻的连续字
        rest = _strip_verb_prefix(tail)
        if 2 <= len(rest) <= 4:
            got = _try(rest)
            if got:
                return got

    # 2) 公共分词给出的主题：先整串判，判不过再按标点切子句逐个试
    #    （切子句这一步是第二轮补的：`_common.topic` 会在逗号处截断，留下的半句话
    #     本身不带标点，整串判据看不见它，只能主动切开来逐个过。）
    raw = _clean(C.topic(goal))
    got = _try(raw)
    if got:
        return got
    for part in re.split(r"[，。、；：！？,;:!?]", goal):
        got = _try(part)
        if got:
            return got

    # 3) 关键词：按它在目标句里出现的先后取，而不是按 worker 的登记顺序。
    #    登记顺序反映的是"这个域都关心什么"，不反映"这句话在问什么"。
    ordered = sorted(
        (k for k in (keywords or []) if k and k in goal),
        key=lambda k: goal.index(k),
    )
    rest = [k for k in (keywords or []) if k not in ordered]
    for k in ordered + rest:
        got = _try(k)
        if got:
            return got

    # 4) 认栽：宁可朴素，也不要「批接口改」这种碎片进标题
    return fallback


if __name__ == "__main__":
    # 自测：前几组是真实踩过的样本，改判据时先跑它。
    #
    # 判据分两档，这是踩过坑之后的结论：
    #   exact=True  必须命中那个最理想的词（作品名类，因为跑偏最刺眼）
    #   exact=False 只要求「可接受」——不是维度词、不是碎片、不是半句话
    # 第一版把每一组都写成精确匹配，结果把「月销」「接口」这种完全可用的结果判成失败，
    # 属于断言过严，不是代码有问题。
    cases = [
        ("梳理万炉第三章的世界观设定，检查伏笔有没有埋在开头", ["主线", "冲突", "开头"], "万炉", True),
        ("《万炉》的战斗数值与关卡节奏要不要改", ["战斗", "数值"], "万炉", True),
        ("帮我弄一下", ["弄一下"], "未命名目标", True),
        ("对标两款竞品做个市场分析，月销 3 万，定价怎么定", ["分析", "定价", "月销"], None, False),
        ("这批接口改了数据字段和并发逻辑，验收要测什么", ["批接口改", "数据字段", "并发"], None, False),
        ("", [], "未命名目标", True),
        # 第二轮补的两类：子句 vs 词
        ("这批接口改完，帮我列验收标准和回归范围", ["验收", "测试", "回归", "缺陷"], None, False),
        ("这段剧情的主线推进太慢，人物动机也说不通", ["剧情", "动机", "人物"], None, False),
    ]
    bad = 0
    for goal, kws, want, exact in cases:
        got = pick(goal, kws, "未命名目标")
        ok = (got == want) if exact else acceptable(got)
        print(f"{'OK ' if ok else 'BAD'} {'精确' if exact else '可接受'} 期望={want!r:<12} 实得={got!r:<14} ← {goal or '(空目标)'}")
        bad += 0 if ok else 1
    print(f"\n{len(cases) - bad}/{len(cases)} 通过")
    raise SystemExit(1 if bad else 0)
