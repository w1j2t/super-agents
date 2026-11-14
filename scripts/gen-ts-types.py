#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen-ts-types.py —— 从 Rust 源码生成 TypeScript 类型（零侵入，不改后端代码）

为什么用"生成"而不是手写：
  手写的 types.ts 会随后端改动而漂移，而漂移是前端联调最贵的 bug。
  这里直接从 src/**/*.rs 的 `pub struct` / `pub enum` 推出来，
  后端一改、重跑一次，前端类型就同步了 —— 不需要人工核对。

用法：
  python scripts/gen-ts-types.py                       # 生成到 generated/types.generated.ts
  python scripts/gen-ts-types.py --out 某路径.ts
  python scripts/gen-ts-types.py --check               # 只比对不写盘，有差异退出码 1

已知边界（脚本会打印 WARN，不会瞎猜）：
  · 只处理"纯单元变体"的 enum；带数据的 enum 会被跳过并警告；
  · 类型无法识别时退化为 `unknown` 并保留原始 Rust 类型作注释；
  · 不解析 `#[serde(skip)]`（本仓库未使用），遇到 `skip_serializing_if` 会把该字段标为可选。
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

PRIM = {
    "String": "string", "str": "string", "bool": "boolean",
    "u8": "number", "u16": "number", "u32": "number", "u64": "number", "usize": "number",
    "i8": "number", "i16": "number", "i32": "number", "i64": "number", "isize": "number",
    "f32": "number", "f64": "number",
}
WARN: list[str] = []

# 这些是运行时内部结构（锁、频率桶、存储句柄），不构成 HTTP 契约，生成到前端只会误导。
SKIP_STRUCTS = {
    "Hist", "Bucket", "RateLimiter", "Metrics", "Store", "Db", "AuditSink",
    "MetricsSnapshot", "Claims", "AuthService", "Config",
    # 可插拔部件的实现类：不是 HTTP 契约，生成到前端只会误导
    "MemoryAuditSink", "FileAuditSink", "AuditKind",
}


def camel_to(s: str, style: str) -> str:
    words = re.findall(r"[A-Z][a-z0-9]*|[a-z0-9]+", s)
    low = [w.lower() for w in words]
    if style == "lowercase":
        return "".join(low)
    if style == "SCREAMING_SNAKE_CASE":
        return "_".join(w.upper() for w in low)
    if style == "kebab-case":
        return "-".join(low)
    return "_".join(low)  # snake_case 及默认


def map_type(t: str) -> str:
    t = t.strip()
    t = re.sub(r"^&\s*(?:'\w+\s*)?", "", t)          # &str / &'static str
    t = re.sub(r"^'\w+\s+", "", t)                    # 生命周期
    t = re.sub(r"\s+", " ", t)

    m = re.match(r"^Option<(.*)>$", t)
    if m:
        return f"{map_type(m.group(1))} | null"
    m = re.match(r"^Vec<(.*)>$", t)
    if m:
        inner = map_type(m.group(1))
        return f"({inner})[]" if "|" in inner else f"{inner}[]"
    m = re.match(r"^(?:HashMap|BTreeMap)<(.*)>$", t)
    if m:
        kv = split_top(m.group(1))
        if len(kv) == 2:
            k = map_type(kv[0])
            if k in ("string", "number"):
                return f"Record<{k}, {map_type(kv[1])}>"
            WARN.append(f"Map 键类型 `{kv[0]}` 不能作 TS 索引 → 已退化为 unknown")
            return f"unknown /* {t} */"
    m = re.match(r"^\[(.*)\]$", t)                    # &[T] / &'static [T]
    if m:
        return f"{map_type(m.group(1))}[]"
    m = re.match(r"^(?:Arc|Rc|Box|Mutex|RwLock|RefCell)<(.*)>$", t)
    if m:
        return map_type(m.group(1))
    if t.startswith("DateTime<") or t.startswith("NaiveDateTime"):
        return "string"
    if t in ("serde_json::Value", "Value", "JsonValue"):
        return "unknown"
    if t in PRIM:
        return PRIM[t]
    if re.match(r"^[A-Za-z_][\w:]*$", t):
        return t.split("::")[-1]
    if t.startswith("(") :                              # 元组
        parts = [p for p in re.split(r",\s*", t.strip("()")) if p]
        return "[" + ", ".join(map_type(p) for p in parts) + "]"
    WARN.append(f"未识别类型 `{t}` → 已退化为 unknown")
    return f"unknown /* {t} */"


def split_top(body: str) -> list[str]:
    """按顶层逗号切分（忽略 <> 与 () 内部）。"""
    out, depth, cur = [], 0, []
    for ch in body:
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur))
    return out


def block(txt: str, start: int) -> tuple[str, int]:
    """从 start（左花括号后一个字符）读到配对的右花括号。"""
    depth, i, buf = 1, start, []
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
    return "".join(buf), i


