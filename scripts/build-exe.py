#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build-exe.py —— 门禁 0：编译出被测二进制，并自证"它比源码新"。

# 为什么需要这道门禁（真实踩过的坑）

regress_all.py 原来只跑 `cargo test`。但 `cargo test` 产出的是**测试可执行文件**，
不会更新 `super-agents.exe`。于是出现了一个非常隐蔽的假绿：

    改了 src/orchestration/executor.rs（20:33）
    跑 regress_all.py → cargo test 通过、冒烟通过、矩阵通过……
    但冒烟和矩阵起的是 18:48 的**旧 exe**

12 道门禁全绿，测的却是上一版程序。这类问题比"红"危险得多：红会让人停下，
假绿会让人放心地带着未验证的改动往下走。

本脚本做两件事，缺一不可：
  1. 真编译：`cargo build`，确保 exe 与源码同版本
  2. 自证新鲜：exe 的 mtime 必须不早于镜像内任何 .rs / Cargo.toml 的 mtime
     （只 build 不校验，等于把"我 build 过了"当成事实；万一 build 用了缓存或失败的旧产物，仍会假绿）

用法：
  python scripts/build-exe.py [--build-dir <ascii 镜像目录>] [--exe <exe 路径>]
退出码：0 = 编译成功且二进制新鲜；非 0 = 编译失败或二进制落后于源码
"""

import argparse
import os
import pathlib
import hashlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
try:
    from sync_build import resolve_build as _resolve_build
    DEFAULT_BUILD = _resolve_build()
except Exception:  # noqa: BLE001
    DEFAULT_BUILD = pathlib.Path(os.environ.get("LOCALAPPDATA", ".")) / "sa-build"

TOOLCHAIN = "+stable-x86_64-pc-windows-gnu"

# 内容指纹文件：与 exe 同目录，记录"上一次编译时源码长什么样"。
# 为什么不再只信 mtime：镜像同步（sync_build）会用 copy 覆盖文件、把 mtime 刷成
# 同步时刻，于是**内容一字未改**也会被判成"源码更新了"（2026-11-14 实遇到：
# Cargo.lock 12:56 编译、13:11 同步刷 mtime → 门禁 0 假红，挡住整轮全量）。
# mtime 还有一个反向漏洞：改完又改回、或先用 copy 保留旧时间戳，能骗过它。
# 指纹取内容，两种错都不犯；代价只是全仓 .rs 读一遍（约 1MB，毫秒级）。
FINGERPRINT_NAME = ".src-fingerprint"
# 允许的时钟容差：文件系统时间精度 + 编译瞬间的写入顺序，1 秒足够
TOLERANCE = 1.0


def newest_source_mtime(build_dir: pathlib.Path) -> tuple:
    """返回 (mtime, 路径)。只扫源码，绝不扫 target/——否则刚编译出的产物会自我证明新鲜。"""
    newest, where = 0.0, ""
    for pat in ("**/*.rs", "Cargo.toml", "Cargo.lock"):
        for p in build_dir.glob(pat):
            parts = set(p.parts)
            if "target" in parts or "target-gnu" in parts:
                continue
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if m > newest:
                newest, where = m, str(p.relative_to(build_dir))
    return newest, where


def source_fingerprint(build_dir: pathlib.Path) -> str:
    """源码内容指纹：路径 + 字节，按路径排序后哈希。

    只含"能让 exe 变样"的输入（.rs / Cargo.toml / Cargo.lock）。
    scripts/*.py 与 docs 不进指纹：它们改了不需要重编 exe，
    若算进来会让"只改门禁脚本"也触发一次全量重编，白白拖慢循环。
    """
    h = hashlib.sha256()
    paths = list(build_dir.glob("**/*.rs"))
    paths += [build_dir / "Cargo.toml", build_dir / "Cargo.lock"]
    for p in sorted(paths, key=lambda x: str(x)):
        if set(p.parts) & {"target", "target-gnu"}:
            continue
        if not p.is_file():
            continue
        h.update(str(p.relative_to(build_dir)).replace("\\", "/").encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-dir", default=str(DEFAULT_BUILD))
    ap.add_argument("--exe", default=str(pathlib.Path(DEFAULT_BUILD) / "target-gnu" / "debug" / "super-agents.exe"))
    ap.add_argument("--skip-build", action="store_true", help="只校验新鲜度，不编译")
    args = ap.parse_args()

    build_dir = pathlib.Path(args.build_dir)
    exe = pathlib.Path(args.exe)
    if not build_dir.exists():
        print("镜像目录不存在：%s（先跑 scripts/sync_build.py）" % build_dir)
        return 2

    if not args.skip_build:
        # 先同步，再编译。
        #
        # cargo 只看镜像里的源码，不看工作区。不同步就直接编，会得到：
        # 源码改了 → cargo 认为无变化(0.4s 编完) → 脚本热情地报告
        # "二进制与源码同版本" → 后续门禁全绿，测的却是上一版程序。
        # 这是最坏的一种假绿：连"新鲜度自检"本身都被骗过去了。
        try:
            os.environ["SA_BUILD_DIR"] = str(build_dir)
            sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
            import sync_build  # noqa: E402  (同目录脚本，延迟导入避免影响 --skip-build)

            sync_build.sync()
        except Exception as e:  # noqa: BLE001
            print("同步源码到镜像失败：%s" % e)
            return 1
        env = dict(os.environ)
        env["CARGO_TARGET_DIR"] = str(build_dir / "target-gnu")
        env["PYTHONIOENCODING"] = "utf-8"
        t0 = time.time()
        r = subprocess.run([ "cargo", TOOLCHAIN, "build", "--quiet"],
                           cwd=str(build_dir), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, timeout=1800)
        if r.returncode != 0:
            print("编译失败（rc=%d）：" % r.returncode)
            print((r.stdout or "") + (r.stderr or ""))
            return 1
        print("编译完成 %.1fs" % (time.time() - t0))

    if not exe.exists():
        print("编译后仍找不到二进制：%s" % exe)
        return 1

    fp_now = source_fingerprint(build_dir)
    fp_file = exe.parent / FINGERPRINT_NAME
    fp_prev = fp_file.read_text(encoding="utf-8").strip() if fp_file.is_file() else ""
    src_m, src_where = newest_source_mtime(build_dir)
    exe_m = exe.stat().st_mtime
    exe_s = time.strftime("%H:%M:%S", time.localtime(exe_m))
    src_s = time.strftime("%H:%M:%S", time.localtime(src_m)) if src_m else "-"
    # mtime 差异只作提示：同步会刷 mtime，它已经不是可靠判据了。
    drift = "（mtime 显示源码晚 %s，系同步刷新，内容一致）" % src_where if (
        src_m and exe_m + TOLERANCE < src_m) else ""

    if not args.skip_build:
        # 刚编完：exe 就是当前源码的产物，直接落指纹并放行。
        # 指纹在编译**之后**重算：cargo 可能顺手改 Cargo.lock（依赖解析），
        # 用编译前的旧值会把下一次自检判成"落后"。
        fp_file.write_text(source_fingerprint(build_dir), encoding="utf-8")
        print("二进制与源码同版本：exe=%s 指纹 %s…%s" % (exe_s, fp_now[:12], drift))
        return 0

    # --skip-build：只校验不编译，靠指纹比对。
    if fp_prev and fp_prev == fp_now:
        print("二进制与源码内容一致：exe=%s 指纹 %s…%s" % (exe_s, fp_prev[:12], drift))
        return 0

    if not fp_prev:
        # 指纹文件缺（首次升级 / 被清理）：退回 mtime 判定，别让升级中出现一次莫名变红。
        if src_m and exe_m + TOLERANCE < src_m:
            print("二进制落后于源码：exe=%s 最新源码=%s(%s)（无指纹记录，按 mtime 判）"
                  % (exe_s, src_s, src_where))
            print("继续跑后面的门禁会得到'假绿'——测的是上一版程序。已中止。")
            return 1
        print("二进制与源码同版本：exe=%s 最新源码=%s(%s)（无指纹记录，按 mtime 判）"
              % (exe_s, src_s, src_where))
        return 0

    print("二进制落后于源码内容：exe=%s 指纹 %s… ≠ 当前源码 %s…%s"
          % (exe_s, fp_prev[:12], fp_now[:12], "（%s）" % src_where if src_where else ""))
    print("继续跑后面的门禁会得到'假绿'——测的是上一版程序。已中止。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
