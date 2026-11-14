#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""workers/_common.py —— ProcessRunner 脚本的公共骨架。

设计约束（与 src/orchestration/runners/process.rs 的约定一一对应）：
  * **唯一输入通道是环境变量**。命令行参数一概不用：拼接即注入面。
  * **退出码语义**：0 = 成功（stdout 即步骤产出摘要）；非 0 = 可重试失败。
    因此"业务上的拒绝"（比如红线命中）必须走 stdout 标记，不能靠退出码，
    否则治理层会把它当成瞬时故障反复重试。
  * **stdout 必须是 UTF-8**。Windows 默认管道编码是 cp936，Rust 端按 UTF-8 解码，
    不显式 reconfigure 就会出现"执行成功但输出是乱码"这种最难查的故障。
  * **stdout 有 4000 字符上限**（process.rs OUTPUT_LIMIT，保留尾部）。
    所以摘要要短：先说结论，细节放在 [[SA]] 那行 JSON 里。

约定输出形态：
    第一段：给人看的中文摘要（会被截断保留尾部，故结论写最前）
    最后一行：[[SA]]{"agent":"...","ok":true,...}  机读结构化结果
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from typing import Any, Callable

# ---------------------------------------------------------------- 输出通道

def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


_force_utf8()


def env(name: str, default: str = "") -> str:
    """读环境变量，缺省用 default，并去掉首尾空白。"""
    v = os.environ.get(name)
    return default if v is None else v.strip()


def goal() -> str:
    return env("SA_GOAL")


def task_type() -> str:
    return env("SA_TASK_TYPE", "unknown")


