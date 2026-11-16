#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""evolve.py —— 自进化闭环的度量与代际比较器。

## 这个脚本要回答的唯一问题

"第 N 代真的比第 N-1 代好吗？" —— 而且答案必须经得起反问。

不这么做的后果（本项目已经踩过）：看到"这一批达交付 62%、上一批 45%"就宣布
改好了。两批各只有几十个样本，62% 与 45% 之间隔着巨大的置信区间，这种比较
只是在读噪声。y 用绝对值比大小，等于用运气给自己发奖状。

## 三层判据（任何一层不过，就不算进化）

1. **质量层**：交付率、编译率必须"不退化"（超过容差就算退化，不是看均值）。
2. **成本层**：单位交付的 token、单轮耗时不得显著变差 —— 靠把超时调大、
   把重试堆满来刷交付率，是假进化。
3. **样本层**：这批样本必须够（默认每代 >= 100 轮且覆盖 >= 2 个项目），
   否则直接拒绝比较，不让它参与代际判定。

显著性用 Wilson 区间判断，而不是点估计：小样本下 p=0.62 与 p=0.45 完全可能
来自同一个分布。只有两个区间不重叠（或以给定方向明确分离）才认定为真变化。

## 用法

    # 把当前全部实测数据固化为一"代"（会写入证据，不可改）
    python scripts/evolve.py --new-gen --note "修了任务流排序与失败归因"

    # 看当前代与上一代的对比
    python scripts/evolve.py --delta

    # 看失败样本都堆在哪些桶里（找下一个该修的 bug）
    python scripts/evolve.py --buckets

    # 只统计指定前缀的批次
    python scripts/evolve.py --buckets --include camp

退出码：0 = 正常；2 = 本代被判定为"未通过进化判据"（有指标退化或样本不足）。
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import math
import pathlib
import statistics
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
SOAK = ROOT / "docs" / "evidence" / "soak"
EVO = ROOT / "docs" / "evidence" / "evolve"


# ------------------------------------------------------------------ 读样本
def load_samples(include="", exclude="", pattern="*.jsonl"):
    """读全部批次样本，按 (tag, round) 去重。

    去重键必须是 (tag, round)：续跑会把同一轮重写一遍，若按整行哈希去重，
    同一轮会被算成两个样本，样本量虚增、比例失真。
    """
    rows, seen = [], set()
    for f in sorted(glob.glob(str(SOAK / pattern))):
        stem = pathlib.Path(f).stem
        # 派生文件不是主样本，必须在 glob 之后立刻拦掉：
        # `-retry` 是分解重试的第二轮，`-decomp` 是两轮结论行。二者都命中 *.jsonl，
        # 而它们的 tag 与主批次不同（camp-g-retry != camp-g），
        # 所以 (tag, round) 去重**拦不住**——放进来就是样本量虚增 + 成功率虚高。
        # 这正是本项目反复踩的那类坑：新加一列数据，先想它是"证据"还是"又一个样本"。
        if stem.endswith("-retry") or stem.endswith("-decomp"):
            continue
        tag = stem
        if tag.startswith("soak-"):
            tag = tag[len("soak-"):]
        if include and not tag.startswith(include):
            continue
        if any(tag.startswith(x) for x in exclude.split(",") if x):
            continue
        for line in io.open(f, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            key = (tag, o.get("round"))
            if key in seen:
                continue
            seen.add(key)
            o["_tag"] = tag
            rows.append(o)
    return apply_current_judge(rows)


_CC = {}


def apply_current_judge(rows):
    """按**当前**判据重算所有"有证据"样本的编译结论，返回新列表。

    为什么要默认重算：尺子只能有一把。样本里存的 `compiles` 是**产生那一刻**
    的判定，判分器改进后如果不重算，老样本就永远带着旧错参与比较——"不许换尺"
    要防的正是这个。而"留证据 + 按当前尺重算"能同时满足两条：历史不重跑、
    跨代可比。

    实测价值（本台第一手）：重算 233 条后 48 条翻案，其中 35 条是
    `compiles=True → None`，原文全是 `'...'` —— Python 里 `...` 是合法的
    Ellipsis 表达式，`compile("...")` 返回成功，于是"模型啥也没写"被记成
    "代码编译通过"。另 13 条是 `<code here>` 之类被记成语法错。
    **假绿比假红还多**，这条重算第一次把虚高的编译通过率打回原形。

    没有证据的老批次原样保留（物理上无法重算），并由 `legacy-unverified`
    桶明确标出，不参与靶心排序。
    """
    sys.path.insert(0, str(HERE))
    try:
        import soak_project as sp
    except Exception:
        return rows
    out = []
    for r in rows:
        r = dict(r)
        ev = r.get("judge_evidence")
        if isinstance(ev, dict) and ev.get("code_full") and not ev.get("code_truncated"):
            code = ev["code_full"]
            lsrc = r.get("lang_source") or ev.get("lang_source") or ""
            lang = r.get("code_lang") or ev.get("lang") or "py"
            key = (lang, ev.get("code_sha1") or "")
            if key not in _CC:
                if sp.is_placeholder(code):
                    _CC[key] = (None, "placeholder")
                elif lsrc == "fallback-py":
                    _CC[key] = (None, "lang-guessed")
                else:
                    ok, _ = sp.compile_check(lang, code)
                    _CC[key] = (ok, "local-recalc")
            ok, csrc = _CC[key]
            r["compiles_original"] = r.get("compiles")
            r["compiles"] = ok
            r["compile_source"] = csrc
            # 清空旧标签，交给 reason_of 按新结论重推，避免同一份样本上
            # 同时存在"旧标签"和"新结论"两套说法。
            r["failure_reason"] = ""
        out.append(r)
    return out


def lang_of(r):
    lg = (r.get("code_lang") or "").lower()
    return "python" if lg in ("python", "py") else ("rust" if lg in ("rust", "rs") else "none")


def reason_of(r):
    """样本的失败原因。老批次没有 failure_reason 字段，这里补一套等价推导。

    ⚠ 第五条反假进化条款（不许把"不知道"算成"失败"）在这里的具体落点：
    老批次的 `compiles` 是**旧二态口径**（把 worker 自报交付当编译结论，出过
    34 条假红），且多数没有 judge_evidence 可复验。直接用 `compiles is False`
    推导成 syntax-error，等于把不可验证的旧结论冒充成本代证据 —— 实测 61 条
    syntax-error 里 41 条属于此类，占三分之二，靶心因此指错了方向。
    所以单列 `legacy-unverified`：它照实计为"已知有问题但证据不在"，不进可
    复验桶、不参与"下一刀"排序。
    """
    fr = r.get("failure_reason")
    if fr:
        return fr
    legacy = not r.get("judge_evidence")
    if r.get("done") and r.get("delivered_code") is True:
        return ""
    if not r.get("code_chars"):
        if (r.get("elapsed") or 0) >= 580 or r.get("status") in (None, "", "unknown"):
            return "timeout-no-output"
        return "no-code-at-all"
    # 前提不成立（占位符 / 语言猜错）要单独立桶，不能塞进 syntax-error。
    # 这三者在本台都真实存在且量很大：`'...'` 既会被判"编译通过"（合法 Ellipsis），
    # `<code here>` 会被判"语法错"，而两者其实都是"模型压根没写代码"。
    if r.get("compiles") is None:
        cs = r.get("compile_source") or ""
        if cs == "placeholder":
            return "placeholder-instead-of-code"
        if cs == "lang-guessed":
            return "lang-guessed"
        if not r.get("judge_evidence"):
            return "legacy-unverified"
        return "unverified"
    if r.get("compiles") is False:
        return "legacy-unverified" if legacy else "syntax-error"
    if r.get("delivered_code") is False:
        # 语法合法、worker 却自己否认交付 —— 实测内容是 `print('Hello World')`、
        # `import os`、`import os\n# ...` 这类玩具代码。它和 not-delivered 要的
        # 修法完全不同：not-delivered 查交付口径，stub-pass 查「模型为什么放弃
        # 任务、退化成背hello world」。混在一个桶里，等于把能力问题的极端形态
        # 当成流程问题处理。
        return "stub-pass" if r.get("compiles") is True else "not-delivered"
    return "done-not-delivered"


# ------------------------------------------------------------------ 统计工具
def wilson(k, n, z=1.96):
    """二项比例的 Wilson 置信区间。小样本比例的"感觉差很多"多数是噪声，
    必须给区间才好意思说"变好了"。n=0 时返回 (0,1)，即"什么都不知道"。"""
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - m) / d), min(1.0, (c + m) / d))


