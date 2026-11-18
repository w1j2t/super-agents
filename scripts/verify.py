#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键验收：把"后端到底能不能用"压缩成一条命令 + 一张总账。

为什么需要它
------------
单测、冒烟、部件矩阵、契约一致性分散在四个脚本里，靠人逐个记着跑，
迟早会漏一个——而漏掉的往往正是会出问题的那一环（比如契约漂移）。
本脚本按固定顺序全跑一遍，任何一环失败都退出非 0，可以直接当 CI 用。

八道关卡（顺序有讲究：先证"声明与源码一致"，再证"代码能跑"，最后证"端到端真能用"）
  1 OpenAPI 未漂移      docs/openapi.json 必须等于从源码现算出来的
  2 OpenAPI 语义正确    字段集合 / 必填性 / 枚举取值与源码逐个对齐
  3 TS 契约未漂移       docs/40-api-contract-ts.md 的 interface 与源码一致
  4 TS 类型未漂移       generated/types.generated.ts 重新生成后必须无变化
  5 单元测试            cargo test（含取消、限流、部件替换等守护用例）
  6 可执行文件构建       cargo build 出真实二进制（冒烟要跑它，不是模拟）
  7 端到端冒烟          scripts/smoke.py：真起进程、真发 HTTP、真断言
  8 可插拔部件矩阵      scripts/matrix.py：逐案换件（存储/审计/限流/指标/执行）后核心链路仍通

用法
----
    python scripts/verify.py                 # 全跑
    python scripts/verify.py --skip-build    # 已有二进制，跳过 5 与 6（快速回归）
    python scripts/verify.py --build-dir D:\build_sa1

关于 --build-dir
----------------
源码路径含中文时 MinGW 链接会失败（这是环境硬伤，不是代码问题），
所以真正的编译固定在一个纯 ASCII 的副本目录里做。本脚本默认用
`<桌面>/build_sa1`，可用 --build-dir 或环境变量 SA_BUILD_DIR 覆盖。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import pathguard  # noqa: E402  （SA-031：删除前统一过守卫）

ROOT = pathlib.Path(__file__).resolve().parent.parent
# 工具链带空格参数，必须拆成列表交给 subprocess，否则会被当成"一个名字里有空格的可执行文件"→ WinError 2
CARGO = ["cargo", "+stable-x86_64-pc-windows-gnu"]
EXE_REL = pathlib.Path("target-gnu") / "debug" / "super-agents.exe"


def eprint(*a):
    print(*a, file=sys.stderr)


