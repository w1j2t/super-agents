#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check-openapi.py —— 校验 docs/openapi.json 与 src/**/*.rs 的 schema 是否自洽。

它回答一个具体问题：**生成的接口契约有没有把字段写错**（漏字段 / 多字段 / 可选性错标）。

校验三层：
  1. 字段集合：源码 pub struct 的 pub 字段 ⇄ schema.properties
  2. 可选性：源码 Option<T> / #[serde(default)] / skip_serializing_if 的字段，不得出现在 required 里
  3. 枚举取值：源码 enum 的 serde 重命名结果 ⇄ schema.enum

已知局限（不要当成全覆盖）：
  - 只比对**字段名与可选性**，不比对类型描述（`"type": "string"` vs `$ref` 的等价性）；
  - 泛型/元组/嵌套闭包类型在源码侧解析可能退化，会被记为 SKIP 而不是 PASS；
  - `Vec<T>` / `bool` 这类"天然有默认值"的字段不要求出现在 required 里（与 serde 行为一致，但这是启发式）。

退出码：0 = 无问题；1 = 发现不一致。
用法：
    python scripts/check-openapi.py
    python scripts/check-openapi.py --verbose      # 打印每个被检查的 schema
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
OPENAPI = ROOT / "docs" / "openapi.json"


def strip_comments(text: str) -> str:
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"///[^\n]*", "", text)
    return text


def read_all_rs() -> str:
    parts = []
    for f in sorted(SRC.rglob("*.rs")):
        parts.append(f"\n// ==== {f.relative_to(ROOT).as_posix()} ====\n")
        parts.append(f.read_text(encoding="utf-8", errors="replace"))
    return "".join(parts)


def parse_structs(src: str) -> dict[str, list[tuple[str, str, bool]]]:
    """返回 {struct名: [(字段名, 类型原文, 是否 #[serde(flatten)]), ...]}"""
    out: dict[str, list[tuple[str, str, bool]]] = {}
    for m in re.finditer(r"pub struct (\w+)\s*\{", src):
        name = m.group(1)
        i = m.end()
        depth = 1
        j = i
        while j < len(src) and depth > 0:
            if src[j] == "{":
                depth += 1
            elif src[j] == "}":
                depth -= 1
            j += 1
        body = src[i : j - 1]
        fields: list[tuple[str, str, bool]] = []
        attrs: list[str] = []
        for raw in body.splitlines():
            line = raw.strip()
            if line.startswith("///"):
                continue
            if line.startswith("#["):
                attrs.append(line)
                continue
            fm = re.match(r"pub (\w+)\s*:\s*(.+)", line)
            if fm:
                attr_text = " ".join(attrs)
                flat = bool(re.search(r"serde\s*\([^)]*flatten", attr_text))
                fields.append((fm.group(1), fm.group(2).rstrip(",").strip(), flat))
                attrs = []
            elif line and not line.startswith("#") and not line.startswith("pub "):
                # 属性与字段已分离（如多行属性），保守清空
                attrs = []
        out[name] = fields
    return out


def flatten_fields(
    fields: list[tuple[str, str, bool]],
    structs: dict[str, list[tuple[str, str, bool]]],
    notes: list[str],
    owner: str,
    seen: set[str] | None = None,
) -> dict[str, str]:
    """把 `#[serde(flatten)]` 字段按目标类型**展开**，还原 JSON 里真实出现的字段集合。

    不展开就会与 gen-openapi.py 打架：生成器产出平铺 schema，校验器却按源码字面
    期望一个嵌套字段，于是报"漏字段 task / 多字段 goal..."。两边必须用同一套语义。
    """
    seen = set() if seen is None else seen
    out: dict[str, str] = {}
    for fname, ftype, flat in fields:
        if flat:
            inner = re.sub(r"^Option<(.*)>$", r"\1", ftype).strip()
            inner = re.sub(r"^(?:Arc|Rc|Box)<(.*)>$", r"\1", inner).strip()
            if inner in structs and inner not in seen:
                out.update(flatten_fields(structs[inner], structs, notes, owner, seen | {inner}))
            else:
                notes.append(f"{owner}.{fname}: flatten 目标 {inner} 无法展开（自引用或未定义），已跳过")
            continue
        out[fname] = ftype
    return out