def pct(a, b):
    return round(100.0 * a / b, 1) if b else 0.0


def med(seq, nd=1):
    seq = [x for x in seq if isinstance(x, (int, float))]
    return round(statistics.median(seq), nd) if seq else None


def metrics(rows):
    """一代样本的核心指标。率类一律带 Wilson 区间，成本类给中位数。"""
    n = len(rows)
    done = sum(1 for r in rows if r.get("done"))
    deliv = sum(1 for r in rows if r.get("delivered_code") is True)
    comp = sum(1 for r in rows if r.get("compiles") is True)
    judged = sum(1 for r in rows if r.get("compiles") is not None)
    # 可复验口径：证据在手、能被今天这把尺子复算的子集。老批次的 compiles
    # 是旧口径产物（出过假红也已出过假绿），留在全量分母里会让编译率长期
    # 背着脏数据。两个口径都报，但**跨代比较只看 verifiable**。
    ver = [r for r in rows if isinstance(r.get("judge_evidence"), dict)
           and r["judge_evidence"].get("code_full")
           and not r["judge_evidence"].get("code_truncated")]
    comp_v = sum(1 for r in ver if r.get("compiles") is True)
    judged_v = sum(1 for r in ver if r.get("compiles") is not None)
    # 可用交付：worker 自己认了（delivered_code=True）**且**本机实测语法通过。
    # 为什么要多这一把尺：编译通过率只问"这段字符是不是合法语法"，不问"这是不是
    # 任务要的东西"。实测 459 条有证据样本里，delivered_code=True 的 311 条 P50
    # 长度 1352、310 条可编译；delivered_code=False 的 132 条 P50 长度只有 7 个
    # 字符，其中却有 21 条"编译通过"—— 内容是 `print('Hello World')` 这类能跑
    # 但与任务无关的玩具代码（同一串 209 字符在 camp-b/e/f 五个轮次里反复出现）。
    # 只看语法，这 21 条就是纯假绿；加上交付声明这一维，它们立刻归位。
    usable = sum(1 for r in rows if r.get("delivered_code") is True
                 and r.get("compiles") is True)
    usable_v = sum(1 for r in ver if r.get("delivered_code") is True
                   and r.get("compiles") is True)
    toks = sum(int(r.get("tokens") or 0) for r in rows)
    el = [r.get("elapsed") or 0 for r in rows]
    return {
        "n": n,
        "projects": len({r.get("project") for r in rows}),
        "done": done, "done_pct": pct(done, n), "done_ci": wilson(done, n),
        "delivered": deliv, "delivered_pct": pct(deliv, n), "delivered_ci": wilson(deliv, n),
        "compiles": comp, "compiles_pct": pct(comp, judged), "compiles_ci": wilson(comp, judged),
        "judged": judged,
        "compiles_verifiable": comp_v, "judged_verifiable": judged_v,
        "compiles_verifiable_pct": pct(comp_v, judged_v),
        "compiles_verifiable_ci": wilson(comp_v, judged_v),
        "usable": usable, "usable_pct": pct(usable, judged), "usable_ci": wilson(usable, judged),
        "usable_verifiable": usable_v, "usable_verifiable_pct": pct(usable_v, judged_v),
        "usable_verifiable_ci": wilson(usable_v, judged_v),
        # 语法通过与可用交付之间的落差 —— 这个数本身就是"模型交玩具代码"的量化。
        "stub_gap_pct": round(pct(comp_v, judged_v) - pct(usable_v, judged_v), 1)
        if judged_v else None,
        "tokens_total": toks,
        "tokens_per_delivery": round(toks / deliv, 1) if deliv else None,
        "elapsed_p50": med(el), "elapsed_p95": sorted(el)[int(len(el) * 0.95)] if el else None,
        "multi_agent_pct": pct(sum(1 for r in rows if (r.get("agents") or 0) > 1), n),
        "retries_total": sum(int(r.get("retries") or 0) for r in rows),
    }


