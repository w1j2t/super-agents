# -*- coding: utf-8 -*-
"""check_subproc_encoding.py —— 子进程文本解码守护：让"GBK 崩一次"变成"崩不了第二次"。

为什么需要这道门禁
------------------
camp-g 那次长跑（119 轮真实写代码）最后是**跑完了但账不对**：三根线程全崩在
`subprocess.py` 的 `_readerthread`，报

    UnicodeDecodeError: 'gbk' codec can't decode byte 0xab in position 110

真因不在被测代码里，在**评测台自己**：`subprocess.run(..., capture_output=True, text=True)`
没给 `encoding`，Python 就按系统默认（中文 Windows 上是 GBK）去解子进程输出；
只要子进程吐出一个 GBK 解不了的字节，读取线程当场抛异常，而线程崩溃不会打断主流程，
于是**错误被吞掉、成绩照记**——比崩掉更糟：崩掉你会去修，吞掉你会拿它去汇报。

这类 bug 有三个特征，正因如此必须用门禁而不是靠记性：
  · **只有真跑过才暴露**：单测里子进程输出是干净 ASCII，永远碰不到 0xab；
  · **只在特定机器上暴露**：Linux/macOS 默认 UTF-8，同一份代码在那里全绿；
  · **只在大样本上暴露**：跑 3 轮撞不到，跑 119 轮就撞到——越到交付越容易翻车。

所以这道门禁两头都管：
  一、**静态**：全仓扫 `subprocess` 文本调用，缺 `encoding` 或 `errors` 一律判红；
  二、**行为**：真起一个吐非法字节的子进程，验证"正确写法不崩、裸 text=True 确实会崩"。
      第二条既复现了历史故障，又能在环境变化（比如有人全域设了 PYTHONUTF8=1）时把
      "这条检查已经失效"如实报成 SKIP，而不是继续空转成假绿。

用法：
  python scripts/check_subproc_encoding.py           # 正常体检
  python scripts/check_subproc_encoding.py --drill   # 负向演练：塞一个坏文件，必须被抓到
  python scripts/check_subproc_encoding.py --json

退出码：0 = 全绿（或仅 SKIP 项）；1 = 有断言失败；2 = --drill 未按预期变红。
"""
from __future__ import annotations

import argparse
import json
import locale
import os
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent

# 扫描范围：真正会跑子进程的代码。docs / temp / target / marketplace 是产物与外部技能，不在口径内。
SCAN_DIRS = [ROOT / "scripts", ROOT / "workers", ROOT]
SKIP_DIRS = {"target", "target-gnu", "marketplace", "node_modules", ".git",
             "docs", "temp", "dist", "build", "__pycache__", ".venv", "venv"}

CALL_RE = re.compile(r"subprocess\.(run|Popen|check_output|check_call)\s*\(")

RESULTS: list[tuple[str, str, str]] = []   # (状态, 断言名, 说明)


def rec(status: str, name: str, detail: str = "") -> None:
    RESULTS.append((status, name, detail))
    mark = {"OK": "OK  ", "FAIL": "FAIL", "SKIP": "SKIP"}[status]
    print("%s  %s%s" % (mark, name, ("  -> " + detail) if detail else ""))


def _iter_calls(src: str):
    """产出 (起始行号, 调用块文本)。块按括号配平截取，最多 25 行。"""
    lines = src.split("\n")
    for i, line in enumerate(lines):
        if not CALL_RE.search(line):
            continue
        buf, depth = [], 0
        for j in range(i, min(i + 25, len(lines))):
            buf.append(lines[j])
            depth += lines[j].count("(") - lines[j].count(")")
            if depth <= 0 and j > i:
                break
        yield i + 1, "\n".join(buf)


def _strip_strings_and_comments(src: str) -> str:
    """把字符串字面量与注释替换成等长空白，只留可执行代码。

    为什么必须这么做：本文件的 docstring 里就写着 `subprocess.run(..., text=True)`
    这个坏例子，drill 里也有一段坏样板字符串——不清洗的话，扫描器第一件事是把自己抓了。
    假阳性比漏报更坏：它会让人把门禁关掉。等长替换保证行列号不变，报错定位仍准确。
    """
    import io
    import tokenize

    out = list(src)
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.STRING, tokenize.COMMENT):
                (sr, sc), (er, ec) = tok.start, tok.end
                lines = src.split("\n")
                for r in range(sr, er + 1):
                    row = lines[r - 1]
                    a = sc if r == sr else 0
                    b = ec if r == er else len(row)
                    base = sum(len(l) + 1 for l in lines[:r - 1])
                    for idx in range(base + a, base + b):
                        if idx < len(out) and out[idx] != "\n":
                            out[idx] = " "
    except (tokenize.TokenError, IndentationError):
        # 解析不了就退回原文：宁可多报，不可漏报（漏报意味着这道门禁白装）。
        return src
    return "".join(out)


