#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""code-agent 执行脚本：从目标里抽出技术栈与实现要点，给出边界条件清单。

不做代码生成（那需要模型），做的是"动手前的检查表"：
遗漏边界条件是新手代码最常见的失败点，这份清单直接对应可执行的检查项。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _common as C  # noqa: E402

STACK = {
    "rust": "Rust", "cargo": "Rust", "python": "Python", "py": "Python",
    "js": "JavaScript", "ts": "TypeScript", "typescript": "TypeScript",
    "react": "React", "vue": "Vue", "go": "Go", "java": "Java",
    "sql": "SQL", "docker": "Docker", "k8s": "Kubernetes", "fastapi": "FastAPI",
}

EDGE = [
    ("空输入", "入参为空字符串 / 空数组 / 空对象时不得 panic，须返回明确错误"),
    ("超长输入", "长度上限之外要截断或拒绝，不能靠内存硬扛"),
    ("并发", "同一资源被并发访问时必须有序或加锁"),
    ("幂等", "重试同一请求不得产生重复副作用"),
    ("编码", "中文路径 / 非 ASCII 内容不得乱码或写盘失败"),
    ("时区", "时间一律带时区，禁止本地时间直接入库"),
    ("失败可观测", "失败必须留下可定位的日志或错误码，不能静默吞掉"),
]


def work():
    g = C.goal()
    low = g.lower()
    hit = sorted({v for k, v in STACK.items() if k in low})
    kw = C.keywords(g, 6)
    payload = {
        "ok": True,
        "stack": hit or ["未识别（需向需求方确认语言与运行环境）"],
        "keywords": kw,
        "checklist": [{"case": c, "requirement": r} for c, r in EDGE],
        "min_tests": max(6, len(EDGE)),
    }
    summary = (
        f"技术栈：{'/'.join(hit) if hit else '未识别'}；"
        f"已列出 {len(EDGE)} 类边界条件，建议最少 {len(EDGE)} 条测试覆盖。"
    )
    return summary, payload


if __name__ == "__main__":
    C.main(work)