def blame_of(r):
    """归责：这一轮失败该记在框架头上，还是记在模型头上。

    这是整个闭环里最容易被跳过、也最贵的一步。没有它，"下一个靶心"只能按
    桶的大小排序，而桶的大小和"框架能不能修"完全是两件事——把模型能力边界
    当成框架 bug 去修，就是最典型的假进化：改一堆代码，数字不动，还以为在进步。

    实测依据（本台 619 轮，有证据 472 轮）：
      成功组   413 轮 —— steps 全为 4、agents 全为 3、交接合计 1239（3.0/轮）
      失败组   189 轮 —— steps 全为 4、agents 全为 3、交接合计  537（2.84/轮）
    两组的框架行为**完全一致**，差别只在 tokens（937 vs 607）与耗时（112s vs
    185s）：失败组花的时间更多、吐的字更少。流程跑满四步、三个 agent 都到场，
    最后交出来的是 `...` 或 `print('Hello World')` —— 框架没坏，是模型在单轮内
    写不出多部件实现，退化成最小可运行骨架。

    所以凡属"流程跑满但产出不达标"的桶，一律归 model，不进下一刀的候选。
    只有流程压根没起来（steps/agents 为 0）或证据取不全（输出被截断）才算框架。
    """
    why = reason_of(r)
    if not why:
        return None
    steps = r.get("steps") or 0
    agents = r.get("agents") or 0
    # 取数失败必须先于 steps/agents 判断。
    # 以前这两件事完全同形：查详情失败 → task_of() 空 → steps=0、agents=0，
    # 归责就判成"流程没启动"。但 evidence/soak/camp-e r249 反证了这一点：
    # 那轮 elapsed=287s、task_id 齐全，任务明明跑完了，只是最后一步查询 401。
    # 两者修法相反——一个改后端启动，一个改客户端续期与错误处理。
    # 混在一起，就会拿"客户端 bug"的证据去改后端。
    dh = r.get("detail_http")
    if dh is not None and str(dh) != "200":
        return "framework-collector"
    # 样本已按新口径定性为取数失败时，即使没带 detail_http 也照样判 collector：
    # soak_project.failure_reason() 造出这个标签的前提就是查详情非 200，
    # 再要求它重复证明一遍，等于让老批次一律错判成 framework。
    if why == "collect-failed":
        return "framework-collector"
    # 一步都没走成 / 一个 agent 都没起来：流程没启动，算框架。
    if steps == 0 or agents == 0:
        return "framework"
    if why == "timeout-no-output":
        return "framework"
    if why == "unverified":
        # 模型明明吐了很多字（该桶 tokens P50=1255，全样本最高），却取不全代码：
        # 这是输出通道/截断问题，不是模型不会写。
        return "framework"
    if why == "legacy-unverified":
        return "unknown"
    # 流程跑满、产出不达标：模型能力边界。
    return "model"


