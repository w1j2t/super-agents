#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""soak_project.py —— 大型项目级实测台（真模型 / 多 agent 协作 / 可断点续跑）

## 为什么要有这个脚本

门禁体系能证明"接口没退化"，但证明不了"这东西好不好用"。
好用与否只能在真实负载下看：真模型、真任务、真产出。本脚本就是那把尺子。

## 它测什么（判据都是硬的，不看主观印象）

对每一轮任务，从服务端真实返回里取三样东西：任务状态、步骤台账（每步的
agent_id / success / attempts / tokens / output）、共享记忆引用。据此算：

1. `done`       —— 任务是否真到 succeeded（不是 202、不是 running）
2. `agents`     —— 这条任务里实际出场的 agent 数（多 agent 协作的广度）
3. `handoff`    —— 相邻步骤换 agent 的次数（协作是"接力"还是"一个 agent 干到底"）
4. `code`       —— 产出里是否真有代码块（```lang ... ```）；有则抽出最长那段
5. `compiles`   —— 抽出的代码能否过语法关：Python 用 py_compile，Rust 用
                   `rustc --edition 2021 --emit=metadata`（只编译不链接，不出二进制）
6. `retries`    —— attempts>1 的步骤数（错误纠错是否真在工作）
7. `tokens`     —— 本任务消耗，用于算单位产出成本
8. `elapsed`    —— 端到端耗时

第 5 项是这个台子的核心。**"模型说它写了代码"和"这段代码能编译"是两件事**，
中间隔着整个系统的正确性校验能力。只数第 1 项，会把"答得漂亮但生成的东西跑不起来"
记成成功，那就是自欺。

## 上千次怎么跑得动（这是工程约束，不是折中）

`POST /tasks` 的限流键是 `task_submit:{user.id}`，默认 60 次/小时/用户。
所以压测必须：多账号轮转（每个账号各自有配额）+ 显式放宽配额（`--quota`）。
两个手段都要，只放宽不轮转，会退化成"单人连打"，测出来的是限流不是系统。

## 断点续跑

每轮结果立即 append 到 JSONL（带 fsync）。中断后同一条命令加 `--resume` 接着跑，
已完成的轮次跳过。**崩溃不丢样本**——否则上千次跑不完。

## 用法

    # 台子自检：12 轮，先把判据跑通
    python scripts/soak_project.py --rounds 12 --tag baseline

    # 全量：1000 轮，20 个账号，配额放开
    python scripts/soak_project.py --rounds 1000 --users 20 --quota 100000 --tag v1

    # 中断后继续
    python scripts/soak_project.py --rounds 1000 --users 20 --quota 100000 --tag v1 --resume

    # 只汇总，不跑
    python scripts/soak_project.py --report-only --tag v1
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
EVID = ROOT / "docs" / "evidence" / "soak"

sys.path.insert(0, str(ROOT / "scripts"))
import probe_llm_soak as soak  # noqa: E402  复用启动/提交/续期/详情解析，不重造


# ------------------------------------------------------------------ 任务集

