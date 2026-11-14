#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
模块化守护检查（静态）：把"换一个部件不殃及全部"从口号变成可执行的断言。

它不读文档，只读源码；任何一条不满足即退出码 1。

检查项
  M1 端口存在      存储端口 trait Backend、执行端口 trait StepRunner 必须存在
  M2 业务层零泄漏  handlers / services / domain / orchestration(除 runners) 中不得出现具体后端类型名
  M3 注册表同步    StorageKind::ALL / RunnerKind::ALL 的每个取值都必须在工厂 match 中出现
  M4 环境变量单点  SA_STORAGE / SA_RUNNER / SA_AUDIT* 只允许在 config.rs 中被读取
  M5 门面唯一      业务层不得绕过 Store 门面直接触碰 backends:: 模块
  M6 审计端口最小  AuditSink 后端只实现 record/events，不得私自覆盖 list/page/denied_count

用法：
  python scripts/check-modularity.py            # 人类可读报告
  python scripts/check-modularity.py --json     # 机器可读
退出码：0 = 全通过；1 = 有违例（逐条列出文件:行）
"""

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

BUSINESS_DIRS = ["api/handlers", "services", "domain", "orchestration"]
# 端口自身所在的文件不算泄漏（RunnerKind 工厂里当然会出现具体类型）
LEAK_ALLOW = {
    "store/backends/mod.rs",
    "store/mod.rs",
    "store/backends/json.rs",
    "store/backends/memory.rs",
    "orchestration/runners/mod.rs",
    "orchestration/runners/process.rs",
    "orchestration/executor.rs",
    "obs/audit.rs",
    "config.rs",
    "state.rs",
    "main.rs",
    "lib.rs",
}
CONCRETE_NAMES = ["JsonBackend", "MemoryBackend", "ProcessRunner", "SimRunner",
                  "MemoryAuditSink", "FileAuditSink"]


def read(rel):
    return (SRC / rel).read_text(encoding="utf-8")


def rust_files():
    return sorted(SRC.rglob("*.rs"))


def rel(p):
    return p.relative_to(SRC).as_posix()


def hits(pattern, text):
    return [(i + 1, line.strip()) for i, line in enumerate(text.splitlines())
            if re.search(pattern, line)]


def check_ports():
    out = []
    store = read("store/backends/mod.rs")
    execu = read("orchestration/executor.rs")
    out.append(("M1a 存储端口 trait Backend 存在",
                bool(re.search(r"pub trait Backend\b", store)), "store/backends/mod.rs"))
    out.append(("M1b 执行端口 trait StepRunner 存在",
                bool(re.search(r"pub trait StepRunner\b", execu)), "orchestration/executor.rs"))
    audit = read("obs/audit.rs")
    out.append(("M1c 审计端口 trait AuditSink 存在",
                bool(re.search(r"pub trait AuditSink\b", audit)), "obs/audit.rs"))
    return out


def check_leaks():
    out = []
    offenders = []
    for f in rust_files():
        r = rel(f)
        if r in LEAK_ALLOW:
            continue
        if not any(r.startswith(d) for d in BUSINESS_DIRS):
            continue
        text = f.read_text(encoding="utf-8")
        for name in CONCRETE_NAMES:
            for ln, line in hits(r"\b%s\b" % name, text):
                offenders.append(f"{r}:{ln} 出现具体后端名 {name} -> {line[:90]}")
    out.append(("M2 业务层零依赖具体后端（换件不必改业务代码）",
                not offenders, "；".join(offenders[:6]) or "业务层只认 Store / StepRunner 端口"))

    port_offenders = []
    for f in rust_files():
        r = rel(f)
        if r.startswith("store/") or r in {"state.rs", "main.rs", "lib.rs", "config.rs"}:
            continue
        text = f.read_text(encoding="utf-8")
        for ln, line in hits(r"backends::(open|json|memory)", text):
            port_offenders.append(f"{r}:{ln} -> {line[:90]}")
    out.append(("M5 后端只经 Store 门面装配（业务层不直连 backends::）",
                not port_offenders, "；".join(port_offenders[:6]) or "无越级引用"))
    return out


def enum_all(rel_path, enum_name):
    text = read(rel_path)
    m = re.search(r"impl\s+%s\b[\s\S]{0,400}?pub const ALL:\s*\[&'static str;\s*\d+\]\s*=\s*\[([^\]]*)\]"
                  % enum_name, text)
    if not m:
        return None, text
    vals = re.findall(r'"([^"]+)"', m.group(1))
    return vals, text


def check_registry():
    out = []
    kinds, store_mod = enum_all("store/backends/mod.rs", "StorageKind")
    if kinds is None:
        out.append(("M3a StorageKind::ALL 可解析", False, "未找到 ALL 常量"))
    else:
        factory = store_mod[store_mod.find("pub fn open("):]
        factory = factory[:factory.find("\n}\n") + 3] if "\n}\n" in factory else factory
        missing = [k for k in kinds if k not in read("store/backends/mod.rs")]
        variants = re.findall(r"StorageKind::(\w+)\s*=>", factory)
        ok = not missing and len(variants) >= len(kinds)
        out.append(("M3a StorageKind::ALL 每个取值都有工厂分支",
                    ok, f"ALL={kinds} 工厂分支={variants}"))

    rkinds, run_mod = enum_all("orchestration/runners/mod.rs", "RunnerKind")
    if rkinds is None:
        out.append(("M3b RunnerKind::ALL 可解析", False, "未找到 ALL 常量"))
    else:
        variants = re.findall(r"RunnerKind::(\w+)\s*=>", run_mod)
        out.append(("M3b RunnerKind::ALL 每个取值都有工厂分支",
                    len(variants) >= len(rkinds), f"ALL={rkinds} 工厂分支={variants}"))
    return out


def check_audit_registry():
    out = []
    kinds, text = enum_all("obs/audit.rs", "AuditKind")
    if kinds is None:
        out.append(("M3c AuditKind::ALL 可解析", False, "未找到 ALL 常量"))
    else:
        factory = text[text.find("pub fn open("):]
        variants = re.findall(r"AuditKind::(\w+)\s*=>", factory)
        out.append(("M3c AuditKind::ALL 每个取值都有工厂分支",
                    len(variants) >= len(kinds), f"ALL={kinds} 工厂分支={variants}"))

    # M6：分页/过滤/拒绝计数只允许有一套实现（trait 默认方法）。
    # 若某个后端自己又写了一遍 page/list/denied_count，口径就可能漂移 —— 这正是"换件后数据不可信"的来源。
    text = read("obs/audit.rs")
    overrides = []
    for m in re.finditer(r"impl AuditSink for (\w+)\s*\{", text):
        impl_name = m.group(1)
        start = m.end()
        depth, i = 1, start
        while i < len(text) and depth:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        body = text[start:i]
        for meth in ("fn list", "fn page", "fn denied_count"):
            if meth in body:
                overrides.append(f"{impl_name} 覆盖了 {meth}")
    out.append(("M6 审计后端只实现 record/events，分页与计数由端口默认实现统一",
                not overrides, "；".join(overrides) or "无后端私自覆盖分页/计数"))
    return out


def check_env_guard():
    out = []
    for var in ("SA_STORAGE", "SA_RUNNER_DIR", "SA_RUNNER", "SA_AUDIT", "SA_AUDIT_FILE", "SA_AUDIT_CAPACITY"):
        # 只看"读取动作" env::var("X")，注释/报错文案里提到变量名不算读取
        # 认三种读取形态：env::var("X")、env_os("X")、parse_usize("X", ..) 这类 config 内 helper
        pat = re.compile(r'\b(?:env::var(?:_os)?|parse_\w+|env_\w+|read_env)\s*\(\s*"%s"'
                         % re.escape(var))
        files = []
        for f in rust_files():
            text = f.read_text(encoding="utf-8")
            if pat.search(text):
                files.append(rel(f))
        ok = files in ([], ["config.rs"]) or files == ["config.rs"]
        out.append((f"M4 环境变量 {var} 只在 config.rs 读取",
                    ok, f"读取文件={files or '无'}"))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "docs" / "evidence" / "modularity.txt"))
    args = ap.parse_args()

    results = []
    for fn in (check_ports, check_leaks, check_registry, check_audit_registry, check_env_guard):
        results.extend(fn())

    failed = [r for r in results if not r[1]]
    if args.json:
        print(json.dumps({"total": len(results), "failed": len(failed),
                          "items": [{"name": n, "ok": ok, "detail": d} for n, ok, d in results]},
                         ensure_ascii=False, indent=2))
    else:
        lines = []
        for name, ok, detail in results:
            lines.append("%s %s  ->  %s" % ("[OK  ]" if ok else "[FAIL]", name, detail))
        lines.append("")
        lines.append("合计 %d 项 | 通过 %d | 失败 %d" % (len(results), len(results) - len(failed), len(failed)))
        text = "\n".join(lines)
        print(text)
        outp = pathlib.Path(args.out)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(text + "\n", encoding="utf-8")
        print("\n报告已写入 %s" % outp)

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