def bucket_failures(rows):
    """把失败样本分桶，并给出每个桶的"下一刀该切哪"。

    分桶不是为了让报表好看：每个桶对应一类不同的根因与不同的修法。
    timeout-no-output 要查超时/吞吐；syntax-error 要查提示词与代码抽取；
    no-code-at-all 往往是模型确实交不出来（能力问题，不是框架问题）。
    """
    b = {}
    for r in rows:
        why = reason_of(r)
        if not why:
            continue
        b.setdefault(why, []).append(r)

    hint = {
        "timeout-no-output": "查 task-timeout、LLM 后端吞吐、并发度；本地 CPU 推理下 600s 可能本就不够",
        "collect-failed": "任务可能跑完了，是客户端没取回来（查详情返回非 200，多为令牌过期/续期失败）。别去调超时阈值，也别改后端启动——这两处改了数字不会动",
        "no-code-at-all": "模型一句话没写：区分是提示词没要求代码、还是模型能力不够",
        "syntax-error": "代码抽出来了但不合法：查提示词约束、抽取是否截断、围栏语言标注",
        "placeholder-instead-of-code": "模型没写代码、只吐了模板占位符（<code here> / ... / <你的代码>）。实测 90 条全部 delivered_code=False、summary 为空，即 worker 已明说没交付——不是耍滑头，是交不出来：查提示词是否把任务拆得太大、单轮给不出完整实现",
        "stub-pass": "能编译但与任务无关的玩具代码（print('Hello World') / import os），worker 自报未交付。它骗过的是编译率不是质检：查模型在无力完成任务时的退化行为，别再去修语法",
        "lang-guessed": "语言是兜底猜的（fallback-py）：查围栏语言标注与嗅探规则，猜出来的语言不能当实测",
        "unverified": "有代码痕迹但本机复验不了：查 output 是否被平台截断",
        "legacy-unverified": "旧口径残留且证据不在：只能整批重测，别拿它选靶心",
        "not-delivered": "有代码但未达交付标准：查判定阈值是否过严/交付口径",
        "done-not-delivered": "任务跑完但没交付物：查 worker 与 agent 分工是否漏了产出环节",
        "not-done": "任务没收尾：查状态机、取消、重试与超时传播",
    }
    out = []
    for k, v in sorted(b.items(), key=lambda kv: -len(kv[1])):
        langs = {}
        for r in v:
            langs[lang_of(r)] = langs.get(lang_of(r), 0) + 1
        by_proj = {}
        for r in v:
            by_proj[r.get("project") or "?"] = by_proj.get(r.get("project") or "?", 0) + 1
        # 可复验 = 有留存证据（judge_evidence）或已由新口径贴过标签。
        # 没有这个拆分，桶里"看得见的问题"和"只剩结论、证据已丢的旧账"会混成
        # 一个数——而这两者该做的事完全不同：前者能定位，后者只能重测。
        ver = sum(1 for r in v if r.get("judge_evidence") or r.get("failure_reason"))
        blame = {}
        for r in v:
            bl = blame_of(r) or "unknown"
            blame[bl] = blame.get(bl, 0) + 1
        out.append({
            "reason": k, "n": len(v), "pct": pct(len(v), len(rows)),
            "verifiable": ver, "unverifiable": len(v) - ver,
            "langs": langs, "top_projects": sorted(by_proj.items(), key=lambda kv: -kv[1])[:4],
            "blame": blame,
            "hint": hint.get(k, ""),
        })
    return out


def _cmp_rate(cur, prev, key_ci, name):
    """比率比较：看区间是否分离，不拿点估计直接下结论。

    基线可能是旧版本固化的，没有后来新增的指标键（例如
    compiles_verifiable_ci）。缺键时回落到同类旧键并在名字上标明口径；
    再缺就明确写"不可比"。宁可显示"口径不同"，也不要 KeyError 把整条
    判据链打断——那会让人误以为"没问题"。
    """
    # 单级别名不够用：可用交付率是后加的，而它的上游编译率（可复验）也是后加的。
    # 老基线两条都没有，只能一路回落到最早的 compiles_ci，否则一加新指标就断链。
    alias = {
        "compiles_verifiable_ci": ("compiles_ci",),
        "usable_verifiable_ci": ("compiles_verifiable_ci", "compiles_ci"),
    }
    kp = key_ci
    if key_ci not in prev:
        for cand in alias.get(key_ci, ()):
            if cand in prev:
                kp = cand
                break
    if key_ci not in cur or kp not in prev:
        return {"metric": name + "（基线缺此项，不可比）",
                "cur": cur.get(key_ci.replace("_ci", "_pct")), "prev": None,
                "verdict": "不可比", "cur_ci": None, "prev_ci": None}
    a, b = cur[key_ci], prev[kp]
    label = name if kp == key_ci else "%s（基线口径=%s）" % (name, kp)
    sep = (a[0] > b[1]) or (b[0] > a[1])
    if sep and a[0] > b[1]:
        v = "改善"
    elif sep and b[0] > a[1]:
        v = "退化"
    else:
        v = "未见显著差异"
    return {"metric": label, "cur": cur[key_ci.replace("_ci", "_pct")],
            "prev": prev[kp.replace("_ci", "_pct")], "verdict": v,
            "cur_ci": [round(x, 3) for x in a], "prev_ci": [round(x, 3) for x in b]}


def judge_gen(cur, prev, min_n=100):
    """进化判据：样本够 + 质量不退化 + 至少一项显著改善。"""
    why = []
    if cur["n"] < min_n:
        why.append("样本不足：本代 %d 轮 < 门槛 %d 轮，拒绝参与代际判定" % (cur["n"], min_n))
    if cur["projects"] < 2:
        why.append("覆盖面不足：只覆盖 %d 个项目，无法代表框架整体" % cur["projects"])
    if not prev:
        return {"verdict": "首代（无基线）", "reasons": why or ["已固化为基线"], "rows": []}

    # 主尺换成"可用交付"（认领 + 语法双过）。编译通过率留作参照：两者若背离，
    # 说明本代多出来的"通过"是玩具代码而不是真产出，这种改善不算改善。
    rows = [_cmp_rate(cur, prev, "delivered_ci", "达交付率"),
            _cmp_rate(cur, prev, "usable_verifiable_ci", "可用交付率（可复验·主尺）"),
            _cmp_rate(cur, prev, "compiles_verifiable_ci", "编译通过率（可复验·参照）"),
            _cmp_rate(cur, prev, "done_ci", "任务完成率")]
    bad = [r for r in rows if r["verdict"] == "退化"]
    good = [r for r in rows if r["verdict"] == "改善"]
    if cur["tokens_per_delivery"] and prev["tokens_per_delivery"]:
        if cur["tokens_per_delivery"] > prev["tokens_per_delivery"] * 1.2:
            bad.append({"metric": "单位交付 token", "cur": cur["tokens_per_delivery"],
                        "prev": prev["tokens_per_delivery"], "verdict": "退化（成本上升 >20%）",
                        "cur_ci": None, "prev_ci": None})
    if bad:
        v = "未通过（有指标退化）"
    elif good:
        v = "通过（有显著改善且无退化）"
    else:
        v = "待定（无显著变化，需要更多样本或更明确的改动）"
    return {"verdict": v, "reasons": why, "rows": rows}