# 六个"大型项目"，每个项目按真实软件工程阶段拆。任务描述刻意写成"迭代式"，
# 因为大型项目不是一次写完的：先定结构、再写实现、再补测试、再写文档、再改。
# 每一阶段都要求交付**可运行的东西**，而不是"说明一下怎么做"。
PROJECTS = [
    ("P1 存档系统", [
        "为 3D 修仙 RPG《万炉》设计存档系统的数据结构：需要保存角色属性、背包、任务进度与场景位置，"
        "要求可版本升级。请给出 Rust 结构体定义代码，字段要写全，不要省略。",
        "按上面的存档结构，写 Rust 实现：序列化为 JSON 文件、读取时校验版本号、旧版本自动迁移。给出完整可编译代码。",
        "为存档系统写 Rust 单元测试代码：覆盖空存档、版本过旧、字段缺失、文件损坏四种情况。",
        "为存档系统写接口文档：说明文件格式、版本迁移规则、失败时的返回码。",
    ]),
    ("P2 战斗结算", [
        "为《万炉》设计战斗伤害结算规则并用 Rust 实现：包含基础攻击、暴击、防御减伤、元素克制四个环节，"
        "给出可编译的计算函数代码。",
        "为战斗结算写边界测试代码：零攻击、负防御、暴击倍率上限、克制链成环（必须不死循环）。",
        "战斗结算出现死循环（克制链成环时互相放大）。请定位问题并给出修好的代码，说明改了什么。",
    ]),
    ("P3 任务系统", [
        "为《万炉》设计任务链系统：任务有前置依赖、可放弃、可重复，用 Rust 写出任务状态机的数据结构与状态转移函数代码。",
        "为重任务系统写测试代码：前置未完成不能接、放弃后可重接、循环依赖必须被检出。",
        "把任务系统接进 HTTP 接口：给出 axum 路由处理函数的完整代码，含参数校验与错误返回。",
    ]),
    ("P4 限流中间件", [
        "给 axum 后端写一个按用户限流的中间件代码：每分钟 N 次，超出返回 429 并带 Retry-After 头。要求可直接编译。",
        "为限流中间件写测试代码：正常放行、超限拒绝、窗口滑动边界、并发下不超发。",
        "限流中间件在并发下会多放行约 10% 的请求。请定位原因并给出修复后的代码。",
    ]),
    ("P5 日志聚合", [
        "写一个 Python 脚本：把多行 JSON 日志按小时聚合成统计表，输出 CSV。要可直接运行，含 main。",
        "为日志聚合脚本写 pytest 测试代码：空文件、坏行跳过、时区跨越、字段缺失。",
        "日志聚合脚本遇到乱码行会整体崩掉。请修好并给出完整代码。",
    ]),
    ("P6 API 文档生成", [
        "写一个 Python 脚本：扫描 Rust 源码里的 axum 路由，生成 Markdown 接口文档（路径、方法、参数、返回）。要能直接运行。",
        "为文档生成脚本补测试代码：路由带路径参数、嵌套路由、同名方法不同路径三种情况。",
        "生成的文档里路径参数丢了（比如 /tasks/:id 变成 /tasks/）。请定位并修复，给出完整代码。",
    ]),
    # ---- 下面 4 组是 Python 侧项目：不是凑数，而是给"多 agent 协作链路"留出
    # 可成功的样本。上面 6 组全是 Rust，2B 模型在 Rust 上基本交不出可编译代码
    # （实测 smoke5：390/1345 字符、引号未闭合），若任务集全是 Rust，千轮数据
    # 会 100% 失败，看不出"框架本身好不好用"与"模型能力够不够"两件事的区别。
    ("P7 命令行工具", [
        "写一个 Python 命令行工具：扫描目录下的重复文件（按内容哈希），支持 --dry-run 与 --json 输出。"
        "要能直接运行，含 main 与参数解析。",
        "给这个工具写 pytest 测试代码：目录不存在、空目录、同名不同内容、存在真重复四种情况。",
        "工具在大文件上很慢（1GB 文件读满全量哈希）。请改成先按大小分组、再只对同尺寸文件算哈希，给出完整代码。",
    ]),
    ("P8 数据清洗", [
        "写一个 Python 脚本：读入 CSV（含缺失值、重复行、日期格式混乱），清洗后输出规范 CSV。要能直接运行。",
        "给清洗脚本写 pytest 测试代码：全空文件、只有表头、日期跨格式、金额带千分位四种情况。",
        "清洗脚本遇到带 BOM 的 UTF-8 文件会把首列表头读成乱码。请定位并修复，给出完整代码。",
    ]),
    ("P9 微服务接口", [
        "写一个 Python HTTP 服务：提供任务清单的增删改查接口（用标准库 http.server 即可，不要第三方依赖），"
        "含请求体校验与错误码。要能直接运行。",
        "给这个服务写测试代码：创建成功、重复创建冲突、删除不存在、字段类型错误四种情况。",
        "服务的并发请求会互相覆盖内存里的数据。请加线程锁并说明锁的粒度，给出完整代码。",
    ]),
    ("P10 任务队列", [
        "写一个 Python 文件队列 worker：从目录取任务文件、按顺序处理、处理完归档，失败重试 3 次后进死信目录。"
        "要能直接运行，含 main 与优雅退出。",
        "给队列 worker 写测试代码：正常处理、处理超时、重试耗尽进死信、退出时不留半成品文件四种情况。",
        "worker 在进程被杀时会留下半个输出文件，下次启动会当成已完成。请定位并修复（先写临时文件再原子改名），"
        "给出完整代码。",
    ]),
]


def _project_lang(goals) -> str:
    """按任务文本判项目语言：全部目标里提到 Python 即算 py，否则算 rs。

    用文本判定而不是给 PROJECTS 加第三个字段：任务描述本身就必须写清语言
    （否则 worker 也得靠猜），顺着它判等于不增加第二处真相。
    """
    return "py" if "python" in " ".join(goals).lower() else "rs"