def run(cmd: list[str], cwd: pathlib.Path, env: dict | None = None, timeout: int = 900):
    """返回 (rc, 合并输出)。不抛异常——失败要让总账继续往下走完。"""
    e = os.environ.copy()
    e["PYTHONIOENCODING"] = "utf-8"
    e.pop("SA_RUNNER_DIR", None)  # 防止外部残留污染矩阵用例
    if env:
        e.update(env)
    try:
        p = subprocess.run(cmd, cwd=str(cwd), env=e, capture_output=True,
                           timeout=timeout, text=True, encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"超时（>{timeout}s）"
    except FileNotFoundError as exc:
        return 127, f"命令不存在：{exc}"


def sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "<缺文件>"


class Step:
    """SA-029：三态，不是两态。

    原来 `--skip-build` 把 5/8、6/8 记成 `ok=True`，于是"跳过两步"的总账长得和
    "八步全过"一模一样，退出码还是 0。看报告的人只知道"绿了"，不知道有两步
    根本没跑——跳过被记成通过，是比失败更难发现的一种假绿。
    """

    def __init__(self, name: str, detail: str = "", status: str = "fail"):
        self.name, self.detail, self.status, self.cost = name, detail, status, 0.0

    @property
    def ok(self) -> bool:
        return self.status == "pass"

    @property
    def skipped(self) -> bool:
        return self.status == "skip"

    def __repr__(self):
        flag = {"pass": "通过", "skip": "跳过", "fail": "失败"}[self.status]
        extra = f" — {self.detail}" if self.detail else ""
        return f"[{flag}] {self.name}（{self.cost:.1f}s）{extra}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-dir", default=os.environ.get("SA_BUILD_DIR", ""))
    ap.add_argument("--skip-build", action="store_true", help="跳过 cargo test/build，用现有二进制跑冒烟")
    ap.add_argument("--verbose", action="store_true", help="失败时打印完整输出")
    a = ap.parse_args()

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from _paths import build_dir  # noqa: E402

    # 默认镜像走统一解析：写死的桌面 build_sa1 早已废弃，留着它等于默认指向空目录。
    build = pathlib.Path(a.build_dir) if a.build_dir else build_dir()
    if not build.exists():
        eprint(f"[FAIL] 编译副本目录不存在：{build}")
        eprint("       源码路径含中文时无法直接编译（MinGW 链接器硬伤）。")
        eprint("       请先创建 ASCII 路径副本（Cargo.toml + src/ + scripts/ + docs/ + generated/），")
        eprint("       或用 --build-dir 指向已有的副本。")
        return 2

    # 把源码/脚本/文档同步进副本，保证测的就是当前源码
    for sub in ("src", "scripts", "docs", "generated"):
        src, dst = ROOT / sub, build / sub
        if src.exists():
            if dst.exists():
                # SA-031：--build-dir 指向仓库本身或系统目录时，这句 rmtree 删的
                # 就是真源码。守卫拦下即退出，不降级为"用副本旧内容继续"（那是假绿）。
                try:
                    pathguard.safe_rmtree(dst, base=build,
                                          allow_names={"src", "scripts", "docs", "generated"},
                                          what=f"同步副本 {sub}")
                except pathguard.GuardError as exc:
                    eprint(f"[FAIL] {exc}")
                    eprint("       请检查 --build-dir / SA_BUILD_DIR 是否指到了源码仓库或系统目录。")
                    return 2
            shutil.copytree(src, dst)
    for f in ("Cargo.toml", "Cargo.lock"):
        if (ROOT / f).exists():
            shutil.copy2(ROOT / f, build / f)

    exe = build / EXE_REL
    target_env = {"CARGO_TARGET_DIR": str(build / "target-gnu")}
    steps: list[Step] = []
    outputs: dict[str, str] = {}

    def record(name: str, ok: bool, detail: str = "", out: str = "", cost: float = 0.0,
               skipped: bool = False):
        s = Step(name, detail, "skip" if skipped else ("pass" if ok else "fail"))
        s.cost = cost
        steps.append(s)
        outputs[name] = out
        flag = {"pass": "  OK  ", "skip": "  SKIP", "fail": "  FAIL"}[s.status]
        print(flag + (f" {name} — {detail}" if detail else f" {name}"))
        return ok

    print(f"源码目录：{ROOT}")
    print(f"编译副本：{build}")
    print(f"二进制  ：{exe}")
    print("-" * 78)

    # 1 契约生成未漂移
    t0 = time.time()
    rc, out = run([sys.executable, "scripts/gen-openapi.py", "--check"], build)
    record("1/8 OpenAPI 未漂移（源码现算 == docs/openapi.json）", rc == 0,
           "一致" if rc == 0 else "有差异：需要重新生成 openapi.json", out, time.time() - t0)

    # 2 契约语义正确
    t0 = time.time()
    rc, out = run([sys.executable, "scripts/check-openapi.py"], build)
    m = re.search(r"发现\s*(\d+)\s*处不一致", out)
    record("2/8 OpenAPI 语义正确（字段/必填/枚举与源码对齐）", rc == 0,
           "0 处不一致" if rc == 0 else f"{m.group(1) if m else '若干'} 处不一致", out, time.time() - t0)

    # 3 TS 契约文档一致
    t0 = time.time()
    rc, out = run([sys.executable, "scripts/check-contract.py"], build)
    m = re.search(r"一致\s*(\d+)\s*\|\s*有差异\s*(\d+)", out)
    record("3/8 TS 契约文档一致（docs/40-api-contract-ts.md）", rc == 0,
           (f"{m.group(1)} 个 interface 全部对齐" if m and rc == 0 else "存在字段差异"), out, time.time() - t0)

    # 4 TS 类型未漂移
    t0 = time.time()
    ts_path = build / "generated" / "types.generated.ts"
    before = sha(ts_path)
    rc, out = run([sys.executable, "scripts/gen-ts-types.py"], build)
    after = sha(ts_path)
    record("4/8 TS 类型未漂移（重新生成后无变化）", rc == 0 and before == after,
           "无变化" if before == after else "内容发生变化：请把 generated/types.generated.ts 一并提交",
           out, time.time() - t0)

    if not a.skip_build:
        # 5 单元测试
        t0 = time.time()
        rc, out = run([*CARGO, "test"], build, target_env, timeout=1200)
        m = re.search(r"test result: ok\. (\d+) passed", out)
        record("5/8 单元测试全绿", rc == 0 and bool(m),
               f"{m.group(1)} passed" if m else "编译或断言失败", out, time.time() - t0)

        # 6 二进制构建
        t0 = time.time()
        rc, out = run([*CARGO, "build"], build, target_env, timeout=1200)
        record("6/8 可执行文件构建成功", rc == 0 and exe.exists(),
               str(exe) if exe.exists() else "未产出二进制", out, time.time() - t0)
    else:
        # SA-029：跳过就写"跳过"。另外"省掉编译"不等于"可以不验新鲜度"——
        # 拿一个几天前的 exe 跑冒烟一样会全绿，而测的根本不是当前源码。
        t0 = time.time()
        rc, out = run([sys.executable, "scripts/build-exe.py",
                       "--build-dir", str(build), "--skip-build", "--no-sync"], build)
        record("5/8 二进制新鲜度（--skip-build 也不许省）", rc == 0,
               "与当前源码同版本" if rc == 0 else "二进制落后于源码：先跑一次完整 verify",
               out, time.time() - t0)
        record("6/8 单元测试", False, "--skip-build：本次未跑 cargo test", skipped=True)
        record("6b/8 可执行文件构建", False, "--skip-build：本次未跑 cargo build，用的是现成 exe", skipped=True)

    if not exe.exists():
        record("7/8 端到端冒烟", False, f"缺少二进制 {exe}")
        record("8/8 可插拔部件矩阵", False, "缺少二进制")
    else:
        # 7 端到端冒烟
        t0 = time.time()
        rc, out = run([sys.executable, "scripts/smoke.py", "--exe", str(exe)], build, timeout=900)
        m = re.search(r"合计\s*(\d+)\s*项\s*\|\s*通过\s*(\d+)\s*\|\s*失败\s*(\d+)", out)
        record("7/8 端到端冒烟（真进程 + 真 HTTP）", rc == 0 and bool(m),
               f"{m.group(2)}/{m.group(1)} 项通过" if m else "冒烟失败", out, time.time() - t0)

        # 8 可插拔部件矩阵
        t0 = time.time()
        rc, out = run([sys.executable, "scripts/matrix.py", "--exe", str(exe)], build, timeout=900)
        m = re.search(r"矩阵合计\s*(\d+)\s*项\s*\|\s*通过\s*(\d+)\s*\|\s*失败\s*(\d+)", out)
        record("8/8 可插拔部件矩阵（换件后核心链路仍通）", rc == 0 and bool(m),
               f"{m.group(2)}/{m.group(1)} 项通过" if m else "矩阵失败", out, time.time() - t0)

        # 生成物回写：副本里算出来的就是唯一真源，必须回到源码目录
        for rel in ("docs/openapi.json", "generated/types.generated.ts"):
            s, d = build / rel, ROOT / rel
            if s.exists() and (not d.exists() or sha(s) != sha(d)):
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(s, d)
                print(f"  (已回写生成物：{rel})")

    print("-" * 78)
    passed = sum(1 for s in steps if s.status == "pass")
    skipped = sum(1 for s in steps if s.status == "skip")
    failed = sum(1 for s in steps if s.status == "fail")
    for s in steps:
        print(" ", repr(s))
    print("-" * 78)

    for s in steps:
        if s.status == "fail":
            print(f"\n===== 失败详情：{s.name} =====")
            tail = outputs.get(s.name, "")
            print(tail if a.verbose else "\n".join(tail.splitlines()[-40:]))

    if failed:
        print(f"总账：{len(steps)} 步 — 通过 {passed} / 跳过 {skipped} / 失败 {failed}。")
        return 1
    if skipped:
        # 退出码 3 = "没红，但也没全跑"。调用方脚本据此不能把它当全绿。
        print(f"总账：{len(steps)} 步 — 通过 {passed} / 跳过 {skipped} / 失败 0。")
        print("      跳过 {n} 步（--skip-build），**这不是全绿**：编译与单测本次未验证。".format(n=skipped))
        print("      交付前请去掉 --skip-build 再跑一次。")
        return 3

    print(f"总账：{passed}/{len(steps)} 全部通过 —— 后端在当前状态下是真的可用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