def preceding_attrs(txt: str, pos: int) -> str:
    """取结构体/枚举声明前紧邻的属性块文本。"""
    head = txt[:pos]
    lines = head.splitlines()
    attrs: list[str] = []
    for ln in reversed(lines):
        s = ln.strip()
        if s.startswith("#[") or s.startswith("///") or s.startswith("//") or s == "":
            attrs.append(s)
            # 继续往上最多 12 行
            if len(attrs) > 12:
                break
        else:
            break
    return "\n".join(reversed(attrs))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(pathlib.Path(__file__).resolve().parent.parent))
    ap.add_argument("--out", default="generated/types.generated.ts")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    root = pathlib.Path(a.root)
    src = root / "src"
    if not src.exists():
        print(f"[FAIL] 找不到 {src}")
        return 1

    chunks: list[str] = []
    # 只取对外契约相关的模块：领域模型 + 接口层（视图/入参）。
    # 其余（store / metrics / config / orchestration 内部结构）不暴露给前端，生成出来只会造成噪音。
    ONLY_DIRS = ("domain", "handlers", "security", "services", "obs")
    for p in sorted(src.rglob("*.rs")):
        if not any(part in ONLY_DIRS for part in p.parts):
            continue
        rel = p.relative_to(root).as_posix()
        txt = p.read_text(encoding="utf-8", errors="replace")
        local: list[str] = []

        # ---- enum ----
        for m in re.finditer(r"pub enum (\w+)\s*\{", txt):
            name = m.group(1)
            if name in SKIP_STRUCTS:      # 内部实现枚举，不对外暴露
                continue
            attrs = preceding_attrs(txt, m.start())
            body, _ = block(txt, m.end())
            variants, ok = [], True
            for v in split_top(body):
                v = re.sub(r"#\[[^\]]*\]", "", v)
                v = "\n".join(ln for ln in v.splitlines() if not ln.strip().startswith("//"))
                v = v.strip()
                if not v:
                    continue
                mm = re.match(r"^(\w+)\s*$", v)
                if not mm:
                    ok = False
                    break
                variants.append(mm.group(1))
            if not ok:
                WARN.append(f"{rel}: enum {name} 带数据变体，已跳过（请手工维护）")
                continue
            if not variants:
                continue
            style = "snake_case"
            ms = re.search(r'rename_all\s*=\s*"([^"]+)"', attrs)
            if ms:
                style = ms.group(1)
            union = " | ".join(f"'{camel_to(v, style)}'" for v in variants)
            local.append(f"export type {name} = {union};")

        # ---- struct ----
        for m in re.finditer(r"pub struct (\w+)\s*(?:<[^>]*>)?\s*\{", txt):
            name = m.group(1)
            if name in SKIP_STRUCTS:      # 内部实现结构，不对外暴露
                continue
            body, _ = block(txt, m.end())
            fields: list[str] = []
            for seg in split_top(body):
                raw = seg.strip()
                if not raw:
                    continue
                attrs_part, rest = "", raw
                ma = re.match(r"^((?:#\[[^\]]*\]\s*)*)(.*)$", raw, re.S)
                if ma:
                    attrs_part, rest = ma.group(1), ma.group(2)
                # 去掉字段上方的文档注释放到同一段里造成的前缀（如 "/// 说明" 后跟字段）
                rest = "\n".join(ln for ln in rest.splitlines() if not ln.strip().startswith("//"))
                rest = rest.strip()
                mm = re.match(r"^(?:pub(?:\([^)]*\))?\s+)?(\w+)\s*:\s*(.+)$", rest.strip(), re.S)
                if not mm:
                    continue
                fname, ftype = mm.group(1), mm.group(2).strip()
                ts_type = map_type(ftype)
                opt = "?" if "skip_serializing_if" in attrs_part else ""
                fields.append(f"  {fname}{opt}: {ts_type};")
            if not fields:
                continue
            local.append(f"export interface {name} {{\n" + "\n".join(fields) + "\n}")

        if local:
            chunks.append(f"// ===== 源: {rel} =====\n" + "\n\n".join(local))

    header = (
        "// 本文件由 scripts/gen-ts-types.py 自动生成，请勿手工修改。\n"
        "// 重新生成：python scripts/gen-ts-types.py\n"
        "// 类型来源：src/**/*.rs 的 pub struct / pub enum（字段名与枚举取值均按源码推导）\n\n"
    )
    out_text = header + "\n\n".join(chunks) + "\n"

    # ---- 重名体检：TS 的同名 interface 会"声明合并"，两个不同 Rust 结构撞名会静默串味 ----
    declared = re.findall(r"^export (?:interface|type) (\w+)", out_text, re.M)
    dup = sorted({n for n in declared if declared.count(n) > 1})
    if dup:
        print("[FAIL] 生成结果出现重名声明（TS 会自动合并，导致前端字段串味）：")
        for d in dup:
            print("  -", d)
        print("  处理方式：给其中一个 Rust 结构改一个更具体的名字（如 ListQuery → TaskListQuery / MemoryListQuery）。")
        return 1

    out_path = root / a.out
    if a.check:
        old = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
        same = old.strip() == out_text.strip()
        print("CHECK:", "一致" if same else "有差异（请重新生成）")
        return 0 if same else 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(out_text, encoding="utf-8")
    n_iface = out_text.count("export interface ")
    n_union = out_text.count("export type ")
    print(f"已生成 {out_path}")
    print(f"interface {n_iface} 个 | 联合类型 {n_union} 个 | 行数 {len(out_text.splitlines())}")
    if WARN:
        print("\nWARN:")
        for w in sorted(set(WARN)):
            print("  -", w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
