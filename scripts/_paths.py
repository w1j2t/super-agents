#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""脚本共用的「被测对象在哪」解析。

存在的理由：早期三个脚本把 exe 路径写死成 `%USERPROFILE%\\Desktop\\build_sa1\\...`。
那个镜像后来废弃了，`resolve_build()` 迁到 `%LOCALAPPDATA%\\sa-build`，但写死的地方
没人改。后果不是编译失败，而是**更难查的一类**：

- 单独跑 `probe_alerting.py` → "找不到可执行文件: ...build_sa1...（用 --exe 指定）"
- 而同一道门禁在 `regress_all.py` 里是绿的，因为它显式传了 `--exe`

于是同一份代码，在门禁里绿、手动跑红。看日志的人会以为二进制没编译，实际是
路径考古。凡是"在哪找被测物"这类事实，只能有一处定义。
"""
from __future__ import annotations

import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def default_exe() -> pathlib.Path:
    """按统一镜像解析规则给出默认 exe 路径。

    与 `sync_build.resolve_build()` / `regress_all.py` 共用同一套候选顺序
    （SA_BUILD_DIR → 已产出过二进制的镜像 → 已存在目录 → %LOCALAPPDATA%\\sa-build），
    避免"三个脚本各指一个镜像"。
    """
    import sync_build  # 延迟导入：避免调用方仅为取一个路径就触发其顶层逻辑

    return sync_build.resolve_build() / "target-gnu" / "debug" / "super-agents.exe"


def build_dir() -> pathlib.Path:
    """统一的镜像目录（编译、测试、证据落盘都以它为准）。"""
    import sync_build

    return sync_build.resolve_build()
