#!/usr/bin/env python3
"""证据与文档脱敏：把本机绝对路径、用户名从仓库内文本中抹掉。

背景：`docs/evidence/` 是门禁与压测的原始输出留痕，里面天然带本机路径
（`%USERPROFILE%\\...`）。这些文件要进公开仓库，路径就是最直接的指纹：
它同时暴露了操作系统账户名与目录布局。本脚本把它们归一化成占位符。

规则（幂等，重复跑结果一致）：
  1. `%USERPROFILE% / `%USERPROFILE% / `%USERPROFILE% → `%USERPROFILE%`
  2. 任何裸出现的 Windows 账户名（本机实测用户名由参数给出）→ `<user>`
  3. `<host>` / `<host>` 形态的计算机名 → `<host>`
  4. 邮箱 `<mail>@yyy.zzz` 中的本地部分 → `<mail>@yyy.zzz`

用法：
  python scripts/sanitize_privacy.py            # 干跑，只报告
  python scripts/sanitize_privacy.py --apply    # 真正改写
  python scripts/sanitize_privacy.py --apply --check   # 只检查，有残留则退出码 1

退出码：0 = 干净；1 = 仍有残留（--check 模式）。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TEXT_SUFFIXES = {
    ".txt", ".log", ".json", ".jsonl", ".md", ".rs", ".py", ".toml",
    ".yaml", ".yml", ".ts", ".tsx", ".js", ".html", ".css", ".ps1", ".sh", ".lock",
}

SKIP_DIRS = {"target", ".git", "node_modules", "__pycache__", "marketplace"}

USER_HOME = Path.home()
USER_NAME = os.environ.get("USERNAME") or USER_HOME.name

# 本机绝对路径 → 占位符。正斜杠与反斜杠两种写法都要覆盖。
PATH_PATTERNS = [
    (re.compile(re.escape(str(USER_HOME).replace("\\", "\\\\")), re.I), "%USERPROFILE%"),
    (re.compile(re.escape(str(USER_HOME).replace("\\", "/")), re.I), "%USERPROFILE%"),
    (re.compile(r"[A-Za-z]:\\\\Users\\\\[^\\/\s\"']+"), "%USERPROFILE%"),
    (re.compile(r"[A-Za-z]:/Users/[^/\s\"']+"), "%USERPROFILE%"),
    (re.compile(r"[A-Za-z]:\\\\Documents and Settings\\\\[^\\/\s\"']+", re.I), "%USERPROFILE%"),
]
HOST_RE = re.compile(r"\b(?:DESKTOP|WIN)-[A-Z0-9]{4,12}\b")
MAIL_RE = re.compile(r"\b([A-Za-z0-9._%+\-]+)@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")
# 裸用户名：用词边界夹住，避免误伤 943550 之类的数字串
NAME_RE = re.compile(rf"(?<![A-Za-z0-9_\-]){re.escape(USER_NAME)}(?![A-Za-z0-9_\-])", re.I)

KEEP_MAIL_DOMAINS = {"users.noreply.github.com", "example.com", "example.org"}


def sanitize(text: str) -> str:
    out = text
    for pat, repl in PATH_PATTERNS:
        out = pat.sub(repl, out)
    out = HOST_RE.sub("<host>", out)
    out = NAME_RE.sub("<user>", out)

    def _mail(m: re.Match[str]) -> str:
        domain = m.group(2)
        # GitHub 的 noreply 地址本身就是脱敏产物，原样保留
        if any(domain.lower().endswith(d) for d in KEEP_MAIL_DOMAINS):
            return m.group(0)
        return f"<mail>@{domain}"

    out = MAIL_RE.sub(_mail, out)
    return out


def iter_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix.lower() in TEXT_SUFFIXES:
                yield p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写回文件")
    ap.add_argument("--check", action="store_true", help="只检查，有残留返回 1")
    args = ap.parse_args()

    changed, scanned, remaining = 0, 0, []
    for p in iter_files():
        scanned += 1
        try:
            raw = p.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            continue
        fixed = sanitize(raw)
        if fixed == raw:
            continue
        if args.apply and not args.check:
            p.write_text(fixed, encoding="utf-8")
            changed += 1
        else:
            remaining.append(p.relative_to(ROOT).as_posix())

    if args.check:
        if remaining:
            print(f"[FAIL] {len(remaining)} 个文件仍含本机隐私痕迹：")
            for r in remaining[:40]:
                print("  -", r)
            return 1
        print(f"[OK] {scanned} 个文本文件均无本机路径/用户名残留")
        return 0

    if args.apply:
        print(f"[OK] 已脱敏 {changed} 个文件（扫描 {scanned} 个）")
    else:
        print(f"[DRY] {len(remaining)} 个文件需要脱敏（扫描 {scanned} 个）；加 --apply 执行")
        for r in remaining[:20]:
            print("  -", r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
