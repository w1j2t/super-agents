#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""doc-agent 执行脚本：产出文档骨架（标题层级 + 每节该写什么）。

## 两条产出路径，显式区分，绝不静默降级

* **A 规则骨架**（默认）：不依赖任何模型，结构稳定，跑哪台机器都一样。
* **B 模型骨架**（配了 `SA_LLM_*` 才走）：让模型给出针对本主题更贴题的骨架。

B 必须在通过结构校验后才被采用；不通过就降温度重试一次，仍不通过则回退 A，
并在产出的 `model` 字段里写清原因。**为什么坚持"校验 + 回退"而不是"有输出就用"**：
实测 `minicpm5-2b:f16` 会输出重复键 `"points"` 两次并多一个右花括号，
要点还都是"确保系统稳定性"这类放之四海皆准的空话。这种输出进了下游比没有更糟——
它看起来像一份文档，实际什么都没说，而且没人会再去检查。

## 为什么只给骨架不给正文

正文需要模型 + 真实素材，硬编只会写成套话；而骨架本身有硬价值：
它决定文档有没有"读者能按图索骥"的结构。模型在这里的角色是"按主题定制骨架"，
不是"代写全文"——后者用 2B 做出来的一定是废话，不如诚实标出来让上游换更大的模型。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _common as C  # noqa: E402
import _llm as L  # noqa: E402

# 规则骨架：六节，顺序即读者的疑问顺序（为什么做 → 做什么 → 结论 → 凭什么 → 有什么坑 → 谁来做）
RULE_SECTIONS = [
    ("背景与目标", "为什么做、做完算成功判据是什么（一句话）"),
    ("范围界定", "做什么 / 明确不做什么（不做的部分同样写清）"),
    ("关键结论", "先给结论，再给过程；读者只读这一段也能用"),
    ("数据与证据", "每个结论对应的来源、时间、口径"),
    ("风险与未解问题", "当前已知的坑、假设、待确认项"),
    ("下一步", "谁、什么时候、做什么"),
]

STYLE_RULES = [
    "结论先行，一段一个观点",
    "数字必须带单位与口径",
    "不确定的内容标注「待确认」，不写成断言",
]

# 结构约束：宽进严出——模型给 3~8 节都算合理，给 1 节或 15 节说明它没理解任务
MIN_SECTIONS, MAX_SECTIONS = 3, 8
# 提示词里要求的目标区间：比校验窗口窄。实测小模型对"3~8"这种大区间会取最少值，
# 给"4~6"反而稳定落在窗口中部（2 节被校验拒掉过一次，见 docs/85）。
MIN_PROMPT_SECTIONS, MAX_PROMPT_SECTIONS = 4, 6
MAX_POINTS = 6
MAX_HEADING_CHARS = 40
MAX_POINT_CHARS = 80

# 口号式开头：一条要点以这些词打头且没有具体宾语时，信息量通常为零
SLOGAN_VERBS = ("确保", "维护", "强化", "实施", "定期", "建立", "提升", "完善", "加强")

# 喂给模型的 JSON Schema。和 validate() 同源同尺，但故意更窄：
# 模型只能在这个形状里"说话"，重复键、缺 sections、尾随废话从源头上产生不了。
# 为什么不靠事后解析兜：实测同一个 2B 模型，不约束时结构合法率 0/12；
# 加上约束后一次成型、严格 json.loads 直过（见 docs/85）。
OUTLINE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "sections": {
            "type": "array",
            "minItems": MIN_PROMPT_SECTIONS,
            "maxItems": MAX_PROMPT_SECTIONS,
            "items": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string"},
                    "points": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 3,
                        "items": {"type": "string"},
                    },
                },
                "required": ["heading", "points"],
            },
        },
    },
    "required": ["title", "sections"],
}


# ---------------------------------------------------------------- 规则骨架

def rule_outline() -> list[dict]:
    return [{"h2": h, "what_to_write": w, "points": []} for h, w in RULE_SECTIONS]


