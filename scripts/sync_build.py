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


def sync() -> None:
    """工作区 → 镜像：src / Cargo.* 参与编译，scripts / docs / generated 供契约脚本读取。

    scripts 与 docs 也必须同步：否则契约脚本会拿镜像里的旧脚本、旧 json 去比对源码，
    得出的"一致"是假的（真实踩过：镜像里的旧 openapi.json 覆盖掉刚生成的新文件）。
    """
    MIRROR.mkdir(parents=True, exist_ok=True)
    for sub in ("src", "scripts", "docs", "generated"):
        src, dst = ROOT / sub, MIRROR / sub
        if not src.exists():
            continue
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
    for name in ("Cargo.toml", "Cargo.lock"):
        src = ROOT / name
        if src.exists():
            shutil.copy2(src, MIRROR / name)


def run(args: list[str]) -> int:
    env = {
        **os.environ,
        "CARGO_TARGET_DIR": str(TARGET),
        "PATH": str(Path.home() / ".cargo" / "bin") + os.pathsep + os.environ.get("PATH", ""),
    }
    cmd = [CARGO, f"+{TOOLCHAIN}"] + args
    print("$", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=str(MIRROR), env=env, text=True, encoding="utf-8", errors="replace")
    return proc.returncode


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    sync()
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
