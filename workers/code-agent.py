#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""code-agent —— 编程开发 agent（真生成代码 / 语法自检 / 失败自修）

## 这次改造的起因（务必留着，别删）

原版这个 agent **不生成代码**：它只从目标里认技术栈、列一张边界条件检查表，
就返回 ok=True。实测证据在 docs/evidence/soak/soak-baseline.jsonl ——
"写一个 Rust 存档模块"这类任务被判 succeeded，全程 0 次模型调用、0.4 秒结束、
产出一行代码都没有。

任务状态是绿的，交付物是空的。这种"假绿"比直接失败更危险：它会让上层
以为活干完了，实际什么也没落。所以这里做三件事：

1. **真生成**：走 `_llm` 真调模型，json_schema 强约束输出结构（2B 模型的
   结构不合法率不加约束时是 12/12，加了才可用）。
2. **真检查**：拿到代码不是结束，要过语法关。Python 用 `compile()`，
   Rust 用 `rustc --crate-type lib --emit=metadata` 真编译。
3. **真自修**：编译不过就把**错误原文**回喂给模型重写，最多 3 轮。
   这一步是"错误纠错"能力在代码路径上的落地——只报错不自修的 agent
   等于把问题原样丢回给用户。

## 一个必须说清的口径

Rust 代码里 `use axum::...` 这类外部依赖，单独拿 rustc 编译必然报
"unresolved import"。那是**依赖没供给**，不是作者的语法写错了。
把两者混在一起统计，会让"代码通过率"这个数字彻底失真。所以这里按
rustc 的错误码分流：只有依赖类错误码（E0431/E0432/E0433/E0463）时
视为"语法通过但依赖未供给"，并在 detail 里写明。宁可把口径写细，
也不要给出一个看起来漂亮、实际没意义的通过率。

## 降级纪律

模型没配 / 连不上 / 超预算时，如实返回 `code=None` + `mode="rules-only"` +
失败原因，绝不编一段假代码顶上。上层看到 mode 就知道"这轮没真干活"。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import _common as C  # noqa: E402
import _llm as L  # noqa: E402


STACK = {
    "rust": "Rust", "cargo": "Rust", "axum": "Rust", "tokio": "Rust",
    "python": "Python", "脚本": "Python", "py": "Python",
    "js": "JavaScript/TypeScript", "ts": "JavaScript/TypeScript",
    "typescript": "JavaScript/TypeScript", "react": "React", "vue": "Vue",
    "go": "Go", "java": "Java", "sql": "SQL", "docker": "Docker",
    "k8s": "Kubernetes", "fastapi": "FastAPI",
}

EDGE = [
    ("空输入", "入参为空串/空数组/零值时的行为必须定义，不得崩"),
    ("超长输入", "超长字符串或大数组要有上限与拒绝策略"),
    ("并发", "同一资源被并发访问时的一致性保证"),
    ("幂等", "重复提交同一请求的结果必须与一次相同"),
    ("编码", "非 UTF-8 / 含 BOM / 含 emoji 的文本处理"),
    ("时区", "跨时区的时间计算不得依赖本地时区"),
    ("失败可观测", "出错必须留下可诊断信息，不能静默吞掉"),
]

CODE_HINTS = ("写", "实现", "生成", "代码", "脚本", "函数", "模块", "接口",
              "修", "改", "补", "重构", "测试", "编译", "bug", "报错", "死循环",
              # 以下这组是补的：第一版用 Rust 数据结构任务实测，目标里没有"写/实现"
              # 这类动词，wants_code 被误判成 False，等于把"要代码的活"当"不要代码的活"。
              "定义", "结构", "数据结构", "设计", "类", "枚举", "类型", "schema")

