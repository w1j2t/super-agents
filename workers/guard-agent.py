#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""guard-agent 执行脚本：对目标文本做一次真实的风险扫描。

与 Rust 侧 `REDLINES` 两道闸门并存是有意的：
  * Rust 闸门在执行前判定，命中直接 fatal，是**硬底线**，任何后端都绕不过；
  * 本脚本在 guard 步骤里再扫一遍，词表更宽（正则级），用于发现 Rust 词表没覆盖的形态，
    并把结果写进审计。它只报告、不阻断——阻断权在治理层，脚本不得越权改判。

重要细节：命中后**不回显原文**，只输出类型与掩码。
在审计日志里再抄一份敏感串，等于把泄露面从输入扩散到日志。
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _common as C  # noqa: E402

# (类型, 正则, 掩码方式)
PATTERNS = [
    ("身份证号", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "keep_tail4"),
    ("银行卡号", re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "keep_tail4"),
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "keep_tail4"),
    ("邮箱", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "keep_domain"),
    ("API 密钥", re.compile(r"(sk-[A-Za-z0-9]{12,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{12,})"), "hide_all"),
    ("明文口令", re.compile(r"(密码|口令|password|passwd|pwd)\s*[:=：]\s*\S+", re.I), "hide_all"),
]

# 与 Rust REDLINES 同义的处置类词（越权 / 违法意图）
INTENT_WORDS = ["窃取", "盗取", "绕过登录", "未经授权访问", "撞库", "爆破", "洗钱", "伪造证件"]


def mask(kind: str, raw: str) -> str:
    if kind == "hide_all":
        return "***已隐藏***"
    if kind == "keep_domain" and "@" in raw:
        return "***@" + raw.split("@", 1)[1]
    return ("***" + raw[-4:]) if len(raw) > 4 else "***"


def work():
    g = C.goal()
    hits = []
    for kind, pat, how in PATTERNS:
        for m in pat.finditer(g):
            hits.append({"type": kind, "masked": mask(how, m.group(0)), "pos": m.start()})
    intents = [w for w in INTENT_WORDS if w in g]

    blocked = bool(intents) or any(h["type"] in ("身份证号", "明文口令") for h in hits)
    payload = {
        "ok": not blocked,
        "blocked": blocked,
        "sensitive_hits": hits,
        "intent_flags": intents,
        "note": "本脚本只报告，不阻断；阻断权在治理层 Executor 的红线闸门",
    }
    if blocked:
        reason = intents[0] if intents else hits[0]["type"]
        summary = (
            f"风险扫描命中（{reason}），建议阻断本任务；"
            f"命中项 {len(hits)} 处，意图词 {len(intents)} 个。"
        )
    elif hits:
        summary = f"未命中处置类意图，但检测到 {len(hits)} 处敏感信息形态，产物中不得回显原文。"
    else:
        summary = "风险扫描通过：未命中敏感信息形态与处置类意图。"
    return summary, payload


if __name__ == "__main__":
    C.main(work)
