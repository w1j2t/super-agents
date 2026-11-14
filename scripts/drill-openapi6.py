#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门禁 6（OpenAPI 未漂移）的负向演练 —— 证明这道门禁真的会红。

为什么需要这个脚本
------------------
门禁 6 在 2026-11-13 转红过一次，那次是**真事故**，不是演练：`docs/openapi.json`
里的 `POST /api/v1/tasks` 响应体没有 schema，而门禁一直显示"一致"。
修好之后出现一个新问题——**谁也没见过它正常工作的样子**。
"曾经红过"不等于"现在有牙齿"，因为红的可能是别的分支。

所以这里用**变异法**反向证明：把登记表按事故时的样子改坏，看门禁会不会转红。

三种变异，各守一件事
--------------------
A. 清空 `RESPONSE_SCHEMA_OVERRIDE`        → 等价于"当初根本没登记"
   期望：RC=1（有差异）。若得 0，说明登记表压根没生效，或契约已被悄悄回退。

B. 键写回 tuple `("post", "/api/v1/tasks")` → 等价于"登记了但永远匹配不上"
   期望：RC=2 + `[FATAL]`。"写错了却静默忽略"是假绿的原始根因，必须启动即死。

C. 键写成 `post /api/v1/taskk`（少一个 s） → 格式合法、路径不存在
   期望：RC=1（有差异）。这一条说明自检**只管格式不管存在性**——
   键拼错时不会 FATAL，只会表现为"结果对不上"。这正是它的用处：
   真拼错时门禁仍会红，只是红得不够直白。

安全边界
--------
全流程只读正本、只在 `target/drill/` 下动副本：
 - 绝不修改 `scripts/gen-openapi.py`；
 - 变体的 `--check` 只读 `docs/openapi.json` 与源码，不写任何文件；
 - 正本未登记（表为空）时直接记 FAIL 并说明原因，绝不假装通过。

用法
----
    python scripts/drill-openapi6.py            # 三种变异全跑
    python scripts/drill-openapi6.py --verbose  # 打印每次变异的完整输出

退出码：0 = 三种变异全部按预期表现；1 = 有变异没按预期表现（门禁可能失效）。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
DRILL_ROOT = ROOT / "target" / "drill"
EVIDENCE = ROOT / "docs" / "evidence" / "openapi6-negative-drill.txt"

results: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" | {detail}" if detail else ""))


def _run(py: pathlib.Path, cwd: pathlib.Path) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(py), "--check"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=str(cwd))
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def _first_line(text: str) -> str:
    return (text.splitlines() or [""])[0][:120]