SCHEMA = {
    "type": "object",
    "properties": {
        "language": {"type": "string", "enum": ["python", "rust"]},
        "filename": {"type": "string"},
        "code": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": ["language", "filename", "code", "notes"],
    "additionalProperties": False,
}

SYSTEM = ("你是代码生成器。只输出一个 JSON 对象，不要任何解释、不要 Markdown 包裹、"
          "不要输出 JSON 以外的任何字符。")

RUSTC = pathlib.Path(os.path.expanduser("~")) / ".cargo" / "bin" / "rustc.exe"
DEP_CODES = {"E0431", "E0432", "E0433", "E0463"}   # can't find crate / unresolved import
CODE_CAP = 200_000                                  # 单份代码上限，防 payload 撑爆


# ---------------------------------------------------------------- 判语言

def pick_language(goal: str) -> str:
    """从目标里判语言。判不出时按任务味道兜底，而不是硬塞一个。

    "后端/接口/中间件"这类活默认 Rust —— 本项目的后端就是 Rust，
    让它默认写 Python 等于替用户改主意。
    """
    low = goal.lower()
    if any(k in low for k in ("rust", "axum", "cargo", "tokio", "编译器")):
        return "rust"
    if any(k in low for k in ("python", "脚本", "pytest", "py ", "csv", ".py")):
        return "python"
    if any(k in goal for k in ("后端", "接口", "中间件", "限流", "路由", "状态机", "结算")):
        return "rust"
    return "python"


# ---------------------------------------------------------------- 提示词

def _prompt(goal: str, lang: str) -> str:
    """生成提示词。

    **别在提示词里出现 `<完整代码>` 这种占位符字面量。**
    实测（docs/evidence/soak/codegen-modes.jsonl）：第一版提示词写了
    `\"code\":\"<完整代码>\"`，2B 模型直接把 `...` 照抄进 code 字段，
    三个点还是合法 Python（Ellipsis），语法检查照样绿。去掉占位字面量、
    改成描述性要求后，同一模型同一任务输出了 2152 字符、43 行真代码。
    对小模型来说，示例里的占位符就是答案。
    """
    if lang == "python":
        shape = ("单个 .py 文件，含必要的 import 与 main 入口，能被 python 直接解析。")
        plan = "按「导入 → 数据结构 → 核心函数 → 入口」的顺序写。"
    else:
        shape = ("完整 Rust 代码，含必要的 use 与 struct/fn 定义，能被 rustc 解析。"
                 "外部依赖（如 axum）可以正常 use，缺依赖不影响判分。")
        # 2B 写 Rust 极容易直接放弃（实测输出就三个点），给它一个可执行的骨架顺序，
        # 比说"写完整代码"有用得多。
        plan = ("先写 use，再写 struct 并逐字段给出类型，然后写 impl 或 fn，"
                "最后按需写 main。每一步都要落到真实代码，不要留空。")
    return (
        f"任务：{goal}\n\n"
        f"要求：\n"
        f"1. 用 {lang} 写出完整可用的实现，不要伪代码，不要 TODO，不要用省略号代替内容。\n"
        f"2. {shape}\n"
        f"3. {plan}\n"
        f"4. 字段/函数要写全，注释用中文简短说明关键判断。\n"
        f"5. 只输出一个 JSON 对象，四个字段：language（值为 {lang}）、"
        f"filename（文件名）、code（这里是**完整代码正文**）、notes（一两句说明做了什么）。\n"
        f"6. code 里的换行写成 \\n 转义，字符串内双引号写成 \\\"。\n"
    )


def _fix_prompt(goal: str, lang: str, code: str, err: str) -> str:
    """自修提示词。**必须把上一版代码带进来**。

    第一版只说了"错误是什么"，没给代码。实测结果是模型每轮都凭空重写，
    而且第 2、3 轮直接把 language 改成 python —— 它在猜，不是在修。
    把上一版代码摆进去、明说"只修这个问题、语言保持 {lang}"，修才落得住。
    """
    body = (code or "").strip()
    if len(body) > 1500:
        body = body[:1500] + "\n...（后面还有内容，已截断）"
    head = f"上一版代码：\n{body}\n\n" if body else ""
    return (
        f"任务：{goal}\n\n"
        f"{head}"
        f"这份代码没通过检查，问题原文：\n{err}\n\n"
        f"请只修掉上面这个问题，其余结构保持不变，语言必须仍然是 {lang}。\n"
        f"重新输出完整代码（不是补丁片段，也不要只写注释说明你打算怎么改）。\n"
        f"只输出一个 JSON 对象，四个字段：language、filename、code、notes。\n"
    )


# ---------------------------------------------------------------- 语法检查

def _balance(code: str) -> tuple[bool, str]:
    """括号/引号粗配平。作为真编译前的快速筛，抓最常见的"生成到一半断了"。"""
    stack = []
    pairs = {")": "(", "]": "[", "}": "{"}
    in_s = None
    esc = False
    for i, ch in enumerate(code):
        if in_s:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == in_s:
                in_s = None
            continue
        if ch in ("'", '"'):
            in_s = ch
        elif ch in "([{":
            stack.append(ch)
        elif ch in ")]}":
            if not stack or stack[-1] != pairs[ch]:
                return False, f"第 {i} 字符处 {ch} 不配对"
            stack.pop()
    if stack:
        return False, f"结尾有 {len(stack)} 个未闭合的 {''.join(stack[-3:])}"
    if in_s:
        return False, "字符串引号未闭合"
    return True, ""


PLACEHOLDER = {"...", "…", "pass", "todo", "tbd", "<complete code>", "<完整代码>", "code", "none"}


def substantive(lang: str, code: str) -> tuple[bool, str]:
    """判"到底写了没有"。这是语法检查补不上的那一刀。

    `compile("...")` 是合法的（Ellipsis 是合法表达式），`fn main(){}` 也是合法
    Rust —— 语法检查只能证明"这段文本不是乱码"，证明不了"它实现了需求"。
    实测里 2B 把 code 填成三个点，语法检查全绿，这是最隐蔽的一种假绿。

    这里用可解释的三条硬线：长度过线、有定义（def/class/fn/struct/impl）、
    去掉注释后还有足够的实质行。过了不保证能用，但过不了肯定不能用。
    """
    c = (code or "").strip()
    if not c:
        return False, "代码为空"
    if c.lower().strip("` \n.") in PLACEHOLDER:
        return False, "占位符：%r" % c[:40]
    if len(c) < 80:
        return False, "仅 %d 字符" % len(c)
    pat = r"^\s*(def |class )" if lang == "python" else r"^\s*(fn |struct |impl |enum |trait )"
    if not re.search(pat, c, re.M):
        return False, "无函数/结构定义"
    body = [ln for ln in c.splitlines()
            if ln.strip() and not ln.strip().startswith(("#", "//"))]
    if len(body) < 6:
        return False, "实质行数仅 %d" % len(body)
    return True, "ok(%d 行)" % len(body)


def _unwrap(code: str) -> str:
    """剥掉模型给代码多包的一层引号。

    实测（tag=rust-struct 第 2 次）：2B 输出了
        "use std::collections::HashMap;\n\nstruct GameSystem {...}
    开头多一个裸引号、结尾没有配对的 —— 于是 balance 检查报"字符串未闭合"，
    一份 39 行、内容完全可用的 Rust 代码被判死。

    这不是框架的错，但框架该有这一层：小模型在 JSON 字符串里写代码时，
    引号纪律经常崩。剥掉再验一遍的成本是 0，收益是把"能用的代码"救回来。
    只在原样检查已经失败时才用，绝不改写本来就通过的代码。
    """
    c = (code or "").strip()
    if len(c) >= 2 and c[0] == '"' and c[-1] == '"':
        return c[1:-1]
    if c.startswith('"') and not c.rstrip().endswith('"'):
        return c.lstrip('"')
    return c


def syntax_check(lang: str, code: str) -> dict:
    """真检查。返回 {ok, method, detail}。ok=None 表示环境缺工具、无法判定。"""
    if not code.strip():
        return {"ok": False, "method": "empty", "detail": "代码为空"}

    bal_ok, bal_detail = _balance(code)
    if not bal_ok:
        return {"ok": False, "method": "balance", "detail": bal_detail}

    if lang == "python":
        try:
            compile(code, "<generated>", "exec")
            return {"ok": True, "method": "python:compile", "detail": "语法树解析通过"}
        except SyntaxError as exc:
            return {"ok": False, "method": "python:compile",
                    "detail": f"{exc.msg} (第 {exc.lineno} 行)"}
        except Exception as exc:                      # noqa: BLE001
            return {"ok": False, "method": "python:compile",
                    "detail": f"{type(exc).__name__}: {exc}"}

    # rust
    exe = str(RUSTC) if RUSTC.exists() else "rustc"
    try:
        with tempfile.TemporaryDirectory() as td:
            src = pathlib.Path(td) / "probe.rs"
            src.write_text(code, encoding="utf-8")
            # --crate-type lib：不要求有 main，只做解析与类型检查，不链接
            res = subprocess.run(
                [exe, "--edition", "2021", "--crate-type", "lib",
                 "--emit=metadata", "-o", str(pathlib.Path(td) / "p.rmeta"), str(src)],
                capture_output=True, text=True, timeout=90)
            err = (res.stderr or "")
            if res.returncode == 0:
                return {"ok": True, "method": "rustc:lib", "detail": "编译通过"}
            codes = set(re.findall(r"error\[(E\d+)\]", err))
            first = ""
            for line in err.splitlines():
                if line.startswith("error"):
                    first = line.strip()
                    break
            if codes and codes <= DEP_CODES:
                # 只是外部 crate 没供给，代码本身语法成立
                return {"ok": True, "method": "rustc:lib",
                        "detail": "语法通过；外部依赖未供给（%s）" % ",".join(sorted(codes))}
            if not codes and "error" not in err:
                return {"ok": None, "method": "rustc:lib",
                        "detail": "rustc 未给出可解析错误：%s" % err.strip()[:120]}
            return {"ok": False, "method": "rustc:lib",
                    "detail": (first or err.strip().splitlines()[0])[:160]}
    except FileNotFoundError:
        return {"ok": None, "method": "no-rustc", "detail": "环境没有 rustc，无法判定"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "method": "rustc:lib", "detail": "编译超时(90s)"}


# ---------------------------------------------------------------- 模型路径

def model_code(goal: str, lang: str) -> tuple[dict | None, dict]:
    """生成→检查→自修，最多 3 轮。返回 (产物, 元信息)。"""
    meta = {
        "used": False, "model": L.describe().get("model") or "",
        "reason": "", "rounds": 0, "fix_rounds": 0,
        "attempts": 0, "elapsed_ms": 0, "reasoning_chars": 0,
        "completion_tokens": 0, "history": [], "constrained": True,
    }
    prompt = _prompt(goal, lang)
    last_err = ""
    best: dict | None = None

    for rnd in (1, 2, 3):
        res = L.chat(prompt, system=SYSTEM, max_tokens=3200,
                     temperature=0.2 if rnd == 1 else 0.0, schema=SCHEMA)
        meta["rounds"] = rnd
        for k in ("attempts", "elapsed_ms", "reasoning_chars", "completion_tokens"):
            meta[k] += int(res.get(k) or 0)

        if not res["ok"]:
            why = f"模型调用失败：{res.get('error')}"
            if res.get("finish") == "length":
                why = ("预算不足（finish=length，completion_tokens=%s）"
                       % res.get("completion_tokens", 0))
            meta["reason"] = why
            if res.get("error") in ("unreachable", "timeout", "not_configured"):
                return None, meta                    # 环境问题，重试无意义
            meta["history"].append(why)
            continue

        obj, why = L.parse_json_object(res["content"])
        if obj is None:
            last_err = f"JSON 解析失败：{why}"
            meta["history"].append(last_err)
            prompt = _fix_prompt(goal, lang, "", last_err)
            continue

        code = (obj.get("code") or "").strip()
        if not code:
            last_err = "模型返回的 code 为空"
            meta["history"].append(last_err)
            prompt = _fix_prompt(goal, lang, "", last_err)
            continue
        if len(code) > CODE_CAP:
            code = code[:CODE_CAP]
            meta["history"].append("代码超长，已截断到 200KB")

        use_lang = obj.get("language") or lang
        if use_lang not in ("python", "rust"):
            use_lang = lang

        # 语言不符要拦。实测（tag=rust-struct）里模型把 Rust 任务做成了
        # `print('hello')` 并自称 language=python —— 这种"答非所问"如果只看
        # 语法检查（合法）和 ok=True，会被算成一次成功交付。
        if use_lang != lang:
            last_err = "语言不符：要求 %s，模型返回 %s" % (lang, use_lang)
            meta["history"].append("第 %d 轮 —— %s" % (rnd, last_err))
            meta["fix_rounds"] = rnd
            prompt = _fix_prompt(goal, lang, code, last_err)
            continue

        # 顺序不能反：先问"写没写"，再问"语法对不对"。
        # 反过来的话，`...` 会以"语法合法"的身份溜过第一道门。
        sub_ok, sub_why = substantive(use_lang, code)
        if sub_ok:
            chk = syntax_check(use_lang, code)
        else:
            chk = {"ok": False, "method": "substantive", "detail": sub_why}

        # 检查挂了先试着救一次：剥掉多余的包裹引号再验。
        # 只有"救回来的版本本身合格"才替换，绝不把不合格的东西洗成合格。
        repaired = ""
        if sub_ok and chk["ok"] is False:
            alt = _unwrap(code)
            if alt != code:
                sub2, why2 = substantive(use_lang, alt)
                chk2 = (syntax_check(use_lang, alt) if sub2
                        else {"ok": False, "method": "substantive", "detail": why2})
                if sub2 and chk2["ok"] is not False:
                    code, chk, sub_why = alt, chk2, why2
                    repaired = "已剥离模型多加的包裹引号"

        meta["used"] = True
        best = {"language": use_lang,
                "filename": obj.get("filename") or ("main.rs" if use_lang == "rust" else "main.py"),
                "code": code, "notes": obj.get("notes") or "",
                "substantive": {"ok": sub_ok, "detail": sub_why},
                "repaired": repaired,
                "syntax": chk}
        if sub_ok and chk["ok"] is not False:
            return best, meta                        # 通过，或环境无法判定（不硬判红）
        last_err = ("实质内容不足：%s" % sub_why) if not sub_ok else \
                   ("%s：%s" % (chk["method"], chk["detail"]))
        meta["history"].append("第 %d 轮检查未过 —— %s" % (rnd, last_err))
        meta["fix_rounds"] = rnd
        prompt = _fix_prompt(goal, lang, code, last_err)

    meta["reason"] = last_err or "模型未产出可用代码"
    return best, meta                            # 返回最后一版（带失败标记），供上层决策


# ---------------------------------------------------------------- 落盘（可选）

def maybe_write(code_info: dict) -> str:
    """只有显式给了 SA_CODE_OUT 才落盘。

    agent 默认不往磁盘写生成物：写文件属于 SandboxExec 权限范畴，
    该由编排层按 RBAC 决定，而不是每个 agent 自己找地方写。
    """
    out = os.environ.get("SA_CODE_OUT") or ""
    if not out or not code_info:
        return ""
    try:
        d = pathlib.Path(out)
        d.mkdir(parents=True, exist_ok=True)
        f = d / code_info["filename"]
        f.write_text(code_info["code"], encoding="utf-8")
        return str(f)
    except Exception as exc:                          # noqa: BLE001
        return "写入失败：%s" % exc


# ---------------------------------------------------------------- 主流程

def work():
    g = C.goal()
    low = g.lower()
    hit = sorted({v for k, v in STACK.items() if k in low})
    kw = C.keywords(g, 6)
    lang = pick_language(g)
    wants_code = any(h in g for h in CODE_HINTS)

    code_info, meta, mode = None, {"used": False, "reason": "未配置模型"}, "rules-only"
    delivered = False
    if L.enabled():
        code_info, meta = model_code(g, lang)
        if code_info:
            # delivered 才是"真交付"的唯一口径：内容要实、语法要过。
            # 两者缺一，mode 只能是 model-unverified，上层据此降级或重派。
            delivered = bool(code_info.get("substantive", {}).get("ok")) and \
                        code_info["syntax"]["ok"] is True
            if delivered:
                mode = "model" if meta.get("fix_rounds", 0) == 0 else "model-self-fixed"
            else:
                mode = "model-unverified"
        else:
            # 模型跑了但没产出任何可用代码（连占位符都不合格）。
            # 不能沿用 rules-only —— 那是"根本没模型"的语义，两者要分开，
            # 否则排障时会去查模型配置，实际是模型能力不够。
            mode = "model-failed"
    else:
        meta = {"used": False, "model": "",
                "reason": "未配置模型（SA_LLM_BASE / SA_LLM_MODEL 为空）"}

    written = maybe_write(code_info) if (code_info and delivered) else ""

    payload = {
        "ok": True,
        # mode 是给上层看的第一眼信号：
        #   model / model-self-fixed = 真生成了代码且过了检查
        #   model-unverified         = 生成了但检查没过（自修用尽）
        #   rules-only               = 没生成代码，只有清单（绝不能当成交付物）
        "mode": mode,
        "wants_code": wants_code,
        "delivered_code": delivered,
        "stack": hit or ["未识别（需向需求方确认语言与运行环境）"],
        "language": (code_info or {}).get("language", lang),
        "filename": (code_info or {}).get("filename", ""),
        "code": (code_info or {}).get("code", ""),
        "code_chars": len((code_info or {}).get("code", "")),
        "notes": (code_info or {}).get("notes", ""),
        "substantive": (code_info or {}).get("substantive", {}),
        "repaired": (code_info or {}).get("repaired", ""),
        "syntax": (code_info or {}).get("syntax", {}),
        "written_to": written,
        "keywords": kw,
        "checklist": [{"case": c, "requirement": r} for c, r in EDGE],
        "min_tests": max(6, len(EDGE)),
        "model": meta,
    }

    if delivered:
        summary = (f"已生成 {payload['language']} 代码 {payload['code_chars']} 字符"
                   f"（{payload['filename']}），检查通过：{code_info['syntax']['detail']}")
        if mode == "model-self-fixed":
            summary += f"；经 {meta.get('fix_rounds')} 轮自修"
    elif code_info:
        # 失败原因要报**真正失败的那一项**。第一版无脑取 substantive 的 detail，
        # 于是出现"代码未达交付标准：ok(39 行)"这种自相矛盾的结论。
        if (code_info.get("substantive") or {}).get("ok") is False:
            why = (code_info.get("substantive") or {}).get("detail")
        else:
            why = (code_info.get("syntax") or {}).get("detail")
        summary = (f"代码未达交付标准：{why or '未知'}（{payload['code_chars']} 字符，"
                   f"自修 {meta.get('fix_rounds')} 轮用尽），不可当成交付物")
    else:
        summary = (f"未生成代码：{meta.get('reason') or '模型不可用'}；"
                   f"仅输出 {len(EDGE)} 类边界清单，技术栈 {payload['stack'][0]}")

    # 机器可读尾标（实测踩出来的必需件，位置有讲究）：
    # runner 上报的 step output 平台只留**末段约 120 字符**（verify2 第 2 轮从
    # "gator.py mode=model fix=0 |" 开头，前半个 [CODE] 被切掉），所以标记必须放在
    # 结尾——放开头等于把关键字段主动送进垃圾桶，报表就会把"已交付 2680 字符"记成 0。
    mark = ("[CODE] ok=%d lang=%s chars=%d file=%s mode=%s fix=%d" % (
        1 if delivered else 0,
        payload["language"] or "-",
        payload["code_chars"],
        payload["filename"] or "-",
        mode or "-",
        meta.get("fix_rounds") or 0,
    ))
    # 同一份标记再放进 payload 的**最后一个字段**：runner 保留的是输出末段，
    # 而 payload（含代码正文）排在 summary 之后，长任务里整段 summary 都被挤出窗口
    # （verify3 轮 2/3：out 尾巴是类定义，[CODE] 不见踪影）。JSON 保序，放最后必被留下。
    payload["mark"] = mark.replace("[CODE] ", "")
    return summary + " || " + mark, payload


if __name__ == "__main__":
    C.main(work)
