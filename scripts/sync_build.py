#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同步源码到 ASCII 副本并执行 cargo。

为什么必须这样：工作区路径含中文与括号（「新建文件夹(7)」），MinGW ld 在链接阶段
会因非 ASCII 路径失败，cargo 也会在中文路径下产生失败缓存。因此在 %LOCALAPPDATA%\\sa-build
（纯 ASCII）保留一份镜像，所有编译/测试/冒烟都在镜像里跑，源码以工作区为准。

用法：
    python scripts/sync_build.py check            # cargo check --all-targets
    python scripts/sync_build.py test             # cargo test
    python scripts/sync_build.py build            # cargo build
    python scripts/sync_build.py test -- --nocapture
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pathguard  # noqa: E402  （SA-031：删除前统一过守卫）

ROOT = Path(__file__).resolve().parent.parent
def resolve_build() -> Path:
    """统一的镜像目录解析规则（供 sync_build.py 与 regress_all.py 共用）。

    历史上同时存在过两套镜像（桌面 build_sa1 与 %LOCALAPPDATA%\\sa-build），
    路径不一致会导致"测的和跑的不是同一份二进制"，这类混乱比编译失败更难查。

    选择顺序（先能用，再整洁）：
      1. 显式环境变量 SA_BUILD_DIR —— 用户说了算；
      2. 已经产出过二进制的镜像 —— 免去从头编译；
      3. 已存在的目录 —— 至少不凭空新建；
      4. 都没有：%LOCALAPPDATA%\\sa-build —— 纯 ASCII、不污染桌面。
    """
    env = os.environ.get("SA_BUILD_DIR", "").strip()
    if env:
        return Path(env)
    exe_rel = Path("target-gnu") / "debug" / "super-agents.exe"
    cands = [ROOT.parent / "build_sa1",
             Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "sa-build"]
    for c in cands:
        if (c / exe_rel).exists():
            return c
    for c in cands:
        if c.exists():
            return c
    return cands[-1]


MIRROR = resolve_build()
CARGO = str(Path.home() / ".cargo" / "bin" / "cargo.exe")
TARGET = MIRROR / "target-gnu"   # 与冒烟/矩阵使用的 CARGO_TARGET_DIR 保持一致
TOOLCHAIN = "stable-x86_64-pc-windows-gnu"

# SA-030：`MIRROR` 在 import 时定死，调用方后解析的 `--build-dir` 改不了它——
# `sync()` 写默认镜像、cargo 编 `--build-dir`，两边分叉却都打印"已同步"。
# 所以写目标一律显式传参；`MIRROR` 只保留作"没传参时"的缺省值。
SYNC_SUBS = ("src", "scripts", "docs", "generated", "workers")


def _atomic_copy_dir(src: Path, dst: Path, base: Path, label: str) -> None:
    """完整复制到临时名 → 校验 → 换名替换，**全程不先删旧目录**。

    SA-031 修法第 2 条要求。原实现是 `rmtree(dst)` 再 `copytree(src, dst)`：
    删除成功、复制失败（磁盘满、文件被占用）就留下一个**空的/半截的** src 目录，
    镜像再也编不过，而源码侧无副本可回滚。改成先造好新的再换名，
    复制阶段任何异常都动不到旧目录。
    """
    tmp = dst.with_name(f"{dst.name}.sync-tmp-{os.getpid()}")
    bak = dst.with_name(f"{dst.name}.sync-bak-{os.getpid()}")
    pathguard.safe_rmtree(tmp, base=base, allow_prefix=f"{dst.name}.sync-tmp-", what=f"{label} 临时目录")
    if bak.exists():
        pathguard.safe_rmtree(bak, base=base, allow_prefix=f"{dst.name}.sync-bak-", what=f"{label} 备份")
    try:
        shutil.copytree(src, tmp)                      # ① 先造新的
        n_src = sum(1 for p in src.rglob("*") if p.is_file())
        n_tmp = sum(1 for p in tmp.rglob("*") if p.is_file())
        if n_src != n_tmp:                             # ② 校验，宁可不换也不换半截
            raise OSError(f"{label} 复制不完整：源 {n_src} 个文件，复制出 {n_tmp} 个")
        if dst.exists():
            os.replace(dst, bak)                       # ③ 换名（同盘原子）
        try:
            os.replace(tmp, dst)                       # ④ 新的就位
        except OSError:
            if bak.exists():
                os.replace(bak, dst)                   # 回滚：旧目录还在
            raise
        if bak.exists():
            pathguard.safe_rmtree(bak, base=base, allow_prefix=f"{dst.name}.sync-bak-",
                                  what=f"{label} 备份")
    finally:
        if tmp.exists():
            pathguard.safe_rmtree(tmp, base=base, allow_prefix=f"{dst.name}.sync-tmp-",
                                  what=f"{label} 临时目录")


def sync(build_dir: Path | None = None) -> None:
    """工作区 → 镜像：src / Cargo.* 参与编译，scripts / docs / generated 供契约脚本读取。

    scripts 与 docs 也必须同步：否则契约脚本会拿镜像里的旧脚本、旧 json 去比对源码，
    得出的"一致"是假的（真实踩过：镜像里的旧 openapi.json 覆盖掉刚生成的新文件）。

    `build_dir`：SA-030 要求的显式目标。不传才退回模块级 `MIRROR`（等价旧行为）。
    """
    mirror = Path(build_dir) if build_dir is not None else MIRROR
    mirror.mkdir(parents=True, exist_ok=True)
    # workers 一并同步：默认执行后端是 process，脚本目录必须随产物分发，
    # 否则"开箱即真实执行"只是口号（三方审查 H2）。
    for sub in SYNC_SUBS:
        src, dst = ROOT / sub, mirror / sub
        if not src.exists():
            continue
        _atomic_copy_dir(src, dst, base=mirror, label=f"镜像同步 {sub}")
    for name in ("Cargo.toml", "Cargo.lock"):
        src = ROOT / name
        if src.exists():
            shutil.copy2(src, mirror / name)


def run(args: list[str], build_dir: Path | None = None) -> int:
    mirror = Path(build_dir) if build_dir is not None else MIRROR
    env = {
        **os.environ,
        "CARGO_TARGET_DIR": str(mirror / "target-gnu"),
        "PATH": str(Path.home() / ".cargo" / "bin") + os.pathsep + os.environ.get("PATH", ""),
    }
    cmd = [CARGO, f"+{TOOLCHAIN}"] + args
    print("$", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=str(mirror), env=env, text=True, encoding="utf-8", errors="replace")
    return proc.returncode


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    try:
        sync()
    except pathguard.GuardError as exc:
        # 守卫拒绝时**不降级为"继续用镜像旧内容"**：那等于测旧代码还报绿。
        print(f"[FAIL] {exc}")
        print("       镜像路径看起来指到了源码或系统目录，"
              "请检查 SA_BUILD_DIR / --build-dir 是否少写了一层目录名。")
        return 2
    action = sys.argv[1]
    rest = sys.argv[2:]
    if action == "check":
        return run(["check", "--all-targets"] + rest)
    if action == "test":
        return run(["test"] + rest)
    if action == "build":
        return run(["build"] + rest)
    if action == "clippy":
        return run(["clippy", "--all-targets", "--", "-D", "warnings"] + rest)
    print(f"未知动作: {action}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