def emit(summary: str, payload: dict[str, Any]) -> None:
    """打印摘要 + 机器可读行。摘要必须精简。"""
    payload = dict(payload)
    payload.setdefault("agent", env("SA_AGENT_ID", "unknown"))
    payload.setdefault("task_id", env("SA_TASK_ID"))
    payload.setdefault("step", env("SA_STEP_INDEX"))
    payload.setdefault("attempt", env("SA_ATTEMPT"))
    print(summary.strip())
    print("[[SA]]" + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def main(fn: Callable[[], tuple[str, dict[str, Any]]]) -> None:
    """统一入口：计时、异常兜底。

    异常一律转成退出码 1（可重试）+ stderr 里的简短原因。
    堆栈只在 SA_WORKER_DEBUG=1 时输出，避免把噪声写进审计。
    """
    started = time.time()
    try:
        summary, payload = fn()
        payload["elapsed_ms"] = int((time.time() - started) * 1000)
        emit(summary, payload)
        sys.exit(0)
    except Exception as exc:  # noqa: BLE001
        print(f"worker 执行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        if env("SA_WORKER_DEBUG") == "1":
            traceback.print_exc()
        sys.exit(1)


# ---------------------------------------------------------------- 轻量文本工具
# 刻意不引入 jieba / numpy / requests 之类依赖：
# 后端脚本必须"拷到任何一台机器上都能跑"，装依赖才能跑等于没做。

# 停用词。这里曾经写成 `set("的了和与及…".split())` —— 那一长串中间没有空格，
# 于是 `.split()` 返回的是**整个字符串作为一个元素**的集合，`w in _STOP` 永远为假，
# 停用词表从未生效过（词段切分也因此一个分隔词都拿不到）。2026-11 修复：显式分两块。
_STOP_CHARS = set(
    "的了和与及或在是对把被让给这那请帮做搞弄要有能会吧呢啊呀嘛哦么我你他她它您咱们"
    # 介词与序数：「给到团队」的「到」、「按天归档」的「按」都是纯功能字，
    # 留着就会和后面粘成「到团队」这种脏词。含这些字的高频真词（到期/签到/按时）
    # 已在 _LEXICON 里，由 _lexicon_guard 先认走，不会被切坏。
    "到向按第每各"
)

_STOP_WORDS = {
    "一个", "我们", "你们", "他们", "它们", "可以", "需要", "应该", "因为", "所以", "如果",
    "但是", "而是", "然后", "并且", "以及", "进行", "实现", "完成", "一下", "一些", "怎么",
    "如何", "什么", "为什么", "哪个", "哪些", "这个", "那个", "目前", "现在", "以后", "关于",
    "通过", "以便", "不要", "不用", "不必", "能够", "是否", "不错", "一份", "一篇", "一条",
    "一张", "一批", "一组", "一遍", "一次", "一点", "一个关于",
    # 时间指代：它们是**参照系**不是主题。实测「分析这次蓝屏的转储文件」里
    # 「这次」不被切，会和后缀粘成「次蓝屏」这种脏词进 doc-agent 的要点标题。
    "这次", "本次", "此次", "上次", "下次", "该次", "那一次",
    # 序数词同时间指代：只表示"第几个"，不承载主题。
    "第一", "第二", "第三", "每个", "各个", "分别",
    # 时间指代/时间范围词。它们是最容易霸占 keywords 名额的一类噪声：
    # 「把本周的报销单汇总」里「本周」会挤掉真对象（keywords 长度优先，2 字词之间
    # 按字典序排，时间词的拼音顺序往往靠前）。任务的关键词要的是"做什么"，
    # 不是"什么时候"。
    "本周", "上周", "下周", "本月", "上月", "下月", "今年", "去年", "明年",
    "今天", "明天", "昨天", "后天", "前天", "上午", "下午", "中午", "晚上",
    # 频率/逐一遍历副词：整词进停用表，好让 _SPLIT_WORDS 把它整段切走，
    # 而不是留下「按天」的「天」去和后面的词粘成碎片。
    "按天", "每天", "每周", "每月", "每年", "每日", "逐条", "逐项", "逐一",
    # 口语正反问式。它字面上是"能+不+能"，三个字里没有一个是虚字，_FUNC_CHARS
    # 一刀切不下它，于是整段粘在动词段里：实测「评估三台机器能不能跑起来这个游戏」
    # 变成一整段 12 字，词表扫描只在开头认出「评估」，真词「机器」被整个漏掉。
    "能不能", "会不会", "要不要", "能否", "可否",
    # 动词短语——它们原先被错收进 _LEXICON，等于把"动作"当"主题"喂给下游：
    # doc-agent 的要点标题第一行会写成「汇总成」。放进停用表后由 _SPLIT_WORDS
    # （按长度降序）整段切走，「把上周的会议纪要汇总成周报」才能干净切成
    # 「会议纪要」+「周报」。
    "汇总成", "落地到", "讲清楚", "说明白",
    # 动作/口语副词：作为关键词没有信息量，且会跟前面的时间词粘成 4 字整段
    # （「每天九点提醒我备份」→「九点提醒」）。放进停用表由 _SPLIT_WORDS 整段切走。
    "提醒", "顺便", "顺手", "记得", "一下下",
}

_STOP = _STOP_CHARS | _STOP_WORDS

_PUNCT = "，。；：！？、（）【】《》「」“”‘’—…·,.;:!?()[]{}<>\"'`~@#$%^&*+=|\\/_\n\t\r "

# 切句后要从片段**尾部剥掉**的标点：只含真正表示"断句/停顿"的符号。
# 刻意不含 《》「」【】""'' 这类成对闭合符号——它们属于正文，剥掉就是吃字
# （实测踩过：目标里写《试点报告》，一刀切 rstrip 后变成《试点报告，标题残缺）。
_TRAIL_PUNCT = "，。；：！？、,.;:!?…·— \t\r\n"

# 词段末尾可剥的方位词/处所字。**只剥末尾单字**，且剥完要 ≥2 字（见 _trim_tail）。
# 刻意不含「上」「前」「后」这类高频构词字：「上线」「后台」「前提」都会被误伤。
_TAIL_DIRS = "里中下内外的"

# 多字方位/范围尾缀。必须整段剥，不能只剥末字——「800 字以内」若先按单字剥「内」，
# 剩下「字以」，而 tokens 的"段 ≤4 字且干净就收"恰好会把这个 2 字残片收进结果，
# 等于把碎片直接送进 keywords。整段剥掉「以内」后只剩「字」，交由 n<2 丢弃。
_TAIL_SUFFIX = ("以内", "以上", "以下", "之间", "为止", "左右", "以外", "之前", "之后")

# 数量词：只用于剥「三个候选」这种"数+量"前缀（见 _trim_tail）。
# 量词表刻意只收不构成真词主干的那些——「台」「条」「项」单独拎出来太容易撞上
# 「平台」「条件」「项目」，这里靠"前一个字必须是数词"来兜住，所以可以放进来。
_NUM_CHARS = "一二三四五六七八九十两几半数多"
_COUNT_CHARS = "个位份条项次组批张篇台套只件种类"

# 额外切段字：它们**只**用于切段，不进 _FUNC_CHARS。
# 不能直接把 _STOP_CHARS 整个并进切段判断——那张表里有「会」「要」「能」，
# 一并进去「会议」会被切成「议」、「要求」会被切成「求」。这里只收介词与序数。
#
# 「要」是单独评估后收进来的：它是能愿动词，不属于介词/序数，但它粘在词尾造成的
# 破坏更大——实测「字段名要和源码一致」整段 4 字被当成一个干净的词收下，
# doc-agent 的要点标题直接写成「字段名要」。收进来之后，含"要"的常用词
# （要求 / 重要 / 主要 / 要点 / 只要 / 必要 / 要素 / 摘要）必须**全部**在 _LEXICON 里，
# 靠 _lexicon_guard 整词认走；漏一个就会被切成「求」「重」「点」这种单字。
_SPLIT_EXTRA = set("到向按第每各要")

# 虚字表：中文里的结构助词 / 单字介词 / 代词 / 承接词。
# 判据用途只有一个——**剪掉跨词切出来的碎片**："小模型的试"含「的」、「型的地」含「的」，
# 它们不可能是业务词；而真词（本地 / 模型 / 试点 / 预算 / 接口 / 周报）几乎不含虚字。
# 这不是词典，也绝不假装能分词：只负责把明显不是词的碎片挡在门外。
_FUNC_CHARS = set("的了和与及在是把被让给这那很就都也还从对为于而且并或如果因所之其么吧呢啊我你他她它您咱")

# 锚点词表：用来把候选词的**边界对齐到真实的词上**。
#
# 为什么非要有它：纯长度贪心切分不知道词从哪儿开始。实测「帮我写一份把大模型引入本地
# 小模型的试点报告」会被切成「帮我写一」+「入本地小」+「地小模型」——全是跨词碎片，
# 而 doc-agent 会把这些当要点标题写进文档。
#
# 有了词表，扫描时先认词、认出就整段跳过，边界自然对齐；词表没覆盖的地方才退化成
# 保守的两字窗口。词表是**纯数据、零依赖**，按需要继续加词即可，不影响任何调用方。
_LEXICON = frozenset(
    """
    大模型 小模型 模型 本地 云端 离线 试点 报告 周报 日报 月报 方案 计划 预算 复盘 总结
    调研 对比 分析 需求 设计 评审 上线 回滚 灰度 发布 版本 验收 交接 文档 示例 说明 结论
    建议 问题 原因 现象 影响 范围 边界 风险 成本 进度 质量 用户 功能 性能 安全 权限 角色
    配置 环境 脚本 代码 函数 接口 表结构 数据库 缓存 队列 消息 事件 状态 错误 异常 重试
    超时 限流 审计 存储 内存 磁盘 网络 端口 进程 线程 并发 集群 节点 调度 编排 路由 网关
    部署 镜像 容器 服务 模块 组件 架构 流程 步骤 任务 目标 数据 指标 监控 日志 告警 测试
    用例 门禁 回归 契约 协议 字段 枚举 依赖 外部 内部 引入 迁移 改造 优化 重构 修复 补全
    梳理 盘点 拆分 合并 校验 验证 演练 探针 预算 红线 治理 插件 槽位 替换 隔离 容错 降级
    前端 后端 页面 组件库 数据库 主键 索引 查询 排序 分页 权限 令牌 登录 注册 刷新 注销
    桌面 文件夹 路径 目录 文件 备份 归档 压缩 解压 截图 图片 照片 录音 视频 音频 剪辑
    表格 清单 图表 报表 会议 纪要 待办 跟进 排期 里程碑 客户 合同 发票 报销 结算 付款
    学习 计划 课程 笔记 复习 考试 论文 专业 编程 算法 引擎 建模 游戏 关卡 数值 美术 动画
    安装 卸载 更新 升级 设置 参数 开关 启动 关闭 进程 窗口 输入法 字体 驱动 显卡 主板
    给出 覆盖 退款 场景 结论 蓝屏 转储 手机 候选 缺点 优点 总额 纪要 待办 职责 检索
    草案 草稿 章节 口径 判据 根因 时序 竞态 快照 订阅 广播 回放 时间轴 断点 续传 心跳
    并行 对齐 对象 分布式 一致性 可用性 幂等 事务 故障转移 主从 主备 副本 分片 选主 负载均衡
    扩容 缩容 漂移 租约 亲和 反亲和 熔断 降级 背压 雪崩 探活 就绪 存活 长连接 慢查询 抖动
    预热 灰度 选举 切换 摘除 驱逐 打散 亲和性 拓扑 容灾 多活 限流 流控 队列 积压 重放
    到期 签到 到达 报到 到场 到底 按时 按键 对接 联调 对应
    增加 整理 起草 盘点 梳理 准备 检查 评估 核对 统计 归类 输出 生成 补充 替换 定位
    规划 统筹 拆解 推进 排布 观测 复跑 收尾 定稿
    """.split()
    # ⚠ 下面这段**必须**写在三引号之外。词表本体是字符串，字符串里的 "#" 不是注释，
    # 会被 split() 当成词收录——2026-11 我恰好踩过：把说明文字写进三引号，
    # "「查一下" "在本机的" "漏一个就会被" 这些半句话全成了词表条目，
    # 而 _lexicon_guard 会把含虚字的条目整段认走，于是「在本机的」被当成一个词
    # 漏进了 keywords。要加词就加在两段三引号之间，注释写在这里。
    #
    # 2026-11 补的一批实词：它们此前不在表里，段内扫描一律漏过，症状是"真词凭空
    # 消失"而不是"切出错词"，所以一直没被人工发现。实测「评估三台机器能不能跑起来
    # 这个游戏」只剩「评估 / 游戏」，「机器」没了。
    + """
    工具 小工具 机器 中文 显存 订单 回填 冒烟 地形 语音 识别 输入 推理 工程 证据
    原型 统一 排查 确认 断线 可用 就绪 减少 挑选 搭建 排序 交互 反馈 素材 贴图 渲染
    """.split()
    # 「项目」原先漏收（词表只收了量词「项」），后果是「按项目分类」被切成 4 字整段
    # 「项目分类」，真词「项目」反而拿不到。含"要"的词必须齐——_SPLIT_EXTRA 里现在
    # 有"要"，漏一个就会被 _runs 从中间劈开（见 _SPLIT_EXTRA 的注释）。
    + """
    项目 重要 主要 要点 要求 只要 必要 要素 摘要
    复现 记录 汇总 提取 转换 评审 审批 通知 同步 发散 提问 追踪 清理 落地 收口 兜底
    """.split()
)


def _wordish(s: str) -> bool:
    """候选片段是否**可能是词**：非空且不含虚字。"""
    return bool(s) and not any(c in _FUNC_CHARS for c in s)


# 切词段用的"分隔词"：虚字之外的整词噪声（帮我 / 一个 / 进行 / 完成 …）。
# 按长度降序匹配，保证「为什么」不会被「什么」抢先切走。
_SPLIT_WORDS = sorted((w for w in _STOP if len(w) >= 2), key=len, reverse=True)


def _trim_tail(seg: str) -> str:
    """剥掉词段末尾的方位词/数量词。只剥**末尾**，且剥完仍要 ≥2 字。

    实测「手机里的照片」「高并发下的超时」被切成「手机里」「高并发下」，
    「整理三个候选方案」被切成「三个候选方案」——末尾那个方位词或开头的数量
    让它不成为一个词。而「上线」「内部」「回滚」这类真词要么不含可剥字，
    要么剥完只剩一个字（不满足 ≥2），不会被误伤。
    """
    # 多字尾缀必须先于单字方位词判断，否则「字以内」会先被剥成「字以」（见 _TAIL_SUFFIX）。
    for suf in _TAIL_SUFFIX:
        if len(seg) >= len(suf) + 1 and seg.endswith(suf):
            seg = seg[: -len(suf)]
            break
    if len(seg) >= 3 and seg[-1] in _TAIL_DIRS:
        seg = seg[:-1]
    # 数量词前缀/后缀：「三个候选」「十个脚本」「两台机器」「评估三台」里的"数+量"
    # 不承载主题，留着会把真词挤下去（实测「评估三台」把「评估」整个吃掉）。
    if len(seg) >= 3 and seg[0] in _NUM_CHARS and seg[1] in _COUNT_CHARS:
        seg = seg[2:]
    elif len(seg) >= 4 and seg[-2] in _NUM_CHARS and seg[-1] in _COUNT_CHARS:
        seg = seg[:-2]
    # 尾缀单字数词：「规划多 agents 集群」被切成「规划多」——「多」是修饰，
    # 但长度优先排序会让这个 3 字碎片压过真词「规划」。
    # 只在**剥完剩下的部分本身就是词表词**时才剥，避免误伤「差不多」这类整词。
    if len(seg) >= 3 and seg[-1] in "多几数两" and seg[:-1] in _LEXICON:
        seg = seg[:-1]
    return seg


def _lexicon_guard(chunk: str, i: int) -> str:
    """从 chunk[i] 起，若开头是一个**含构词虚字**的词表词，返回它，否则返回空串。

    为什么需要它：`_FUNC_CHARS` 是无差别的一刀切，而「并」「对」「给」「这」
    既是虚字也是构词成分。「并发」「对比」「对接」「给出」里的这几个字一旦被
    当分隔符，就会切出「发下」「比表」「接文档」「出结论」这种残片——
    残片会被 `keywords()` 的长度优先排序顶到最前面，直接污染 doc-agent 的要点标题。

    只对"含虚字的词表词"生效是刻意的：不含虚字的词（试点报告 / 周报汇总）本来就
    不会被切开，走原路径能保住这种 4 字组合，不必在这里多此一举。
    """
    for size in (4, 3, 2):
        gram = chunk[i : i + size]
        # 切段用的虚字表（_STOP_CHARS）与判"像不像词"的虚字表（_FUNC_CHARS）不同源，
        # 两边都可能吃掉构词成分，所以取并集。
        if gram in _LEXICON and any(
            c in _STOP_CHARS or c in _FUNC_CHARS or c in _SPLIT_EXTRA for c in gram
        ):
            return gram
    return ""


def _runs(chunk: str) -> list[str]:
    """按 词表锚点 + 虚字 + 分隔词 把块切成"词段"。

    这一步是全部精度的来源：词段边界落在词边界、虚字、噪声词上，段内剩下的连续汉字
    就是"疑似词"的地盘，后面的扫描只在段内进行，绝不会从段中间跨过去。
    """
    out: list[str] = []
    buf = ""
    i = 0
    n = len(chunk)
    while i < n:
        # ① 先认词：词表里含虚字的词（并发 / 对比 / 对接 / 给出）整词切走。
        g = _lexicon_guard(chunk, i)
        if g:
            if buf:
                out.append(_trim_tail(buf))
                buf = ""
            out.append(g)
            i += len(g)
            continue
        # ② 再试**多字分隔词**（这个 / 然后 / 为什么），最后才试单字虚字。
        # 顺序不能反：「这」本身是虚字，若先判单字，「这个」永远匹配不上，
        # 结果是「这」被切成一个字、剩下「个项目」这种带量词的碎片（实测踩过）。
        hit = ""
        for w in _SPLIT_WORDS:
            if chunk.startswith(w, i):
                hit = w
                break
        if hit:
            if buf:
                out.append(_trim_tail(buf))
                buf = ""
            i += len(hit)
            continue
        ch = chunk[i]
        if ch in _FUNC_CHARS or ch in _SPLIT_EXTRA:
            if buf:
                out.append(_trim_tail(buf))
                buf = ""
            i += 1
            continue
        buf += ch
        i += 1
    if buf:
        out.append(_trim_tail(buf))
    return out


def tokens(text: str) -> list[str]:
    """极简分词：按标点/空白切块 → 按虚字切词段 → 段内沿锚点词表扫描。

    为什么不是"全位置滑窗"：滑窗会切出跨词碎片——「引入本地小模型」能切出
    「入本地小」「地小模型」，而 `keywords()` 又是长度优先排序，4 字碎片会把
    2 字的真词（本地 / 模型）整个挤掉。实测表现是 doc-agent 的要点标题里
    出现「入本地小」这种不像话的词。

    现在的切法：
      1. 按标点切块；
      2. 块内按 **虚字 + 噪声词** 切成词段（`_runs`），段边界即词边界候选；
      3. 段 ≤4 字且干净 → 整段作为一个候选（"周报汇总" 这种双词组合也保留）；
      4. 段内再沿 `_LEXICON` 扫描（长词优先），命中就整段跳过，**绝不跨词**；
         扫描不到就前进一个字——宁可少词，也不产出碎片。

    仍然不追求语言学正确：这里只需要"能从目标里拎出几个像样的词"，
    真正的语义理解留给模型后端。
    """
    raw: list[str] = []
    buf = ""
    for ch in text:
        if ch in _PUNCT:
            if buf:
                raw.append(buf)
                buf = ""
        else:
            buf += ch
    if buf:
        raw.append(buf)

    out: list[str] = []
    for chunk in raw:
        for run in _runs(chunk):
            n = len(run)
            if n < 2 or run in _STOP:
                continue
            if n <= 4 and _wordish(run):
                # 短段整段收的前提是"这一段本身像个词"。3/4 字段若有一半命中词表、
                # 另一半不命中，说明它是"真词 + 碎片"粘出来的：
                #   「订单表加」= 订单 + 表加、「先搭地形」= 先搭 + 地形、
                #   「排查高」  = 排查 + 高
                # 整段收下会把碎片顶上 keywords 头部（长度优先，4 字还排第一），
                # doc-agent 的要点标题就成了「订单表加」。丢掉整段不会丢信息——
                # 命中的那半截后面照样会被段内扫描单独收进来。
                # 两截都不命中（子文件夹 / 修仙游戏）或两截都命中（试点报告 /
                # 证据归档）时保留整段：前者多半是真词，后者是干净的双词组合。
                if run in _LEXICON:
                    out.append(run)
                elif n == 2:
                    out.append(run)
                elif n == 3:
                    if run[:2] not in _LEXICON:
                        out.append(run)
                else:
                    if (run[:2] in _LEXICON) == (run[2:] in _LEXICON):
                        out.append(run)
            i = 0
            while i < n - 1:
                hit = ""
                for size in (4, 3, 2):
                    if i + size <= n:
                        gram = run[i : i + size]
                        if gram in _LEXICON:
                            hit = gram
                            break
                if hit:
                    out.append(hit)
                    i += len(hit)
                else:
                    i += 1
    seen: set[str] = set()
    uniq: list[str] = []
    for w in out:
        if w not in seen:
            seen.add(w)
            uniq.append(w)
    return uniq


# 约束 / 否定类片段：它们是"限制条件"，不是"主题"。
# 混进关键词会让下游把"How"当成"What"，实测表现为：主题被识别成「不要引入外部依赖」。
_NEG = ("不要", "不能", "禁止", "不得", "不许", "无需", "别", "避免", "限制", "只能", "必须")


def keywords(text: str, limit: int = 8) -> list[str]:
    """取长度优先（长词信息量高）、去重后的关键词，并剔除约束/否定类片段。"""
    cand = [
        w
        for w in tokens(text)
        if len(w) >= 2 and w not in _STOP and not any(n in w for n in _NEG)
    ]
    cand.sort(key=lambda w: (-len(w), w))
    out: list[str] = []
    for w in cand:
        if any(w in kept or kept in w for kept in out):
            continue
        out.append(w)
        if len(out) >= limit:
            break
    return out


def split_sentences(text: str, by_comma: bool = False) -> list[str]:
    """切句。by_comma=True 时连逗号也切，用于"分句即子任务"的场景（计划拆解）。

    片段返回前会剥掉**尾部的断句标点**（`_TRAIL_PUNCT`），因为调用方把它当"内容"用：
    标题、子任务描述后面跟一个孤零零的「，」是纯噪声。实测踩过的正是这个——
    doc-agent 输出的标题是「…本地小模型的试点报告，」，因为切句时标点被一起收进了片段。
    闭合括号/引号不在剥离表里，所以《试点报告》这种写法不会被吃掉结尾。
    """
    seps = "。；！？\n;!?" + ("，," if by_comma else "")
    parts, buf = [], ""
    for ch in text:
        buf += ch
        if ch in seps:
            s = buf.strip(" \t\r\n").rstrip(_TRAIL_PUNCT)
            if s:
                parts.append(s)
            buf = ""
    if buf.strip():
        parts.append(buf.strip().rstrip(_TRAIL_PUNCT))
    return parts


def clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


_LEAD_VERBS = ("帮我", "帮忙", "请", "麻烦", "给我", "我想", "我要", "需要你", "做一个", "做", "写一个", "写", "搞一个", "搞")

# 请求动词剥完常剩下"量词 + 虚字"（「一份把…」「一个关于…」），它们同样不是主题。
# 单独一张表而不是塞进 _LEAD_VERBS，是为了让注释说清楚"这拨是量词/介词，不是动词"。
_LEAD_NOISE = ("一份", "一个", "一篇", "一条", "一张", "一批", "一组", "关于", "针对", "把", "对")


def _longest_lexeme(base: str) -> str:
    """在 base 里找最长的一个词表词；等长时取先出现的。

    只服务于 topic 的"整句式退化"：当引导动词不在 _LEAD_VERBS 里、句子又长又含虚字时，
    与其把整句当主题，不如退回句中最早露出的实词锚点。
    """
    best = ""
    for i in range(len(base)):
        for size in (6, 5, 4, 3, 2):
            if size <= len(best):
                continue
            if i + size <= len(base) and base[i : i + size] in _LEXICON:
                best = base[i : i + size]
                break
    return best


def topic(text: str, limit: int = 30) -> str:
    """从目标里取出"主题短语"：第一分句 + 去掉开头的请求动词。

    作用：让摘要类 worker 输出「围绕 XX」时不要围绕一句客套话。
    """
    parts = split_sentences(text, by_comma=True)
    base = parts[0] if parts else text
    base = base.strip()
    changed = True
    while changed:
        changed = False
        for v in _LEAD_VERBS + _LEAD_NOISE:
            if not base.startswith(v):
                continue
            # 单字噪声词（把 / 对）会吃掉构词开头的真词：实测「对比 2B 与 14B 的耗时」
            # 被剥成「比 2B…」——「对」当介词剥掉后主题直接改义。
            # 判据：剥完剩下的头两字若本身是词表里的词，说明这个字是构词成分，不剥。
            if len(v) == 1 and len(base) >= 3 and base[:2] in _LEXICON:
                continue
            base = base[len(v):].lstrip("，, 、").rstrip(_TRAIL_PUNCT)
            changed = True
    # 去前导动词后再收一次尾标点：去掉「帮我做一个…」后，剩下的片段可能以顿号/逗号收尾
    base = base.rstrip(_TRAIL_PUNCT).strip()
    if not base:
        kw = keywords(text, 1)
        base = kw[0] if kw else text
    elif len(base) > 12 and any(c in _FUNC_CHARS for c in base):
        # base 仍然是一整句话，不是短语：又长、又含虚字（这/的/把/和），说明句首的
        # 引导动词压根不在 _LEAD_VERBS 里。实测「评估三台机器能不能跑起来这个游戏」
        # 会被原样当作主题塞进摘要的「围绕 XX」，读起来是复读机。
        # 退化为句中最早露出的实词锚点，比整句更有信息量。
        anchor = _longest_lexeme(base)
        if anchor and len(anchor) < len(base):
            base = anchor
    return clip(base, limit)