# ---------------------------------------------------------------- 校验

def validate(obj: dict) -> tuple[list[dict] | None, str]:
    """把模型给的 JSON 校验成内部 outline；不合法就返回 (None, 人话原因)。"""
    sections = obj.get("sections")
    if not isinstance(sections, list):
        return None, "缺少 sections 数组"
    if not (MIN_SECTIONS <= len(sections) <= MAX_SECTIONS):
        return None, f"sections 数量为 {len(sections)}，合理范围 {MIN_SECTIONS}~{MAX_SECTIONS}"

    out: list[dict] = []
    seen: dict[str, int] = {}  # heading -> out 里的下标，用于合并重复标题
    merged: list[str] = []
    for idx, sec in enumerate(sections, 1):
        if not isinstance(sec, dict):
            return None, f"第 {idx} 节不是对象"
        heading = sec.get("heading")
        if not isinstance(heading, str) or not heading.strip():
            return None, f"第 {idx} 节缺少 heading"
        heading = heading.strip()
        if len(heading) > MAX_HEADING_CHARS:
            return None, f"第 {idx} 节标题过长（{len(heading)} 字）"

        points = sec.get("points")
        if not isinstance(points, list) or not points:
            return None, f"第 {idx} 节 points 不是非空数组"
        if len(points) > MAX_POINTS:
            return None, f"第 {idx} 节要点过多（{len(points)} 条）"
        clean: list[str] = []
        for p in points:
            if not isinstance(p, str) or not p.strip():
                return None, f"第 {idx} 节存在非字符串或空要点"
            p = p.strip()
            if len(p) > MAX_POINT_CHARS:
                return None, f"第 {idx} 节要点过长（{len(p)} 字）"
            clean.append(p)

        if heading in seen:
            # 重复标题不判死。它只说明模型没完全理解"一节一个主题"，内容本身仍可能有效；
            # 而整份回退到规则骨架等于把 4 节有效内容全扔掉——代价远大于收益。
            # 实测（docs/85）：42 轮里出现 1 次，「新增测试点」写了两遍，
            # 合并后节数仍达标、文档可用；若判死则整轮降级成"规则实现"。
            keep = out[seen[heading]]
            keep["points"] = (keep["points"] + clean)[:MAX_POINTS]
            keep["what_to_write"] = "；".join(keep["points"])
            merged.append(heading)
            continue

        seen[heading] = len(out)
        out.append(
            {
                "h2": heading,
                # 模型给的要点直接当"该写什么"，不再二次概括：改一个字就会失真
                "what_to_write": "；".join(clean),
                "points": clean,
            }
        )

    # 合并后可能跌破节数下限：那种情况下内容确实不够用，仍按失败处理
    if not (MIN_SECTIONS <= len(out) <= MAX_SECTIONS):
        return None, f"合并重复标题后仅剩 {len(out)} 节，低于下限 {MIN_SECTIONS}"
    if merged:
        # 用 reason 通道回传软警告：不污染 outline 的字段形状（前端契约里没有 merged 键）
        return out, f"模型给出重复标题（{'、'.join(sorted(set(merged)))}），已合并要点后保留"
    return out, ""