def build_goals(rounds: int, suite: str = "all"):
    """把项目阶段摊平成一条有顺序的任务流。

    刻意做成"项目内按阶段推进、跨项目轮转"，而不是随机抽题：
    这样跑出来的序列更接近真实的连续迭代开发（后一阶段能看到前一阶段的产物语境），
    也让"第 N 轮"与"哪个阶段"可复现、可对账。
    """
    stages = []
    for pname, goals in PROJECTS:
        if suite != "all" and _project_lang(goals) != suite:
            continue
        for i, g in enumerate(goals, 1):
            stages.append({"project": pname, "stage": i, "goal": g})
    out = []
    r = 0
    while len(out) < rounds:
        s = stages[r % len(stages)]
        cycle = r // len(stages) + 1          # 第几遍迭代
        item = dict(s)
        item["round"] = len(out) + 1
        item["cycle"] = cycle
        if cycle > 1:
            # 第二轮起明确告诉它"这是同一项目的又一次迭代"，避免完全同题触发缓存式假绿
            item["goal"] = (f"（第 {cycle} 次迭代）在已有实现的基础上，{s['goal']}")
        out.append(item)
        r += 1
    return out


# ------------------------------------------------------------------ 判分

CODE_RE = re.compile(r"```([A-Za-z0-9_+#.-]{0,20})[ \t]*\n(.*?)```", re.S)

# 代码围栏的语言标签是模型随手写的，不能当判据用：早前只认 ```python/rust，
# 模型写 ```py3、```rs、``` 或干脆不写标签时，抽取直接落空；而兜底逻辑又把一切
# 未知标签一律当 rust 送去 rustc，Python 代码当然编译不过——评测台自己制造了
# "代码通过率 62%" 这种假低分。所以标签只作提示，真判定看内容特征。
_PY_HINTS = ("def ", "import ", "class ", "print(", "self.", "__init__", "lambda ", "elif ", "None", "True")
_RS_HINTS = ("fn ", "let mut ", "impl ", "pub struct ", "pub fn ", "-> ", "println!", "use std::", "&mut ", "match ", "::new(")


def sniff_lang(body: str) -> str:
    """按内容判断语言。判不出来就返回 unknown，交给下游记 skipped 而不是判死。"""
    py = sum(body.count(h) for h in _PY_HINTS)
    rs = sum(body.count(h) for h in _RS_HINTS)
    if py == 0 and rs == 0:
        return "unknown"
    return "py" if py >= rs else "rs"


def extract_code(text: str):
    """从步骤产出里抽出最长的代码块。返回 (lang, body, lang_source)。

    取最长而不是第一个：模型常先给一小段示意、再给完整实现，第一个往往最没用。
    lang_source 记 label（模型自己标了）还是 sniff（我们嗅出来的），报告里要能追溯。
    """
    best = (None, None, 0, None)
    for m in CODE_RE.finditer(text or ""):
        label = (m.group(1) or "").lower().strip()
        body = m.group(2)
        if not body.strip():
            continue
        if label in ("python", "py", "python3", "py3"):
            lang, src = "py", "label"
        elif label in ("rust", "rs"):
            lang, src = "rs", "label"
        else:
            lang, src = sniff_lang(body), "sniff"
        if len(body) > best[2]:
            best = (lang, body, len(body), src)
    return best[0], best[1], best[3]



