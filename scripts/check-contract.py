#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check-contract.py —— 文档与源码一致性检查器

作用：把 docs/40-api-contract-ts.md 里的每个 `export interface X {...}`
与 src/ 下真实 Rust 结构体 `pub struct X {...}` 的字段集合逐个比对，
把"文档写了但源码没有的字段""源码有但文档漏写的字段"全部打出来。

为什么需要它：
  凭记忆写的前端类型最典型的症状就是字段名/字段数对不上。
  这个脚本把"我核对过了"变成"任何人跑一遍就能核对"。

用法：
  python scripts/check-contract.py                     # 默认检查仓库自带的那份文档
  python scripts/check-contract.py --root D:\\proj     # 指定项目根
  python scripts/check-contract.py --doc docs/40-api-contract-ts.md
退出码：0 = 全部一致；1 = 存在差异（可直接用于 CI）
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

# 纯 TypeScript 层的组合类型（后端没有同名结构，属正常）
PURE_TS = {
    "ProblemBody", "TaskList", "MemoryList", "UpsertResult", "AuditList",
    "Role", "UserStatus", "TaskType", "ModelProfile", "TaskStatus",
    "MemoryScope", "Permission", "OnboardingStatus",
}


def parse_rust_structs(src_root: pathlib.Path) -> dict[str, tuple[set[str], str]]:
    out: dict[str, tuple[set[str], str]] = {}
    for p in src_root.rglob("*.rs"):
        txt = p.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"pub struct (\w+)\s*(?:<[^>]*>)?\s*\{", txt):
            name = m.group(1)
            i, depth, buf = m.end(), 1, []
            while i < len(txt) and depth > 0:
                ch = txt[i]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        break
                buf.append(ch)
                i += 1
            fields = set(re.findall(r"^\s*pub (\w+)\s*:", "".join(buf), re.M))
            if fields and name not in out:  # 同名只取第一处
                out[name] = (fields, f"{p.parent.name}/{p.name}")
    return out


def parse_ts_interfaces(doc_path: pathlib.Path) -> dict[str, list[str]]:
    doc = doc_path.read_text(encoding="utf-8")
    out: dict[str, list[str]] = {}
    for m in re.finditer(r"export interface (\w+)\s*\{", doc):
        name = m.group(1)
        i, depth, buf = m.end(), 1, []
        while i < len(doc) and depth > 0:
            ch = doc[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            buf.append(ch)
            i += 1
        fields = re.findall(r"^\s{2}(\w+)\??\s*:", "".join(buf), re.M)
        if fields:
            out[name] = fields
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(pathlib.Path(__file__).resolve().parent.parent))
    ap.add_argument("--doc", default="docs/40-api-contract-ts.md")
    a = ap.parse_args()

    root = pathlib.Path(a.root)
    src_root, doc_path = root / "src", root / a.doc
    for p in (src_root, doc_path):
        if not p.exists():
            print(f"[FAIL] 路径不存在: {p}")
            return 1

    rust = parse_rust_structs(src_root)
    ts = parse_ts_interfaces(doc_path)
    print(f"源码结构体: {len(rust)} 个 | 文档 interface: {len(ts)} 个\n")

    ok = bad = miss = 0
    for name, tfields in ts.items():
        if name in PURE_TS:
            continue
        if name not in rust:
            print(f"[X] {name}: 源码中找不到同名 pub struct —— 文档疑似凭空命名")
            miss += 1
            continue
        rfields, loc = rust[name]
        dup = [f for f in set(tfields) if tfields.count(f) > 1]
        only_r = sorted(rfields - set(tfields))
        only_t = sorted(set(tfields) - rfields)
        if only_r or only_t or dup:
            bad += 1
            print(f"[X] {name}  ({loc})")
            if only_r:
                print(f"      源码有、文档缺: {only_r}")
            if only_t:
                print(f"      文档有、源码无: {only_t}  <-- 前端会照此写出不存在的字段")
            if dup:
                print(f"      文档内重复字段: {sorted(set(dup))}")
        else:
            ok += 1

    print(f"\n结果: 一致 {ok} | 有差异 {bad} | 查无同名 {miss}")
    return 0 if bad == 0 and miss == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