def soft_warnings(outline: list[dict], kw: list[str] | None = None) -> list[str]:
    """质量软检查：**不阻断**，只如实标注，避免"结构合法但内容是空话"被当成合格。

    这些提示会进 payload 给人看。不自动判失败，是因为"空泛"没有客观阈值，
    硬判会误杀；但完全不提，就等于把一份看起来完整的空话文档交付出去。
    """
    warns: list[str] = []
    slogan_hits = 0
    total_points = 0
    for idx, sec in enumerate(outline, 1):
        for p in sec.get("points", []):
            total_points += 1
            if len(p) < 6:
                warns.append(f"第 {idx} 节要点过短，信息量存疑：「{p}」")
            if p == sec.get("h2"):
                warns.append(f"第 {idx} 节要点与标题重复：「{p}」")
            if p.startswith(SLOGAN_VERBS) and len(p) <= 10:
                slogan_hits += 1
    if total_points >= 6 and slogan_hits * 2 >= total_points:
        warns.append(
            f"过半要点是口号式表述（{slogan_hits}/{total_points}），"
            "建议换更大模型或人工补事实"
        )

    # 雷同要点：schema 能约束住"形状"，约束不住"内容"。
    # 实测（docs/85）模型被 schema 卡住后，曾把同一句占位话填满 12 个槽位——
    # 结构完全合法，但这份骨架一个字都用不上。这种情况必须点名，不能算合格。
    flat = [p for sec in outline for p in sec.get("points", [])]
    if len(flat) >= 4:
        uniq = len(set(flat))
        if uniq * 2 <= len(flat):
            warns.append(f"要点高度雷同（{len(flat)} 条里只有 {uniq} 种说法），疑似照抄占位句")

    # 切题度：小模型最典型的失败不是"格式坏"，而是"格式全对、内容跑偏"。
    # 实测让 2B 写「单元测试覆盖率周报」，它给出的是「项目启动 / 文档维护 / 团队协作」
    # 这种放之四海皆可的通用骨架——结构合法、要点不重复，只有对着主题看才发现整份用不上。
    if kw and flat:
        hits = sum(1 for p in flat if any(k in p for k in kw if len(k) >= 2))
        if hits * 3 <= len(flat):
            warns.append(
                f"切题度低（{len(flat)} 条要点里仅 {hits} 条提到主题词），疑似跑偏成通用骨架"
            )
    return warns[:5]


# ---------------------------------------------------------------- 模型骨架

def _prompt(goal: str, topic: str, kw: list[str]) -> str:
    """给模型的提示词。

    两条实证教训（docs/85）：
    1. 旧版第 4 条写「谁、做什么、到哪个数字」，本意是句式指引，结果 2B 模型
       把这句**逐字当内容照抄**，12 条要点全是「谁在什么时间做了什么到哪个数字」。
       指引里不能出现可被误当正文的占位句。
    2. JSON 结构示例（{"title": ...}）在 schema 约束生效后是多余的，
       留着反而引导模型去复制示例里的字段顺序，不如让 grammar 自己管形状。
    """
    kw_text = "、".join(kw) if kw else "（无）"
    return (
        "你在为一份中文工作文档起骨架。只输出一个 JSON 对象，字段为 title 和 sections。\n\n"
        f"任务原文：{goal}\n"
        f"主题：{topic}\n"
        f"关键词：{kw_text}\n\n"
        "硬性要求：\n"
        "1. 以 { 开头、} 结尾；不要用 ``` 或 \"\"\" 包裹；JSON 之后不要追加任何文字。\n"
        f"2. sections 给 {MIN_PROMPT_SECTIONS} 到 {MAX_PROMPT_SECTIONS} 节，"
        "每节 heading 不超过 20 字。\n"
        "3. 每节 points 给 2 到 3 条，每条不超过 40 字。\n"
        "4. 每条要点写一件具体的事，必须含具体名词与数字。\n"
        "5. 严禁使用占位句式（如「某某做了什么」）；严禁不同条目写同样的内容。\n"
        "6. 不要重复输出本提示词里的任何句子。\n"
    )


