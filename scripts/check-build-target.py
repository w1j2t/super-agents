#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门禁 30：构建目标绑定（SA-030）。

守的问题一句话：**"编的是哪份源码"和"跑的是哪个 exe"可以分叉，而报告看不出来。**

三个入口曾是分叉的：
  1. `sync_build.MIRROR` 在 import 时定死，调用方后解析的 `--build-dir` 改不了它
     —— 源码同步进默认镜像，cargo 编 `--build-dir` 指的镜像，打印却说"已同步到 X"；
  2. `--exe` 有独立默认值（指向默认镜像），只传 `--build-dir X` 时门禁 0 编 X、
     后面所有门禁仍跑默认镜像的旧 exe；
  3. 新鲜度指纹只记"源码长什么样"，不记 exe 本身 —— 换个旧 exe 过来再抄份指纹就绿了。

本道用临时镜像做断言，不碰真镜像、不进真实编译（全都是 sync + 假 exe，秒级）：

  T1 sync(build_dir=X) 只写 X，默认镜像一个字节都不动
  T2 --exe 指到别的镜像 → 必须拒绝（rc≠0，且说清期望值）
  T3 exe 被换成别的二进制 → 指纹必须发现（源码没变也照样红）
  T4 无 sync 临时目录 / 备份目录残留（原子替换的副产品不能留在镜像里）
  T5 --skip-sync 必须一路传到门禁 0（静态守护：不传就删掉这个开关，别留假承诺）

退出码：0 全绿；1 有失败。
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
PY = sys.executable
sys.path.insert(0, str(ROOT / "scripts"))
import pathguard  # noqa: E402
import sync_build  # noqa: E402

TEMP = pathlib.Path(tempfile.gettempdir())
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


def _load_build_exe():
    """build-exe.py 名字带横线，只能按路径加载。"""
    spec = importlib.util.spec_from_file_location("build_exe", ROOT / "scripts" / "build-exe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    print("=== 门禁 30：构建目标绑定（SA-030） ===")
    made = []
    try:
        default_mirror = sync_build.resolve_build()
        before = {p.name: p.stat().st_mtime_ns for p in default_mirror.iterdir()} if default_mirror.exists() else {}

        # ── T1 同步只写传入目录 ───────────────────────────────────────────
        tmp_a = pathlib.Path(tempfile.mkdtemp(prefix="sa-mirror-A-", dir=TEMP))
        made.append(tmp_a)
        t = {}
        import time as _t
        t0 = _t.time()
        sync_build.sync(tmp_a)
        cost = _t.time() - t0
        after = {p.name: p.stat().st_mtime_ns for p in default_mirror.iterdir()} if default_mirror.exists() else {}
        expect("T1a sync(build_dir=X) 写进了 X", (tmp_a / "src").is_dir() and any((tmp_a / "src").rglob("*.rs")),
               f"X/src 不存在或为空；耗时 {cost:.1f}s")
        expect("T1b 默认镜像未被写入", before == after,
               f"默认镜像被改了：{sorted(set(after) ^ set(before)) or [k for k in after if before.get(k) != after.get(k)]}")
        print(f"       默认镜像={default_mirror}  同步耗时 {cost:.1f}s")

        # ── T2 --exe 指到别的镜像必须拒 ──────────────────────────────────
        default_exe = default_mirror / "target-gnu" / "debug" / "super-agents.exe"
        r = subprocess.run([PY, str(ROOT / "scripts" / "build-exe.py"),
                            "--build-dir", str(tmp_a), "--exe", str(default_exe), "--skip-build"],
                           cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
        expect("T2 拒绝 build_dir 之外的 exe", r.returncode != 0 and "不属于当前 build_dir" in r.stdout,
               f"rc={r.returncode} out={r.stdout.strip()[:160]}")

        # ── T3 exe 被换掉，指纹必须发现 ──────────────────────────────────
        be = _load_build_exe()
        fake = tmp_a / "target-gnu" / "debug" / "super-agents.exe"
        fake.parent.mkdir(parents=True, exist_ok=True)
        fake.write_bytes(b"fake-binary-v1")
        (fake.parent / be.FINGERPRINT_NAME).write_text(be.stamp(tmp_a, fake), encoding="utf-8")
        r_ok = subprocess.run([PY, str(ROOT / "scripts" / "build-exe.py"),
                               "--build-dir", str(tmp_a), "--skip-build"],
                              cwd=str(ROOT), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=300)
        expect("T3a 指纹一致时放行", r_ok.returncode == 0, f"rc={r_ok.returncode} {r_ok.stdout.strip()[:160]}")

        fake.write_bytes(b"fake-binary-v2-another-build")   # 源码不动，只换 exe
        r_bad = subprocess.run([PY, str(ROOT / "scripts" / "build-exe.py"),
                                "--build-dir", str(tmp_a), "--skip-build"],
                               cwd=str(ROOT), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=300)
        expect("T3b exe 被换掉时必红", r_bad.returncode != 0 and "exe 与指纹记录不符" in r_bad.stdout,
               f"rc={r_bad.returncode} out={r_bad.stdout.strip()[:200]}")

        # ── T4 原子替换不留残骸 ─────────────────────────────────────────
        left = [p.name for p in tmp_a.iterdir() if "sync-tmp-" in p.name or "sync-bak-" in p.name]
        expect("T4 无 sync 临时/备份残留", not left, str(left))

        # ── T5 --skip-sync 必须传到门禁 0 ───────────────────────────────
        src = (ROOT / "scripts" / "regress_all.py").read_text(encoding="utf-8")
        expect("T5 --skip-sync 传到门禁 0", '"--no-sync"' in src and "if args.skip_sync" in src,
               "regress_all 未把 --skip-sync 传给 build-exe（承诺不动镜像，实际又同步了一次）")
    finally:
        for d in made:
            try:
                pathguard.safe_rmtree(d, base=TEMP, allow_prefix="sa-mirror-", what="门禁演练镜像")
            except pathguard.GuardError as exc:
                print(f"[WARN] 演练镜像未能清理：{exc}")

    total = ok + bad
    print(f"\n=== 小结：断言 {total} 项 | 通过 {ok} | 失败 {bad} ===")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