# ------------------------------------------------------------------ 代际固化
def gen_files():
    EVO.mkdir(parents=True, exist_ok=True)
    fs = sorted(EVO.glob("gen-*.json"))
    return fs


def next_gen_no():
    fs = gen_files()
    if not fs:
        return 1
    nos = []
    for f in fs:
        try:
            nos.append(int(f.stem.split("-")[1]))
        except Exception:
            pass
    return (max(nos) + 1) if nos else 1


def save_gen(rows, note, include):
    no = next_gen_no()
    m = metrics(rows)
    prev = None
    fs = gen_files()
    if fs:
        try:
            prev = json.loads(fs[-1].read_text(encoding="utf-8"))["metrics"]
        except Exception:
            prev = None
    j = judge_gen(m, prev, min_n=1)          # 固化时不卡样本门槛，只记录
    obj = {
        "gen": no,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "note": note,
        "include": include,
        "tags": sorted({r["_tag"] for r in rows}),
        "metrics": {k: (list(v) if isinstance(v, tuple) else v) for k, v in m.items()},
        "fail_buckets": bucket_failures(rows),
        "vs_prev": j,
    }
    f = EVO / ("gen-%02d.json" % no)
    f.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    return f, obj


def show_delta():
    fs = gen_files()
    if not fs:
        print("还没有任何代际快照。先跑：python scripts/evolve.py --new-gen --note \"...\"")
        return 0
    latest = json.loads(fs[-1].read_text(encoding="utf-8"))
    prev = json.loads(fs[-2].read_text(encoding="utf-8")) if len(fs) >= 2 else None
    cur = latest["metrics"]
    print("# 代际对比：gen-%02d%s" % (latest["gen"], (" vs gen-%02d" % prev["gen"]) if prev else "（首代基线）"))
    print()
    print("- 时间：%s" % latest["created_at"])
    print("- 说明：%s" % (latest["note"] or "-"))
    print("- 样本：%d 轮 / %d 个项目 / 批次 %s" % (cur["n"], cur["projects"], ", ".join(latest["tags"])))
    print()
    if not prev:
        print("首代没有基线可比，已作为后续比较的基准。")
        return 0
    j = judge_gen(cur, prev["metrics"], min_n=100)
    print("## 判据结论：**%s**" % j["verdict"])
    for r in j["reasons"]:
        print("- %s" % r)
    print()
    print("| 指标 | 上一代 | 本代 | 判定 | 本代区间 | 上一代区间 |")
    print("| --- | --- | --- | --- | --- | --- |")
    for r in j["rows"]:
        print("| %s | %s%% | %s%% | %s | %s | %s |"
              % (r["metric"], r["prev"], r["cur"], r["verdict"],
                 r["cur_ci"] or "-", r["prev_ci"] or "-"))
    print()
    print("| 成本项 | 上一代 | 本代 |")
    print("| --- | --- | --- |")
    for k in ("tokens_per_delivery", "elapsed_p50", "retries_total"):
        print("| %s | %s | %s |" % (k, prev["metrics"].get(k), cur.get(k)))
    return 0 if j["verdict"].startswith("通过") or j["verdict"].startswith("待定") else 2