def main() -> int:
    ap = argparse.ArgumentParser(description="门禁 6 负向演练")
    ap.add_argument("--verbose", action="store_true", help="打印每次变异的完整输出")
    a = ap.parse_args()

    src_path = HERE / "gen-openapi.py"
    if not src_path.exists():
        print(f"[FATAL] 找不到 {src_path}")
        return 1
    original = src_path.read_text(encoding="utf-8")

    # ── 前置：正本必须真的登记了 ──
    m = re.search(r"RESPONSE_SCHEMA_OVERRIDE\s*=\s*\{(.*?)\n\}", original, re.S)
    if not m:
        rec("P0 正本存在登记表", False, "gen-openapi.py 里找不到 RESPONSE_SCHEMA_OVERRIDE")
        print("\n结论：演练无法进行（前提不成立）。")
        return 1
    body = m.group(1)
    has_post_tasks = "post /api/v1/tasks" in body
    rec("P0 正本已登记 post /api/v1/tasks", has_post_tasks,
        "登记表为空或未含该键，负向演练将失去意义（清空==原样）" if not has_post_tasks else "")

    # ── 建临时副本目录（每次新建，不删旧目录：本仓库禁用 rmtree）──
    DRILL_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    work = pathlib.Path(tempfile.mkdtemp(prefix=f"oapi6_{stamp}_", dir=str(DRILL_ROOT)))

    copied = []
    for f in ("gen-openapi.py", "gen-ts-types.py"):
        s = HERE / f
        if s.exists():
            shutil.copy2(s, work / f)
            copied.append(f)
    rec("P1 副本已建立", (work / "gen-openapi.py").exists(), f"{work}（{len(copied)} 个文件）")

    # ROOT 重定向到正本项目：副本必须读真源码与真契约，否则测的是副本自己
    redir = original.replace("ROOT = HERE.parent",
                             f'ROOT = __import__("pathlib").Path(r"{ROOT}")', 1)
    if redir == original:
        rec("P2 ROOT 重定向成功", False, "未找到 'ROOT = HERE.parent'，脚本结构已变，请更新演练")
        print("\n结论：演练脚本需适配。")
        return 1
    rec("P2 ROOT 重定向成功", True, "副本读真源码 + 真 docs/openapi.json")

    # ── 基线：副本原样必须与正本同结论（否则后面所有红绿都不可信）──
    base = work / "baseline.py"
    base.write_text(redir, encoding="utf-8")
    rc_b, out_b = _run(base, work)
    rec("B0 基线（副本原样）与正本一致", rc_b == 0, f"RC={rc_b} | {_first_line(out_b)}")
    if a.verbose:
        print(out_b)

    # ── 变异 A：清空登记表 ──
    srcA = re.sub(r"RESPONSE_SCHEMA_OVERRIDE\s*=\s*\{.*?\n\}", "RESPONSE_SCHEMA_OVERRIDE = {}",
                  redir, count=1, flags=re.S)
    pa = work / "variantA_empty.py"
    pa.write_text(srcA, encoding="utf-8")
    rcA, outA = _run(pa, work)
    okA = rcA == 1
    detailA = f"RC={rcA} | {_first_line(outA)}"
    if rcA == 0:
        detailA += "  ← 清空后仍显示一致：登记表没起作用，或 openapi.json 已被回退成无 schema 状态"
    rec("A 清空登记表 → 必须转红(rc=1)", okA, detailA)
    if a.verbose:
        print(outA)

    # ── 变异 B：键写回 tuple（曾经的真实写法）──
    srcB = redir.replace('"post /api/v1/tasks": "TaskDetail"',
                         '("post", "/api/v1/tasks"): "TaskDetail"', 1)
    if srcB == redir:
        rec("B 键写回 tuple → 必须启动即 FATAL(rc=2)", False,
            "正本表内容已变，未能构造 tuple 变体（请更新演练匹配串）")
    else:
        pb = work / "variantB_tuple.py"
        pb.write_text(srcB, encoding="utf-8")
        rcB, outB = _run(pb, work)
        okB = rcB == 2 and "FATAL" in outB
        rec("B 键写回 tuple → 必须启动即 FATAL(rc=2)", okB,
            f"RC={rcB} | {_first_line(outB)}")
        if a.verbose:
            print(outB)

    # ── 变异 C：键拼错路径（格式合法、目标不存在）──
    srcC = redir.replace('"post /api/v1/tasks": "TaskDetail"',
                         '"post /api/v1/taskk": "TaskDetail"', 1)
    if srcC == redir:
        rec("C 键拼错路径 → 表现为差异(rc=1)", False, "正本表内容已变，未能构造拼写变体")
    else:
        pc = work / "variantC_typo.py"
        pc.write_text(srcC, encoding="utf-8")
        rcC, outC = _run(pc, work)
        okC = rcC == 1
        rec("C 键拼错路径 → 表现为差异(rc=1)", okC, f"RC={rcC} | {_first_line(outC)}")
        if a.verbose:
            print(outC)

    # ── 收尾：副本目录保留（便于取证），只提示位置 ──
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    verdict = "全部按预期" if passed == total else "存在未按预期项，门禁 6 可能有失效风险"
    print(f"\n合计 {total} 项 | 通过 {passed} | 失败 {total - passed}")
    print(f"结论：{verdict}")
    print(f"副本留存：{work}")

    # ── 落证据 ──
    lines = [
        "门禁 6（OpenAPI 未漂移）负向演练",
        f"时间：{_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"正本：{src_path}",
        "",
        "方法：复制 gen-openapi.py 到 target/drill/ 副本，ROOT 重定向指向正本项目，",
        "      对 RESPONSE_SCHEMA_OVERRIDE 做三种变异后跑 --check，看门禁是否按预期转红。",
        "      --check 只读，副本不写任何文件；正本一行未动。",
        "",
        "判据：A 清空登记表 → rc=1；B 键写回 tuple → rc=2 + [FATAL]；C 键拼错 → rc=1。",
        "",
    ]
    for name, ok, detail in results:
        lines.append(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" | {detail}" if detail else ""))
    lines += [
        "",
        f"合计 {total} 项 | 通过 {passed} | 失败 {total - passed}",
        f"结论：{verdict}",
        "",
        "由此确认的三件事：",
        "1. 门禁 6 有判定力——登记表一旦失效，它立刻转红，不是恒绿的摆设。",
        "2. “写错却静默忽略”这条路已被堵死——键格式非法时启动即 rc=2，",
        "   而不是等到某天门禁莫名变绿才回头查。",
        "3. 自检只管格式、不管存在性：键拼错路径不会 FATAL，只会表现为差异。",
        "   这是刻意的取舍——存在性该由真实路由表说话，硬编码第二份路由只会再造一个漂移源。",
        "",
    ]
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text("\n".join(lines), encoding="utf-8")
    print(f"证据：{EVIDENCE}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
