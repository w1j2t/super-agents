#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门禁 29：删除路径守卫（SA-031）。

这一道守的不是功能，是**一个参数就能毁掉工作区**的那类缺陷：
sync_build / verify / smoke 各有一句 rmtree，目标由 `--build-dir` / `--data-dir`
拼出来。参数少写一层目录名，删的就是源码；rmtree 不进回收站，事后无从恢复。
前面 28 道门禁全绿也证明不了这件事——它们每一次都是用**正确参数**跑的。

本道三件事：
  A. 跑 pathguard 自检（19 项判定 + 真删演练，全在临时目录里演）；
  B. 拿**真实路径**做只判不删的断言：仓库本身 / 仓库内 src / 仓库祖先 /
     家目录 / 系统目录 都必须被拒，镜像子目录必须放行；
  C. 静态守护：三个脚本里不得再出现绕过守卫的裸 `shutil.rmtree(...)`。

C 是必要的一环：A/B 只能证明"守卫本身对"，挡不住下一个人新增一句裸 rmtree。
C 的扫描范围必须是 scripts/*.py 全量（pathguard.py 自身除外）：只点名几个脚本，
等于只守住了当时记得的那几个，新脚本照样能带着裸删除进场。
退出码：0 全绿；1 有失败。
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PY = sys.executable

ok = 0
bad = 0


def expect(name: str, cond: bool, detail: str = "") -> None:
    global ok, bad
    if cond:
        ok += 1
        print(f"[OK  ] {name}")
    else:
        bad += 1
        print(f"[FAIL] {name}  {detail}")


def main() -> int:
    print("=== 门禁 29：删除路径守卫（SA-031） ===")

    # A. 守卫自检（独立进程，避免本进程的 cwd/环境干扰）
    r = subprocess.run([PY, str(ROOT / "scripts" / "pathguard.py"), "--selftest"],
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    tail = (r.stdout or "").strip().splitlines()[-1:] or [""]
    expect("A 守卫自检", r.returncode == 0, f"rc={r.returncode} {tail[0]}")
    print(f"       {tail[0]}")

    # B. 真实路径只判不删
    sys.path.insert(0, str(ROOT / "scripts"))
    import pathguard  # noqa: E402

    cases = [
        ("B1 拒绝镜像指到仓库本身（dst=仓库/src）", ROOT / "src", ROOT, False),
        ("B2 拒绝清仓库根", ROOT, ROOT.parent, False),
        ("B3 拒绝清仓库祖先", ROOT.parent, pathlib.Path(ROOT.anchor), False),
        ("B4 拒绝清用户主目录", pathlib.Path.home(), pathlib.Path.home().parent, False),
        ("B5 拒绝清系统目录", pathlib.Path("C:/Windows"), pathlib.Path("C:/"), False),
        ("B6 放行镜像子目录（正常路径）", ROOT.parent / "sa-build-mirror-nowhere" / "src",
         ROOT.parent / "sa-build-mirror-nowhere", True),
    ]
    for name, target, base, should_allow in cases:
        why = pathguard.danger_reason(target, base=base)
        allowed_ = why is None
        expect(name, allowed_ == should_allow, f"判定={('放行' if allowed_ else '拒绝: ' + str(why))}")

    # C. 静态守护：全仓脚本里不得再出现绕过守卫的裸 rmtree
    # 范围从"三个脚本"扩到 scripts/*.py 全量：原来只点名 sync_build / verify / smoke，
    # 其余 7 处裸删除（探针、矩阵、演练）照样躺在仓库里，而门禁每次都报绿 ——
    # 守一个名单等于守住了当时的记忆，不是守住了这件事本身。
    # pathguard.py 自身除外：里面的 rmtree 是守卫的实现，不是绕过守卫的调用。
    # check-pathguard.py 也除外：它要把 "shutil.rmtree(" 当字面量比对，天然带这个串。
    # 带时间戳的快照副本（xxx_20261110_211022_532.py）也除外：那是历史归档，不是
    # 现在会跑的脚本。但它仍会被打出来提醒清理——归档里留着裸删除，哪天有人
    # 拿它当"最新版"跑一遍，那处删除就复活了。
    skip = {"pathguard.py", "check-pathguard.py"}
    # 形如 xxx_20261110_211022_532.py：日期_时分秒_毫秒，是 cp 备份留下的名字
    snapshot_re = re.compile(r"_\d{8}_\d{6}(?:_\d{3})?\.py$")
    all_scripts = sorted((ROOT / "scripts").glob("*.py"))
    snapshots = [p.name for p in all_scripts if snapshot_re.search(p.name)]
    scanned = [p for p in all_scripts if p.name not in skip and p not in
               [q for q in all_scripts if snapshot_re.search(q.name)]]
    if snapshots:
        print(f"[NOTE] 跳过历史快照 {len(snapshots)} 个（建议清理）：{snapshots[:3]}")
    offenders: list[str] = []
    no_guard: list[str] = []
    for p in scanned:
        text = p.read_text(encoding="utf-8")
        bare = [ln.strip() for ln in text.splitlines()
                if "shutil.rmtree(" in ln and not ln.strip().startswith("#")]
        if bare:
            offenders.append(f"{p.name}({len(bare)}处)")
            if "pathguard" not in text:
                no_guard.append(p.name)
    expect(f"C 静态守护：{len(scanned)} 个脚本无裸 rmtree", not offenders,
           f"仍裸删：{offenders[:4]}")
    expect("C 静态守护：用到删除的脚本都已接守卫", not no_guard, f"未接：{no_guard[:4]}")
    # 反向自证：这一条要能真的变红，否则"全绿"只是没扫到东西
    expect("C 自证：扫描范围覆盖到已知含删除的脚本", len(scanned) >= 6,
           f"只扫到 {len(scanned)} 个脚本，范围可能被改小了")

    total = ok + bad
    print(f"\n=== 小结：断言 {total} 项 | 通过 {ok} | 失败 {bad} ===")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