def scan_file(path: pathlib.Path) -> list[tuple[int, str]]:
    """返回该文件里不合规的调用：[(行号, 缺什么)]。"""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if "subprocess." not in raw:
        return []
    src = _strip_strings_and_comments(raw)
    raw_lines = raw.split("\n")
    bad = []
    for lineno, blk in _iter_calls(src):
        # 豁免标记按**原文**判断：注释在清洗后已被抹成空白，清洗后再找就永远找不到。
        # 只有"故意写坏来证明断言有效"的样板才允许打这个标记，且必须写明为什么。
        span = raw_lines[lineno - 1: lineno + blk.count("\n") + 1]
        if any("noqa: subproc-encoding" in l for l in span):
            continue
        is_text = ("text=True" in blk) or ("universal_newlines" in blk)
        if not is_text:
            # 二进制口径不强制：拿的是 bytes，怎么解由调用方决定，不在这道门禁的职责里。
            continue
        missing = []
        if "encoding=" not in blk:
            missing.append("encoding")
        if "errors=" not in blk:
            missing.append("errors")
        if missing:
            bad.append((lineno, "+".join(missing)))
    return bad


def walk_targets():
    seen = set()
    for base in SCAN_DIRS:
        if not base.exists():
            continue
        if base == ROOT:
            files = list(ROOT.glob("*.py"))
        else:
            files = []
            for dp, dns, fns in os.walk(base):
                dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.startswith(".")]
                files.extend(pathlib.Path(dp) / fn for fn in fns if fn.endswith(".py"))
        for p in files:
            rp = p.resolve()
            if rp in seen or not rp.exists():
                continue
            seen.add(rp)
            yield rp


def gate_static(drill: bool = False) -> int:
    """一、静态扫描。"""
    total_calls, offenders, scanned = 0, [], 0
    for p in walk_targets():
        scanned += 1
        src = p.read_text(encoding="utf-8", errors="replace")
        if "subprocess." in src:
            total_calls += sum(1 for _ in _iter_calls(src))
        for lineno, miss in scan_file(p):
            offenders.append("%s:%d 缺 %s" % (p.relative_to(ROOT), lineno, miss))

    if drill:
        # 负向演练：现造一个坏文件，扫描器必须抓到它。
        fake = ROOT / "temp" / "_drill_bad_subproc.py"
        fake.parent.mkdir(parents=True, exist_ok=True)
        fake.write_text(
            "import subprocess\n"
            "r = subprocess.run(['echo', 'x'], capture_output=True, text=True)\n",
            encoding="utf-8")
        try:
            hit = scan_file(fake)
        finally:
            fake.unlink(missing_ok=True)
        if hit and hit[0][1] == "encoding+errors":
            rec("OK", "[drill] 坏样板被抓到（判据有判定力）", "命中 1 处，缺 encoding+errors")
            return 0
        rec("FAIL", "[drill] 坏样板未被抓到——扫描器已失效", "返回 %r" % (hit,))
        return 1

    if offenders:
        rec("FAIL", "静态：subprocess 文本调用必须显式编码",
            "%d 处不合规 / 共扫 %d 文件 %d 处调用" % (len(offenders), scanned, total_calls))
        for o in offenders[:20]:
            print("      " + o)
        return 1

    rec("OK", "静态：subprocess 文本调用必须显式编码",
        "共扫 %d 文件、%d 处调用，不合规 0 处" % (scanned, total_calls))
    return 0


# ---------------------------------------------------------------- 行为验证
BAD_BYTES_CMD = "import sys\nsys.stdout.buffer.write(b'\\xab\\xcd hello')\n"
CLEAN_CMD = "print('hello 正常文本')\n"


def _run(args, **kw):
    return subprocess.run([sys.executable] + args, capture_output=True, **kw)