def compile_check(lang, code):
    """真编译检查。返回 (ok, detail)。

    Python: py_compile 走真实语法树解析。
    Rust: rustc --emit=metadata 只做类型检查+元数据，不链接、不产二进制，
          所以缺 main、缺依赖都能过 —— 我们只判"这段代码是不是合法 Rust"。
          若环境没 rustc，如实返回 skipped，绝不冒充通过。
    """
    if not code:
        return None, "no-code"
    if lang == "py":
        try:
            compile(code, "<soak>", "exec")
            return True, "ok"
        except SyntaxError as exc:
            return False, f"{exc.__class__.__name__}:{exc.msg}:{exc.lineno}"
        except Exception as exc:                      # noqa: BLE001
            return False, f"{exc.__class__.__name__}:{exc}"
    # rust
    rustc = pathlib.Path(os.environ.get("USERPROFILE", ".")) / ".cargo" / "bin" / "rustc.exe"
    exe = str(rustc) if rustc.exists() else "rustc"
    try:
        with tempfile.TemporaryDirectory() as td:
            src = pathlib.Path(td) / "probe.rs"
            src.write_text(code, encoding="utf-8")
            r = subprocess.run([exe, "--edition", "2021", "--emit=metadata",
                                "-o", str(pathlib.Path(td) / "out.rmeta"), str(src)],
                               capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                return True, "ok"
            err = (r.stderr or "").strip().splitlines()
            return False, (err[0][:120] if err else f"rc={r.returncode}")
    except FileNotFoundError:
        return None, "no-rustc"
    except subprocess.TimeoutExpired:
        return False, "rustc-timeout"


def judge(row):
    """把一次 run_task 的原始返回压成一行可统计的判定。"""
    steps = row.get("steps") or []
    agents = []
    for s in steps:
        a = s.get("agent")
        if a and a not in agents:
            agents.append(a)
    handoff = 0
    for a, b in zip(steps, steps[1:]):
        if a.get("agent") and b.get("agent") and a.get("agent") != b.get("agent"):
            handoff += 1
    retries = sum(1 for s in steps if (s.get("attempts") or 1) > 1)
    def _full(s):
        # 判据一律吃完整产出。`out` 是给人看的 120 字摘要，拿它抽代码只能抽到残句，
        # 早前 rust 组「代码字符中位数 17」就是残句的体检报告。
        return s.get("out_full") or s.get("out") or ""

    joins = " ".join(_full(s) for s in steps)

    # 结构化字段优先从 worker 的 [[SA]] payload 取。
    # 为什么要单开这条路：worker 的真交付物在 payload["code"] 里，而
    # extract_code() 只认 ``` 围栏；worker 老实输出 JSON 时它反而抽不到，
    # 结果就是"模型明明写了 1345 字符、报表记 0"（实测 smoke5 第 2 轮）。
    payloads = []
    for s in steps:
        raw = _full(s)
        idx = raw.find("[[SA]]")
        if idx >= 0:
            # 用 raw_decode 而不是 json.loads(fragment)：
            # worker 的 payload 后面常跟进度行/CLIXML 残渣，整串 loads 必抛，
            # 结果就是"模型交付了 1096 字符、报表记 0"（实测 py120 第 8 轮）。
            try:
                obj, _ = json.JSONDecoder().raw_decode(raw[idx + 6:].lstrip())
                payloads.append(obj)
            except json.JSONDecodeError:
                continue
    p_code, p_chars, delivered, modes = "", 0, None, []
    for p in payloads:
        if not isinstance(p, dict):
            continue
        if p.get("mode"):
            modes.append(p["mode"])
        if p.get("code"):
            p_code = p["code"]
        p_chars = max(p_chars, int(p.get("code_chars") or 0))
        if p.get("delivered_code") is not None:
            delivered = bool(p.get("delivered_code"))

    # 次级证据：机器可读前缀。平台把 step output 截到约 120 字符，[[SA]] payload 常被
    # 拦腰切断（verify1 三轮全都如此），此时只剩 code-agent 写在行首的 [CODE] 还完整。
    mark_lang, mark_deliv, mark_chars, mark_mode = "", None, 0, ""
    # mode 段允许缺失/被截断：尾段窗口可能正好切在 mode= 之后（verify4 轮 2 就是
    # "mode=" 戛然而止），此时该轮交付事实仍应记上，只是 mode 留空。
    MARK_RE = re.compile(r'ok=(\d) lang=([a-z]+|-) chars=(\d+) file=([^\s"\\]+)(?: mode=([^\s"\\]*))?')
    for s in steps:
        if s.get("agent") not in (None, "code-agent"):
            continue
        m = MARK_RE.search(_full(s))
        if m:
            mark_deliv = m.group(1) == "1"
            mark_lang = "" if m.group(2) in ("-", "") else m.group(2)
            mark_chars = int(m.group(3))
            mark_mode = "" if (not m.group(5)) or m.group(5) in ("-", "") else m.group(5)
            break
    if delivered is None:
        delivered = mark_deliv
    if not modes and mark_mode:
        modes.append(mark_mode)
    p_chars = max(p_chars, mark_chars)

    lang, code, lang_src = extract_code(joins)
    if not lang and mark_lang:
        lang, lang_src = mark_lang, "worker-mark"
    if p_code:
        code = p_code
        # worker 的结构化 payload 没写语言，靠内容嗅探，别再默认 rust。
        lang, lang_src = sniff_lang(code), "sniff-payload"
        if lang == "unknown":
            lang, lang_src = "py", "fallback-py"
    ok, cdetail = compile_check(lang, code)
    # 取不到完整代码时（平台截断），退回 worker 自检结论，并显式标注来源。
    # 宁可标注口径，也不要留一列全 False 的假读数——那会把"交付成功"记成"编译失败"。
    csrc = "local"
    if not code and mark_deliv is not None:
        ok = bool(mark_deliv)
        csrc = "worker"
        cdetail = "worker 自检（output 被平台截断，本机无法复验）"
    return {
        "done": row.get("task_status") == "succeeded",
        "status": row.get("task_status"),
        "agents": len(agents),
        "agent_list": agents,
        "handoff": handoff,
        "steps": len(steps),
        "retries": retries,
        "code_lang": lang,
        # 语言判定从哪来：label=模型标的围栏 / sniff=按内容嗅探 / worker-mark=worker
        # 自报标记。报告里带上它，才能区分"模型写错了语言"和"我们把语言判错了"。
        "lang_source": lang_src,
        # 字符数取 payload 报告的原始长度：它记录"模型到底写了多少"，
        # 与"能不能交付"是两件事，混在一起会让能力边界看不出来
        "code_chars": max(len(code or ""), p_chars),
        "delivered_code": delivered,
        "mode": modes[-1] if modes else "",
        "compiles": ok,
        "compile_source": csrc,
        "compile_detail": cdetail,
        "tokens": int(row.get("tokens") or 0),
        "elapsed": row.get("elapsed"),
        "http": row.get("http"),
        "note": row.get("note") or "",
        # 失败证据必须留下来：只记"failed"等于没记。每步的成功与否、错误首行、
        # 产出开头，都是后面归因打磨时要看的原始材料。
        "step_detail": [
            {"agent": s.get("agent"), "success": s.get("success"),
             "attempts": s.get("attempts"), "tokens": s.get("tokens"),
             "err": (s.get("err") or "")[:160],
             "out": (s.get("out") or "")[:160]}
            for s in steps
        ],
        # 可审计摘录：把 worker 自报的关键字段单独留一份，报告与事后复算靠它。
        # 不存代码全文——几百轮会把 jsonl 撑到几十 MB，而核对口径只需要这几项。
        "code_payload": [
            {"mode": p.get("mode"), "language": p.get("language"),
             "filename": p.get("filename"), "code_chars": p.get("code_chars"),
             "delivered_code": p.get("delivered_code"), "repaired": p.get("repaired"),
             "rounds": p.get("rounds"), "fix_rounds": p.get("fix_rounds"),
             "summary": (p.get("summary") or "")[:120]}
            for p in payloads if isinstance(p, dict) and p.get("wants_code") is not None
        ],
    }


# ------------------------------------------------------------------ 主循环

def append_jsonl(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def load_done(path):
    done = {}
    if not path.exists():
        return done
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue                      # 上一轮中断写了半行：跳过，不当成已完成
            if o.get("round"):
                done[o["round"]] = o
    return done


def normalize_mode(m: str):
    """把 worker 自报的 mode 归一到标准取值，并回答"这条是不是被截断的残片"。

    为什么要归：平台写 jsonl 时 mode 可能只剩前几个字符（`mo`、`model-unveri`、
    `model-self-f`）。若原样进分布表，一次 100 轮会摊出十几种"模式"，读起来像
    worker 行为混乱，实际是采集端断了。归一后分布才有意义，被截断的另行计数。
    返回 (标准值, 是否残片)。残片的前缀必须能对上标准值才算残片——对不上就是
    真出现了没见过的模式，此时原样保留，不硬塞。
    """
    if not m or m == "-":
        return ("-", False)
    STD = ["model", "model-failed", "model-unverified", "model-self-fix",
           "evidence-only", "rule", "template"]
    if m in STD:
        return (m, False)
    hits = [s for s in STD if s.startswith(m)] if len(m) >= 2 else []
    if len(hits) == 1:
        return (hits[0], True)
    if len(hits) > 1:
        # `mo` 同时能补成 model / model-failed / model-unverified —— 猜一个就是造假，
        # 写成「model?(歧义)」既保留"确实被截断"的事实，又不假装知道它原来是哪个。
        return (f"{hits[0]}?(歧义)", True)
    return (m, False)


def summarize(path, tag):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    if not rows:
        print("没有样本")
        return None
    n = len(rows)
    done = [r for r in rows if r["done"]]
    with_code = [r for r in rows if r["code_lang"]]
    compiled = [r for r in rows if r["compiles"] is True]
    tried = [r for r in rows if r["compiles"] is not None]
    lat = sorted(r["elapsed"] or 0 for r in rows)
    tok = sorted(r["tokens"] for r in rows)

    def pct(a, b):
        return round(100.0 * a / b, 1) if b else 0.0

    # 失败归因：按 status 与 note 归类，别只说"失败了 30%"
    why = {}
    for r in rows:
        if r["done"]:
            continue
        key = r["status"] or ("http%s" % r["http"]) or "unknown"
        why[key] = why.get(key, 0) + 1

    # 协作分布
    collab = {}
    for r in rows:
        collab[r["agents"]] = collab.get(r["agents"], 0) + 1

    # 交付口径与语言分层。
    # 为什么必须分开算：同一批样本里"成功"有三种成色——任务跑完、模型写了代码、
    # 代码达交付标准。只报一个 done_pct，会把"跑了但交不出东西"说成好用；
    # 再按语言拆开，才能看出框架的问题与模型能力上限各占多少。
    delivered = [r for r in rows if r.get("delivered_code") is True]
    mode_dist = {}
    mode_cut = 0
    for r in rows:
        m = r.get("mode") or "-"
        std, was_cut = normalize_mode(m)
        mode_cut += 1 if was_cut else 0
        mode_dist[std] = mode_dist.get(std, 0) + 1

    def bucket(sel):
        if not sel:
            return None
        b_done = sum(1 for r in sel if r["done"])
        b_deliv = sum(1 for r in sel if r.get("delivered_code") is True)
        b_pcc = sum(1 for r in sel if r["compiles"] is True)
        b_judged = sum(1 for r in sel if r["compiles"] is not None)
        chars = sorted((r.get("code_chars") or 0) for r in sel)
        return {
            "n": len(sel),
            "done_pct": pct(b_done, len(sel)),
            "delivered": b_deliv,
            "delivered_pct": pct(b_deliv, len(sel)),
            "compile_pct": pct(b_pcc, b_judged),
            "chars_median": chars[len(chars) // 2] if chars else 0,
        }

    by_lang = {
        "python": bucket([r for r in rows if (r.get("code_lang") or "").lower() in ("python", "py")]),
        "rust": bucket([r for r in rows if (r.get("code_lang") or "").lower() in ("rust", "rs")]),
    }

    src = {}
    for r in rows:
        k = r.get("compile_source") or "-"
        src[k] = src.get(k, 0) + 1

    s = {
        "tag": tag, "rounds": n,
        "compile_source": src,
        "done": len(done), "done_pct": pct(len(done), n),
        "with_code": len(with_code), "code_pct": pct(len(with_code), n),
        "delivered": len(delivered), "delivered_pct": pct(len(delivered), n),
        "mode_dist": mode_dist,
        "mode_truncated": mode_cut,
        "by_lang": by_lang,
        "compiled": len(compiled),
        "compile_judged": len(tried),
        "compile_pct": pct(len(compiled), len(tried)),
        "agents_avg": round(sum(r["agents"] for r in rows) / n, 2),
        "multi_agent_pct": pct(sum(1 for r in rows if r["agents"] >= 2), n),
        "handoff_total": sum(r["handoff"] for r in rows),
        "retries_total": sum(r["retries"] for r in rows),
        "tokens_total": sum(tok),
        "token_median": tok[n // 2] if tok else 0,
        "elapsed_p50": lat[n // 2] if lat else 0,
        # P95 取 min(下标, n-1)：小样本（n=2）时 int(n*0.95)-1 会指到 lat[0]，
        # 算出 P95 比 P50 还小（实测 smoke5：70.75 < 269.89），分位数倒挂会误导判断
        "elapsed_p95": lat[min(int(round(n * 0.95)), n - 1)] if lat else 0,
        "failure_reasons": why,
        "collab_dist": collab,
    }
    return s


def write_report(s, tag):
    EVID.mkdir(parents=True, exist_ok=True)
    p = EVID / f"soak-{tag}.md"
    lines = [
        f"# 大型项目实测报告 · {tag}",
        "",
        f"> 由 `scripts/soak_project.py` 生成，样本 {s['rounds']} 轮。判据见脚本头。",
        "",
        "| 指标 | 读数 |",
        "| --- | --- |",
        f"| 任务真正完成（succeeded） | **{s['done']} / {s['rounds']} = {s['done_pct']}%** |",
        f"| 代码达交付标准（delivered） | **{s['delivered']} / {s['rounds']} = {s['delivered_pct']}%** |",
        f"| 产出里有代码块 | {s['with_code']} / {s['rounds']} = {s['code_pct']}% |",
        f"| 代码通过语法/编译检查 | **{s['compiled']} / {s['compile_judged']} = {s['compile_pct']}%**（其余无代码或环境缺 rustc） |",
        f"| 编译判定来源 | local {s.get('compile_source', {}).get('local', 0)} 条 / worker 自检 {s.get('compile_source', {}).get('worker', 0)} 条 |",
        f"| 平均出场 agent 数 | {s['agents_avg']} |",
        f"| 出现 2 个以上 agent 的任务 | {s['multi_agent_pct']}% |",
        f"| 跨 agent 交接总次数 | {s['handoff_total']} |",
        f"| 重试过的步骤总数 | {s['retries_total']} |",
        f"| token 合计 / 中位数 | {s['tokens_total']} / {s['token_median']} |",
        f"| 端到端耗时 P50 / P95 | {s['elapsed_p50']}s / {s['elapsed_p95']}s |",
        "",
        "## 失败归因",
        "",
    ]
    if s["failure_reasons"]:
        lines += ["| 状态 | 条数 |", "| --- | --- |"]
        for k, v in sorted(s["failure_reasons"].items(), key=lambda x: -x[1]):
            lines.append(f"| `{k}` | {v} |")
    else:
        lines.append("没有失败任务。**这不等于系统完美**——只说明在本次任务集与本地模型下没撞到硬失败，")
        lines.append("仍需看上面的\"代码通过率\"与下方的协作分布来判断产出质量。")
    lines += ["", "## 协作分布（每任务出场 agent 数 → 任务数）", "", "| agent 数 | 任务数 |", "| --- | --- |"]
    for k in sorted(s["collab_dist"]):
        lines.append(f"| {k} | {s['collab_dist'][k]} |")

    # 语言分层：这一节是"框架好不好用"和"模型够不够用"的分界线
    lines += ["", "## 按语言分层", "", "| 语言 | 样本 | 完成率 | 达交付标准 | 编译通过 | 代码字符中位数 |", "| --- | --- | --- | --- | --- | --- |"]
    for name, b in (s.get("by_lang") or {}).items():
        if not b:
            lines.append(f"| {name} | 0 | - | - | - | - |")
            continue
        lines.append(
            "| %s | %d | %s%% | %d（%s%%） | %s%% | %d |" % (
                name, b["n"], b["done_pct"], b["delivered"], b["delivered_pct"],
                b["compile_pct"], b["chars_median"]))
    lines += ["", "## 产出模式分布（worker 自报的 mode）", "",
              f"> 已按标准取值归一。其中 {s.get('mode_truncated', 0)} 条是采集时被截断的残片（如 `mo`），"
              "按前缀归入标准值，残片数量本身就是采集健康度指标。",
              "", "| mode | 次数 |", "| --- | --- |"]
    for k, v in sorted((s.get("mode_dist") or {}).items(), key=lambda x: -x[1]):
        lines.append(f"| `{k}` | {v} |")

    lines += [
        "",
        "## 怎么读这份报告",
        "",
        "- **完成率**高但**代码通过率**低 = 系统答得漂亮、生成的东西跑不起来。这是最危险的一种\"好用\"。",
        "- **agent 数恒为 1** = 多 agent 协作没真发生，编排层退化成了单 agent 串行。",
        "- **交接次数为 0** = 只有口头分工，没有实际接力。",
        "- **重试数为 0** = 要么模型一次就对（不太可能），要么错误纠错没被触发（需查熔断阈值）。",
        "",
        "原始逐轮数据见同目录 `soak-%s.jsonl`。" % tag,
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def selftest() -> int:
    """评测台自己的体检。改尺子必须先验尺子。

    为什么单独做：这份脚本判的是"系统好不好用"，它一旦判错，结论就是假的。
    改完就重跑这里，比等 1000 轮跑完再发现口径错了便宜得多。
    """
    fails = []

    def ck(name, cond, got=""):
        if not cond:
            fails.append(f"{name}（实际 {got!r}）")
        print(("  OK  " if cond else "  FAIL") + f"  {name}" + ("" if cond else f" -> {got!r}"))

    # 1. 标签缺失的 Python 代码不能被当成 Rust 送 rustc（这就是旧版 62% 的由来）
    lang, body, src = extract_code("说明\n```\ndef f():\n    return 1\n```\n")
    ck("无标签 Python 嗅探为 py", lang == "py" and src == "sniff", (lang, src))

    # 2. 明确标 rust 的走 label
    lang, body, src = extract_code("```rust\nfn main() { println!(\"hi\"); }\n```")
    ck("rust 标签识别", lang == "rs" and src == "label", (lang, src))

    # 3. 取最长块，不是第一个
    lang, body, src = extract_code("```py\nprint(1)\n```\n```py\ndef g():\n    import os\n    return os.sep\n```")
    ck("取最长代码块", "def g" in (body or ""), body)

    # 4. 判定不了的语言不能硬判死
    lang, _, _ = extract_code("```\nhello world\n```")
    ck("无语义内容判为 unknown", lang == "unknown", lang)

    # 5. 真编译：合法 Python 过、非法 Python 不过
    ok, _ = compile_check("py", "def f():\n    return 1\n")
    ck("合法 Python 通过", ok is True, ok)
    ok, _ = compile_check("py", "def f(:\n")
    ck("非法 Python 不通过", ok is False, ok)

    # 6. mode 残片归一
    for raw, want, cut in (("mo", "model?(歧义)", True), ("model-unveri", "model-unverified", True),
                           ("model-failed", "model-failed", False), ("weird-new-mode", "weird-new-mode", False)):
        got, was_cut = normalize_mode(raw)
        ck(f"mode 归一 {raw} -> {want}", got == want and was_cut == cut, (got, was_cut))

    print(f"\n[selftest] {len(fails)} 项失败" if fails else "\n[selftest] 全绿")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default=None)
    ap.add_argument("--port", type=int, default=8191)
    ap.add_argument("--rounds", type=int, default=12)
    ap.add_argument("--users", type=int, default=4, help="账号数（每个账号独立 60 次/小时配额）")
    ap.add_argument("--quota", type=int, default=0, help="显式放宽提交配额；0=用服务端默认")
    ap.add_argument("--llm-base", default=os.environ.get("SA_LLM_BASE", "http://127.0.0.1:11434/v1"))
    ap.add_argument("--model", default=os.environ.get("SA_LLM_MODEL", "minicpm5-2b:f16"))
    ap.add_argument("--task-timeout", type=int, default=600)
    ap.add_argument("--max-tokens", type=int, default=2400,
                    help="单次生成 token 上限。2B 写 1500 字符代码约 700 token，"
                         "压到 1600 能把单轮墙钟拉短一半，长跑时用得上")
    ap.add_argument("--suite", default="all", choices=["all", "py", "rs"],
                    help="任务集：py=Python 项目（模型交得出的那半），"
                         "rs=Rust 项目（2B 的能力天花板），all=混合按项目轮转")
    ap.add_argument("--tag", default="baseline")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--keep", action="store_true", help="跑完不删数据目录（排查用）")
    ap.add_argument("--selftest", action="store_true", help="只体检评测台自身判据，不起服务")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    out = EVID / f"soak-{args.tag}.jsonl"

    if args.report_only:
        s = summarize(out, args.tag)
        if s:
            p = write_report(s, args.tag)
            print(json.dumps(s, ensure_ascii=False, indent=2))
            print("报告：%s" % p)
        return 0

    exe = soak.resolve_exe(args.exe)
    data_dir = pathlib.Path(tempfile.mkdtemp(prefix="sa_soak_proj_"))
    # 必须用 prepare_data 把 workers/ 复制进 runner 目录：process 型 runner 是按
    # 脚本名在 SA_RUNNER_DIR 里找 <agent>.py 的，空目录会让第一步就红成
    # "未找到该 agent 的脚本"，后面所有步再因前置失败集体短路——
    # 那种红是环境没搭好，不是系统不好用，不能记进样本。
    runner_dir = soak.prepare_data(data_dir, keep=True)

    env_extra = {"SA_LLM_BASE": args.llm_base, "SA_LLM_MODEL": args.model,
                 "SA_LLM_TIMEOUT": "300", "SA_LLM_MAX_TOKENS": str(args.max_tokens)}
    if args.quota:
        env_extra["SA_TASK_SUBMIT_QUOTA_PER_HOUR"] = str(args.quota)

    print("[SOAK] exe=%s" % exe)
    print("[SOAK] model=%s base=%s" % (args.model, args.llm_base))
    print("[SOAK] rounds=%d users=%d quota=%s" % (args.rounds, args.users, args.quota or "默认"))

    if not soak.probe_model_ready(args.llm_base, args.model):
        print("[SOAK] 模型不可用，先启动 ollama 并确认 %s 存在" % args.model)
        return 3

    proc = soak.launch(exe, args.port, data_dir, runner_dir, env_extra)
    base = "http://127.0.0.1:%d" % args.port
    try:
        if not soak.wait_ready(base, budget=120.0):
            print("[SOAK] 服务没起来，看 %s" % (data_dir / "server.log"))
            return 2
        users = ["soak_p%d" % i for i in range(args.users)]
        for u in users:
            soak.ensure_account(base, u)
        soak.set_active(users[0])

        done = load_done(out) if args.resume else {}
        if done:
            print("[SOAK] 续跑：已有 %d 轮样本，从下一个未完成轮继续" % len(done))

        goals = build_goals(args.rounds, args.suite)
        t0 = time.time()
        ok_n = 0
        for item in goals:
            rnd = item["round"]
            if rnd in done:
                continue
            # 账号轮转：每个账号自带配额，避免把限流误记成功能缺陷
            user = users[rnd % len(users)]
            soak.set_active(user)
            row = soak.run_task(base, item["goal"], None, "%s-r%d" % (args.tag, rnd),
                                timeout=args.task_timeout)
            j = judge(row)
            j.update({"round": rnd, "project": item["project"], "stage": item["stage"],
                      "cycle": item["cycle"], "user": user,
                      "goal": item["goal"][:120]})
            append_jsonl(out, j)
            ok_n += 1 if j["done"] else 0
            el = time.time() - t0
            print("[%4d/%d] %-14s st=%-9s agents=%d code=%s comp=%s %.0fs 累计完成 %d"
                  % (rnd, args.rounds, item["project"], j["status"], j["agents"],
                     j["code_chars"] or "-", j["compiles"], el, ok_n), flush=True)
    finally:
        soak.stop(proc)
        if not args.keep:
            pass                       # 目录留到最后一次排查用，不主动删（样本比磁盘重要）

    s = summarize(out, args.tag)
    if s:
        p = write_report(s, args.tag)
        print("\n" + json.dumps(s, ensure_ascii=False, indent=2))
        print("报告：%s" % p)
        print("原始数据：%s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