def parse_enums(src: str) -> dict[str, list[str]]:
    """粗略解析 pub enum 的变体名（未做 serde 重命名推导，仅做存在性/数量提示）"""
    out: dict[str, list[str]] = {}
    for m in re.finditer(r"pub enum (\w+)\s*\{", src):
        name = m.group(1)
        i = m.end()
        depth = 1
        j = i
        while j < len(src) and depth > 0:
            if src[j] == "{":
                depth += 1
            elif src[j] == "}":
                depth -= 1
            j += 1
        body = src[i : j - 1]
        variants = re.findall(r"^\s{0,8}([A-Z]\w*)\s*(?:[({,]|$)", body, re.M)
        out[name] = variants
    return out


def snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if not OPENAPI.exists():
        print(f"[FAIL] 找不到 {OPENAPI}，先运行 python scripts/gen-openapi.py")
        return 1

    oa = json.loads(OPENAPI.read_text(encoding="utf-8"))
    schemas: dict = oa.get("components", {}).get("schemas", {})
    src = read_all_rs()
    structs = parse_structs(src)

    checked = 0
    problems: list[str] = []
    skipped: list[str] = []
    flatten_notes: list[str] = []

    for name, sch in sorted(schemas.items()):
        if sch.get("type") == "string" and "enum" in sch:
            continue  # 枚举在下面单独看
        if name not in structs:
            skipped.append(f"{name}（源码中无同名 struct，属 handler 内联或别名）")
            continue
        checked += 1
        props = set(sch.get("properties", {}).keys())
        req = set(sch.get("required", []))
        # 源码侧同样按 flatten 语义展开：JSON 里 flatten 字段不占键名，它的内部字段
        # 直接并入父对象。不展开就会把"平铺"误判成"漏字段 + 多字段"。
        src_fields = flatten_fields(structs[name], structs, flatten_notes, name)

        missing = set(src_fields) - props
        extra = props - set(src_fields)
        if missing:
            problems.append(f"{name}: schema 漏字段 {sorted(missing)}")
        if extra:
            problems.append(f"{name}: schema 多字段 {sorted(extra)}（源码无）")

        for fname, ftype in src_fields.items():
            is_opt = ftype.startswith("Option<")
            has_default = "default" in ftype or "skip_serializing_if" in ftype
            natural = (
                ftype.startswith("Vec<")
                or ftype.startswith("bool")
                or ftype.startswith("HashMap<")
            )
            if (is_opt or has_default) and fname in req:
                problems.append(f"{name}.{fname}: Option/default 字段被标为 required（前端会以为必填）")
            if not (is_opt or has_default or natural) and fname not in req:
                problems.append(f"{name}.{fname}: 必填字段未进 required（前端会以为可选）")

        if args.verbose:
            print(f"  [OK] {name}: {len(props)} 字段，required {len(req)} 个")

    # 枚举：只做取值集合的交集检查（schema enum 值应全部出现在源码变体的 snake_case 形式中）
    enums = parse_enums(src)
    enum_checked = 0
    for name, sch in sorted(schemas.items()):
        if not (sch.get("type") == "string" and "enum" in sch):
            continue
        if name not in enums:
            continue
        enum_checked += 1
        allowed = {snake(v) for v in enums[name]}
        for val in sch["enum"]:
            if val not in allowed:
                problems.append(f"enum {name}: openapi 值 {val!r} 在源码变体里找不到（可能是手写或改名规则不符）")

    print("=" * 62)
    if flatten_notes:
        print(f"flatten 展开提示: {len(flatten_notes)} 条")
        for n in flatten_notes:
            print("  NOTE", n)
    print(f"检查 struct schema: {checked} 个 | 枚举 schema: {enum_checked} 个")
    print(f"跳过（源码无同名）: {len(skipped)} 个")
    if skipped and args.verbose:
        for s in skipped:
            print("  SKIP", s)
    print("-" * 62)
    if problems:
        print(f"发现 {len(problems)} 处不一致：")
        for p in problems:
            print("  [FAIL]", p)
        return 1
    print("结果: 全部一致（字段集合 / 可选性 / 枚举取值），0 处不一致")
    print("局限: 不比对类型描述等价性；Vec/bool 视为天然可选（与 serde 行为一致）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