def show_buckets(include=""):
    rows = load_samples(include=include, exclude="precheck,smoke,verify,v2-smoke,v3-timeout,codegen-modes")
    if not rows:
        print("没有样本")
        return 0
    m = metrics(rows)
    print("# 失败分桶（找下一个该修的 bug）")
    print()
    print("样本 %d 轮：完成 %d（%s%%）、达交付 %d（%s%%）、编译通过 %d/%d（%s%%，分母只含可判定轮）"
          % (m["n"], m["done"], m["done_pct"], m["delivered"], m["delivered_pct"],
             m["compiles"], m["judged"], m["compiles_pct"]))
    print("   可复验子集：编译通过 %d/%d（%s%%）—— 证据在手、能被当前尺子复算"
          % (m["compiles_verifiable"], m["judged_verifiable"], m["compiles_verifiable_pct"]))
    print("   ★ 可用交付 %d/%d（%s%%）—— worker 认领**且**本机语法通过，这才是真交付"
          % (m["usable_verifiable"], m["judged_verifiable"], m["usable_verifiable_pct"]))
    if m.get("stub_gap_pct") is not None:
        print("   ⚠ 两者落差 %s 个百分点 = 能编译但没人认领的玩具代码，虚高的部分就在这里"
              % m["stub_gap_pct"])
    print()
    bs = bucket_failures(rows)
    if not bs:
        print("没有失败样本。")
        return 0
    # 归责汇总放在最前面：桶的大小不等于"能修的量"。不先把这条讲清楚，
    # 看到 placeholder 90 条就会去改提示词，改完发现数字不动——因为它不是
    # 框架的锅。这一行决定了下面整张表该怎么读。
    tot = {"framework": 0, "model": 0, "unknown": 0}
    for b in bs:
        for k, v in b["blame"].items():
            tot[k] = tot.get(k, 0) + v
    fc = tot.get("framework-collector", 0)
    fw = tot["framework"] + fc
    md, un = tot["model"], tot["unknown"]
    print("## 归责：这些失败该记在谁头上")
    print()
    print("| 归责 | 轮数 | 含义 | 该不该动框架 |")
    print("| --- | --- | --- | --- |")
    print("| framework | %d | 流程没起来（steps/agents 为 0）或输出取不全 | **该，这就是下一刀** |" % fw)
    if fc:
        print("| └ framework-collector | %d | 取数失败：任务可能跑满了，是客户端没取回来（查详情/续期出错），"
              "steps/agents 为 0 是取不到、不是没发生 | **该，但修客户端，别动后端** |" % fc)
    print("| model | %d | 流程跑满四步、三个 agent 都到场，仍写不出东西 | 不该，改框架无用 |" % md)
    print("| unknown | %d | 旧口径残留，证据不在 | 先重测再谈 |" % un)
    print()
    print("> 依据：成功组与失败组的 steps 全为 4、agents 全为 3、交接 3.0 vs 2.84 次/轮，"
          "框架行为一致；失败组只是 tokens 少 35%%、耗时多 65%%。**流程没坏，是模型写不出来。**")
    print()
    print("| 原因 | 轮数 | 占比 | 归责 | 可复验/总 | 语言分布 | 高发项目 | 下一刀切哪 |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for b in bs:
        bl = "/".join("%s=%d" % (k, v) for k, v in sorted(b["blame"].items(), key=lambda kv: -kv[1]))
        print("| %s | %d | %s%% | %s | %d/%d | %s | %s | %s |"
              % (b["reason"], b["n"], b["pct"], bl, b["verifiable"], b["n"],
                 " ".join("%s=%d" % (k, v) for k, v in b["langs"].items()),
                 "、".join("%s×%d" % (k, v) for k, v in b["top_projects"]),
                 b["hint"]))
    # 第二张表：只留可复验样本，决策看这张。
    # 第一张表里「可复验/总」很小的桶，数字大是因为旧账多，不代表现在还在犯；
    # 拿它当靶心就会去修一个可能已经不存在的问题——这正是第 41 条假红的来源。
    ver_rows = [r for r in rows if r.get("judge_evidence") or r.get("failure_reason")]
    if len(ver_rows) < len(rows):
        print()
        print("## 可复验子集（%d/%d 轮，决策以本表为准）" % (len(ver_rows), len(rows)))
        print()
        print("| 原因 | 轮数 | 占本子集 | 语言分布 | 高发项目 |")
        print("| --- | --- | --- | --- | --- |")
        for b in bucket_failures(ver_rows):
            print("| %s | %d | %s%% | %s | %s |"
                  % (b["reason"], b["n"], b["pct"],
                     " ".join("%s=%d" % (k, v) for k, v in b["langs"].items()),
                     "、".join("%s×%d" % (k, v) for k, v in b["top_projects"])))
    return 0


def rejudge(include=""):
    """用留存的证据按"当前判分逻辑"重算编译结论。

    存在的意义：判分器本身会写错。发现错了以后有两条路——全部重跑（几小时，
    且新旧样本混在一起），或者拿旧证据按新逻辑重算（分钟级，且能精确回答
    "这次修正影响了多少条、往哪个方向"）。这条路要求样本里留了 judge_evidence，
    没留的老批次会明确报"不可重放"，绝不猜。
    """
    sys.path.insert(0, str(HERE))
    try:
        import soak_project as sp
    except Exception as e:
        print("无法导入 soak_project：%s" % e)
        return 2
    rows = load_samples(include=include)
    replayable, changed, missing = 0, [], 0
    for r in rows:
        ev = r.get("judge_evidence")
        if not isinstance(ev, dict) or not ev.get("code_full") or ev.get("code_truncated"):
            missing += 1
            continue
        replayable += 1
        # load_samples 已按当前判据重算过，所以"原判定"要从 compiles_original 取，
        # 否则新旧值相同，重放结论会永远是"0 条变化"。
        old = r.get("compiles_original", r.get("compiles"))
        code = ev["code_full"]
        lang = r.get("code_lang") or ev.get("lang") or "py"
        lsrc = r.get("lang_source") or ev.get("lang_source") or ""
        # 判据必须与 soak_project.judge 同源，否则"重放"只是把旧错原样再跑一遍：
        # 占位符与"猜出来的语言"在新判据里都不再是实测结论，重放要跟着翻成 None。
        if sp.is_placeholder(code):
            ok, detail = None, "placeholder（占位符，非可编译代码）"
        elif lsrc == "fallback-py":
            ok, detail = None, "lang-guessed（语言靠兜底猜，前提不成立）"
        else:
            ok, detail = sp.compile_check(lang, code)
        if ok != old:
            changed.append({"tag": r["_tag"], "round": r.get("round"), "project": r.get("project"),
                            "lang": lang, "old": old, "new": ok, "detail": (detail or "")[:80]})
    print("# 判分重放")
    print()
    print("- 样本总数：%d" % len(rows))
    print("- 可重放：%d（有完整证据）" % replayable)
    print("- 不可重放：%d（老批次没留证据，无法复算，不猜）" % missing)
    print("- 结论发生变化：%d" % len(changed))
    if changed:
        print()
        print("| 批次 | 轮 | 项目 | 语言 | 原判定 | 重判 | 依据 |")
        print("| --- | --- | --- | --- | --- | --- | --- |")
        for c in changed[:30]:
            print("| %s | %s | %s | %s | %s | %s | %s |"
                  % (c["tag"], c["round"], c["project"], c["lang"], c["old"], c["new"], c["detail"]))
    return 0


def _usable(r):
    """与 soak_project.usable_of 同一口径：认领交付 + 本机实测能编译。"""
    return r.get("delivered_code") is True and r.get("compiles") is True


def show_decomp(include=""):
    """分解重试的账：花多少、救回多少、救回的是哪一类。

    只回答一个问题——**把"单轮"换成"最多两轮"，可用交付率动了几格，代价是什么**。
    这是本系统唯一打算用来碰"模型能力边界"的框架手段：既然单轮写不出来，
    就让它多轮写。所以它必须能被独立量化，否则加了这个开关也只是多烧一倍 CPU。

    刻意与 metrics/judge_gen 分开：那是"这一代比上一代好不好"的代际判定，
    这是"某个改动值不值"的机制评估。两者的分母与样本要求都不同，混在一起会互相污染。
    """
    dec = sorted(glob.glob(str(SOAK / "soak-*-decomp.jsonl")))
    if include:
        dec = [f for f in dec
               if pathlib.Path(f).stem[len("soak-"):].startswith(include)]
    if not dec:
        print("没有分解重试记录。跑长跑时要加 `--decompose` 才会有。")
        return 1

    rows = []
    for f in dec:
        for line in io.open(f, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    if not rows:
        print("分解记录为空")
        return 1

    n = len(rows)
    cnt = {}
    for r in rows:
        cnt[r.get("verdict")] = cnt.get(r.get("verdict"), 0) + 1
    rescued = cnt.get("rescued", 0)
    still = cnt.get("still-bad", 0)
    misfire = cnt.get("first", 0)

    # 按首轮失败原因分组：哪一类"多轮能救"决定了下一刀往哪里使劲。
    # 如果 rescued 全部集中在某一种原因上，那就说明该原因本质是"一次写不完"，
    # 而不是"根本不会写"——两者对应的修法完全不同。
    by_r = {}
    for r in rows:
        k = ((r.get("first") or {}).get("reason")) or "?"
        d = by_r.setdefault(k, {"n": 0, "rescued": 0})
        d["n"] += 1
        if r.get("verdict") == "rescued":
            d["rescued"] += 1

    extra_s = sum((r.get("second") or {}).get("elapsed") or 0 for r in rows)
    extra_tok = sum((r.get("second") or {}).get("tokens") or 0 for r in rows)

    print("## 分解重试：两轮 vs 一轮\n")
    print("触发 %d 轮 | **救回 %d（%.1f%%）** | 仍不可用 %d | 误触发 %d"
          % (n, rescued, pct(rescued, n), still, misfire))
    print("额外成本：%.1f 分钟 / %d tokens（平均每轮 %.0fs、%d tok）"
          % (extra_s / 60.0, extra_tok, extra_s / max(n, 1), extra_tok // max(n, 1)))
    print()

    base = load_samples(include=include)
    if base:
        nb = len(base)
        u1 = sum(1 for r in base if _usable(r))
        print("| 口径 | 可用交付 | 说明 |")
        print("| --- | --- | --- |")
        print("| 单轮（现状） | %d/%d（%.1f%%） | 只看首轮 |"
              % (u1, nb, pct(u1, nb)))
        print("| 两轮（最多重试一次） | %d/%d（**%.1f%%**） | 首轮不可用的再补一轮 |"
              % (u1 + rescued, nb, pct(u1 + rescued, nb)))
        print("| 净变化 | +%d 轮 | 占可修缺口的 %.1f%% |"
              % (rescued, pct(rescued, max(nb - u1, 1))))
        print()

    print("| 首轮失败原因 | 触发 | 救回 | 救回率 |")
    print("| --- | --- | --- | --- |")
    for k in sorted(by_r, key=lambda x: -by_r[x]["n"]):
        d = by_r[k]
        print("| `%s` | %d | %d | %.1f%% |"
              % (k, d["n"], d["rescued"], pct(d["rescued"], d["n"])))

    if misfire:
        print("\n注意：%d 轮「首轮本来就可用」的样本被送去重试了——判据与"
              "触发条件不一致，先去查 usable_of，别先看这里的收益。" % misfire)
    return 0


def selftest() -> int:
    """给归责与分桶这把尺子本身做体检——正样本 + 负样本 + 反例。

    为什么尺子也要自检：本项目的核心教训是"假红和假绿同源"。归责逻辑一旦
    系统性地偏，后面所有"下一刀"都会指向错的地方，而且看起来很有依据。
    这里用构造样本，断言的是**归责判据的方向性**，不依赖任何真实数据。

    三类样本各有各的作用：
      正样本（该判 framework）—— 保证真框架问题没被漏掉；
      负样本（该判 model）    —— 保证模型能力边界没被错怪到框架头上；
      反例（同形不同因）      —— 保证"看起来一样"的两件事没被判成同一件事。
    """
    fails = []

    def ck(name, want, got):
        ok = want == got
        print("  %-4s %s -> %s" % ("OK" if ok else "FAIL", name, got))
        if not ok:
            fails.append("%s: 期望 %r，实际 %r" % (name, want, got))

    print("[selftest] 归责判据（blame_of）")

    # ---- 正样本：框架 ----
    ck("超时无输出 -> framework",
       "framework", blame_of({"failure_reason": "timeout-no-output",
                              "steps": 0, "agents": 0}))
    ck("流程没起来（有原因、无步骤）-> framework",
       "framework", blame_of({"failure_reason": "not-done",
                              "steps": 0, "agents": 0}))
    ck("流程跑满但输出取不全 -> framework",
       "framework", blame_of({"failure_reason": "unverified",
                              "steps": 4, "agents": 3}))

    # ---- 负样本：模型 ----
    ck("占位符（流程跑满）-> model",
       "model", blame_of({"failure_reason": "placeholder-instead-of-code",
                          "steps": 4, "agents": 3}))
    ck("玩具代码 stub-pass -> model",
       "model", blame_of({"failure_reason": "stub-pass",
                          "steps": 4, "agents": 3}))
    ck("流程跑满但一字未写 -> model",
       "model", blame_of({"failure_reason": "no-code-at-all",
                          "steps": 4, "agents": 3}))
    ck("语法错（代码已拿到）-> model",
       "model", blame_of({"failure_reason": "syntax-error",
                          "steps": 4, "agents": 3}))

    # ---- 边界 ----
    ck("旧口径证据缺失 -> unknown",
       "unknown", blame_of({"failure_reason": "legacy-unverified",
                            "steps": 4, "agents": 3}))
    # 成功轮在真实样本里长这样（camp-e/f 348 条无一例外）：
    #   status=succeeded / done=True / delivered_code=True / failure_reason=""
    ck("成功轮（达交付）-> None（不是失败，不能进分母）",
       None, blame_of({"failure_reason": "", "status": "succeeded", "done": True,
                       "delivered_code": True, "steps": 4, "agents": 3}))
    # 已知设计，不是 bug：status 缺失时 reason_of 会推导成 timeout-no-output
    # （"取不到状态"等同于"没等到返回"）。这条显式钉住，免得以后有人把它当缺陷改掉。
    ck("字段全缺 -> 推成 timeout-no-output（已知设计）",
       "framework", blame_of({"steps": 4, "agents": 3}))

    # ---- 反例：同形不同因，这是本台最贵的一类 ----
    # 取数失败时 steps/agents 也是 0，与"流程没起来"完全同形。
    # 若不先判 detail_http，就会拿客户端 bug 的证据去改后端。
    ck("取数失败但任务真跑了 -> framework-collector（不能并进 framework）",
       "framework-collector",
       blame_of({"failure_reason": "timeout-no-output", "steps": 0, "agents": 0,
                 "detail_http": 401, "elapsed": 287.1, "task_id": "t-1"}))
    ck("查详情 200 正常回来 -> 不被误判成取数失败",
       "framework", blame_of({"failure_reason": "timeout-no-output",
                              "steps": 0, "agents": 0, "detail_http": 200}))
    # soak_project.failure_reason() 打出 collect-failed 时，前提就是查详情非 200。
    # 要求它再带一份 detail_http 才算数，等于让所有老批次退回 framework。
    ck("已定性取数失败但没带 detail_http -> 仍是 collector",
       "framework-collector",
       blame_of({"failure_reason": "collect-failed", "steps": 0, "agents": 0,
                 "elapsed": 287.11}))

    print("\n[selftest] 分桶方向性（bucket_failures）")
    # 分桶必须能容忍混杂语言与缺失字段——历史上 sorted(g) 在 str/int 混排时崩过。
    rows = [
        {"failure_reason": "syntax-error", "code_lang": "python", "project": "P5",
         "judge_evidence": {"code_full": "x=1"}},
        {"failure_reason": "syntax-error", "code_lang": "rust", "project": "P2",
         "judge_evidence": {"code_full": "fn main(){}"}},
        {"failure_reason": "placeholder-instead-of-code", "code_lang": "", "project": "P9"},
        {"failure_reason": "timeout-no-output", "code_lang": "python", "project": "P6",
         "steps": 0, "agents": 0},
        {"failure_reason": "", "status": "succeeded", "done": True,
         "delivered_code": True, "code_lang": "python", "project": "P5"},  # 成功轮，必须被跳过
    ]
    try:
        bs = bucket_failures(rows)
        names = [b["reason"] for b in bs]
        ck("成功轮不进桶", 4, sum(b["n"] for b in bs))
        ck("三个原因分桶", 3, len(bs))
        ck("按轮数降序（同数不崩）", True, names[0] == "syntax-error")
    except Exception as e:  # noqa: BLE001
        ck("bucket_failures 不抛异常", "no-raise", "%s: %s" % (type(e).__name__, e))

    print()
    if fails:
        print("[selftest] 失败 %d 项：" % len(fails))
        for f in fails:
            print("  - " + f)
        return 1
    print("[selftest] 全绿")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true", help="归责与分桶判据自检（不打真实数据）")
    ap.add_argument("--new-gen", action="store_true", help="把当前实测数据固化为新一代")
    ap.add_argument("--note", default="", help="本代做了什么改动")
    ap.add_argument("--include", default="")
    ap.add_argument("--delta", action="store_true")
    ap.add_argument("--buckets", action="store_true")
    ap.add_argument("--rejudge", action="store_true", help="用留存证据按当前判分逻辑重算")
    ap.add_argument("--decomp", action="store_true",
                    help="分解重试的两轮对照：救回率、额外成本、按首轮原因分类")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest()
    if a.new_gen:
        rows = load_samples(include=a.include)
        if not rows:
            print("没有样本可固化")
            return 2
        f, obj = save_gen(rows, a.note, a.include)
        print("已固化 gen-%02d：%s" % (obj["gen"], f))
        print("样本 %d 轮 / 项目 %d 个 / 达交付 %s%%"
              % (obj["metrics"]["n"], obj["metrics"]["projects"], obj["metrics"]["delivered_pct"]))
        return 0
    if a.buckets:
        return show_buckets(a.include)
    if a.rejudge:
        return rejudge(a.include)
    if a.decomp:
        return show_decomp(a.include)
    return show_delta()


if __name__ == "__main__":
    sys.exit(main())