def gate_runtime() -> int:
    """二、行为验证：正确写法不崩，裸 text=True 在本机确实会崩（复现 camp-g）。"""
    rc = 0

    # 2a. 带 encoding + errors：非法字节必须被替换而不是抛异常
    r = _run(["-c", BAD_BYTES_CMD], text=True, encoding="utf-8", errors="replace")
    if r.returncode == 0 and "hello" in r.stdout:
        rec("OK", "行为：encoding+errors 能吞掉非法字节", "stdout=%r" % r.stdout[:24])
    else:
        rec("FAIL", "行为：encoding+errors 能吞掉非法字节",
            "rc=%d err=%r" % (r.returncode, (r.stderr or "")[-120:]))
        rc = 1

    # 2b. 裸 text=True（不设 encoding）：按本机默认编码解同一串字节。
    #     "崩"有两种形态，第二种才是 camp-g 的真身，所以判据不能只看有没有抛异常：
    #       · 主线程版：直接抛 UnicodeDecodeError，好发现；
    #       · 读取线程版：异常抛在 subprocess 的 _readerthread 里，主线程安然无事，
    #         rc 照常返回、stdout 却是空的——错误被吞掉，成绩照记。camp-g 就是这种。
    #     因此判据统一为"该拿到的 hello 拿到没有"。
    def probe(kw, label):
        try:
            r = _run(["-c", BAD_BYTES_CMD], text=True, **kw)
        except UnicodeDecodeError as e:
            rec("OK", label, "主线程抛 UnicodeDecodeError: " + str(e)[:60])
            return
        if "hello" not in (r.stdout or ""):
            rec("OK", label, "读取线程被吞（camp-g 形态）：rc=%d、stdout=%r"
                % (r.returncode, (r.stdout or "")[:20]))
        else:
            rec("SKIP", label, "本机未复现（默认编码可能已是 UTF-8），断言不因此记为通过")

    probe({}, "行为：裸 text=True 在本机确实会崩（复现 camp-g 真因）")

    # 2b-2. 只写 encoding="utf-8" 而漏 errors：0xab 不是合法 UTF-8，一样解不出来。
    #       这条专门证明 errors 不是可选项——历史修复最容易只加一半就是这个原因。
    probe({"encoding": "utf-8"},  # noqa: subproc-encoding
          "行为：只设 encoding 不设 errors 输出照样被吞（errors 不可省）")

    # 2c. 干净输出不能被 2a 的写法破坏（防止"为了不崩把正常文本也吃了"）
    r3 = _run(["-c", CLEAN_CMD], text=True, encoding="utf-8", errors="replace")
    if r3.returncode == 0 and r3.stdout.strip() == "hello 正常文本":
        rec("OK", "行为：正常中文输出原样保留", r3.stdout.strip())
    else:
        rec("FAIL", "行为：正常中文输出原样保留", "rc=%d stdout=%r" % (r3.returncode, r3.stdout))
        rc = 1

    # 2d. 子进程 rc 与 stderr 不受解码设置影响（防止有人顺手把 stderr 也丢了）
    r4 = _run(["-c", "import sys; sys.stderr.write('boom'); sys.exit(7)"],
              text=True, encoding="utf-8", errors="replace")
    if r4.returncode == 7 and "boom" in (r4.stderr or ""):
        rec("OK", "行为：rc 与 stderr 不被解码设置掩盖", "rc=7，stderr 可见")
    else:
        rec("FAIL", "行为：rc 与 stderr 不被解码设置掩盖",
            "rc=%d stderr=%r" % (r4.returncode, r4.stderr))
        rc = 1
    return rc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--drill", action="store_true", help="负向演练：坏样板必须被抓到")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    print("[subproc-encoding] 根目录 %s" % ROOT)
    print("[subproc-encoding] 本机首选编码 %s / PYTHONUTF8=%r / PYTHONIOENCODING=%r"
          % (locale.getpreferredencoding(False), os.environ.get("PYTHONUTF8"),
             os.environ.get("PYTHONIOENCODING")))

    rc = gate_static(drill=args.drill)
    if not args.drill:
        rc |= gate_runtime()

    ok = sum(1 for s, _, _ in RESULTS if s == "OK")
    fail = sum(1 for s, _, _ in RESULTS if s == "FAIL")
    skip = sum(1 for s, _, _ in RESULTS if s == "SKIP")
    print("\n[subproc-encoding] %d 项：OK %d / FAIL %d / SKIP %d"
          % (len(RESULTS), ok, fail, skip))

    if args.json:
        print(json.dumps([{"status": s, "name": n, "detail": d} for s, n, d in RESULTS],
                         ensure_ascii=False, indent=2))

    if args.drill:
        # 演练的"成功"含义与门禁相反：断言把坏样板抓住了，才算这道门禁有判定力。
        if rc == 0:
            print("[subproc-encoding] 负向演练按预期变红（这是好事）：坏样板被抓到")
            return 0
        print("[subproc-encoding] FAIL 负向演练没抓住坏样板——这道门禁已失去判定力")
        return 2

    if fail:
        print("[subproc-encoding] FAIL 有子进程调用会在中文 Windows 上吞掉错误")
        return 1
    print("[subproc-encoding] PASS 子进程文本解码口径一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