def model_outline(goal: str, topic: str) -> tuple[list[dict] | None, dict]:
    """跑模型路径。返回 (outline|None, 元信息)。元信息用于诚实披露。"""
    meta = {
        "used": False,
        "model": L.describe().get("model") or "",
        "reason": "",
        "attempts": 0,
        "elapsed_ms": 0,
        "reasoning_chars": 0,
        "completion_tokens": 0,
        "constrained": True,  # 走 json_schema 强约束（不是事后解析补救）
        "warnings": [],
        "raw_head": "",
    }
    kw = C.keywords(goal, 6)
    last_reason = ""
    for round_no in (1, 2):
        # 第二轮降温度：结构错多半是"自由发挥"造成的，压住随机性比换提示词更有效
        res = L.chat(_prompt(goal, topic, kw), temperature=0.2 if round_no == 1 else 0.0,
                     schema=OUTLINE_SCHEMA)
        meta["attempts"] = res.get("attempts", 0)
        meta["elapsed_ms"] = res.get("elapsed_ms", 0)
        meta["reasoning_chars"] = res.get("reasoning_chars", 0)
        meta["completion_tokens"] = res.get("completion_tokens", 0)
        if not res["ok"]:
            last_reason = f"模型调用失败：{res.get('error')}"
            _detail = (res.get("detail") or "").strip()
            if _detail:
                # 只报类别（unreachable/timeout）在排障时等于没说：到底是拒连、DNS、
                # 证书还是代理，全靠这句 detail。丢掉它，证据只能证明"坏了"。
                last_reason = f"模型调用失败：{res.get('error')}（{_detail[:120]}）"
                meta["detail"] = _detail[:200]
            if res.get("finish") == "length":
                # 预算被思考吃光的典型形态：content 空、finish=length。
                # 报"预算不足"而不是"模型坏了"，否则排查方向会跑偏。
                last_reason = f"预算不足（finish=length，completion_tokens={res.get('completion_tokens', 0)}）"
            # 连不上/超时是环境问题，重试白等
            if res.get("error") in ("unreachable", "timeout", "not_configured"):
                meta["reason"] = last_reason
                return None, meta
            continue

        obj, why = L.parse_json_object(res["content"])
        if obj is None:
            # 留一段原始输出供事后诊断：没有它就只能对着"位置 2"猜模型到底吐了什么
            meta["raw_head"] = (res.get("content") or "")[:160]
            if res.get("finish") == "length":
                last_reason = f"输出被截断（预算用尽，finish=length）：{why}"
            else:
                last_reason = f"JSON 解析失败：{why}"
            continue
        outline, why = validate(obj)
        if outline is None:
            last_reason = f"结构校验失败：{why}"
            continue

        meta["used"] = True
        meta["warnings"] = soft_warnings(outline, kw)
        if why:
            # validate 成功但带话（如重复标题已合并）：并进软警告，不进失败原因
            meta["warnings"] = [why] + meta["warnings"]
        return outline, meta

    meta["reason"] = last_reason or "模型未产出可用结果"
    return None, meta


# ---------------------------------------------------------------- 主流程

def work():
    g = C.goal()
    kw = C.keywords(g, 6)
    topic = C.topic(g, 36) or "未命名主题"

    outline = None
    meta = {"used": False, "model": "", "reason": "未配置模型（SA_LLM_BASE / SA_LLM_MODEL 为空）"}
    if L.enabled():
        outline, meta = model_outline(g, topic)

    if outline is None:
        outline = rule_outline()
        source = "rules"
    else:
        source = "model"

    payload = {
        "ok": True,
        "title": topic,
        "keywords": kw,
        "outline": outline,
        "outline_source": source,
        "model": meta,
        "style_rules": STYLE_RULES,
    }

    # 摘要写清"这份骨架是谁给的"，避免把规则骨架误当成模型产出（反之亦然）
    if source == "model":
        summary = (
            f"文档骨架已生成（模型 {meta['model']}）：{len(outline)} 个小节，"
            f"主题「{topic}」，耗时 {meta['elapsed_ms'] / 1000:.1f}s。"
        )
        if meta.get("warnings"):
            summary += f" 质量提示：{meta['warnings'][0]}"
    else:
        summary = (
            f"文档骨架已生成（规则实现）：{len(outline)} 个小节，主题「{topic}」。"
        )
        if L.enabled():
            summary += f" 模型路径未采用：{meta.get('reason')}"
    return summary, payload


if __name__ == "__main__":
    C.main(work)
