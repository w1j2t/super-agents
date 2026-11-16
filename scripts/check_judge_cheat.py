# -*- coding: utf-8 -*-
"""check_judge_cheat.py —— 判据的"骗子测试"：专门拿假交付去骗编译率。

为什么需要这道门禁
------------------
评测台上最贵的错误不是"功能坏了"，是**判据坏了还看不出来**。语法检查只要问
"这段代码能不能编译"，下列东西全都能骗过去：

    def solve(x): pass                     # 合法，零实现
    def solve(x): ...                      # 合法，零实现
    fn solve(x: i32) -> i32 { todo!() }    # 合法，零实现
    print('Hello World')                   # 合法，玩具

它们编译一律通过，于是"编译率"把这些记成模型的成绩。反过来还有另一种骗法：
检查方式本身错了，把**合法代码判成失败**——本台真发生过：rustc 漏了
`--crate-type lib`，按可执行程序编译、强制要 main，任务却是"写函数"，于是
一整类合法库代码被 E0601 判死。这一条让 Rust 通过率长期停在个位数，
而真凶是评测台。

两种骗法方向相反、后果一样：**数字看似有据，实则无意义**。所以这道门禁两头都测：
  · 假代码不许被记成"编译通过"；
  · 真代码不许被记成"编译失败"。
并且每条断言都配一次负向演练（--drill），证明这些断言**真的会红**——
一条永远绿的测试等于没有测试。

用法：
  python scripts/check_judge_cheat.py           # 正常体检
  python scripts/check_judge_cheat.py --drill   # 负向演练：故意用错判据，看断言会不会红
  python scripts/check_judge_cheat.py --json

退出码：0 = 全绿；1 = 有断言失败（判据失去判定力）；3 = 环境缺 rustc，跳过 Rust 项。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import soak_project as S  # noqa: E402

RESULTS = []
SKIPPED = []


def rec(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("%-4s %-46s %s" % ("PASS" if ok else "FAIL", name, detail))


def skip(name, why):
    SKIPPED.append((name, why))
    print("%-4s %-46s %s" % ("SKIP", name, why))


# ---------------------------------------------------------------- 用例集

GOOD_RUST_FN = (
    "use std::collections::HashMap;\n\n"
    "pub fn tally(items: &[String]) -> HashMap<String, u32> {\n"
    "    let mut m = HashMap::new();\n"
    "    for it in items {\n"
    "        *m.entry(it.clone()).or_insert(0) += 1;\n"
    "    }\n"
    "    m\n"
    "}"
)
GOOD_RUST_STRUCT = (
    "pub struct SaveSlot {\n"
    "    pub id: u64,\n"
    "    pub version: u32,\n"
    "    pub payload: String,\n"
    "}\n\n"
    "impl SaveSlot {\n"
    "    pub fn new(id: u64) -> Self {\n"
    "        Self { id, version: 1, payload: String::new() }\n"
    "    }\n"
    "}"
)
BAD_RUST = "pub fn broken( {\n    let = ;\n}"
GOOD_PY = (
    "import json\n\n"
    "def load(path):\n"
    "    with open(path, encoding='utf-8') as fh:\n"
    "        return json.load(fh)\n\n"
    "def total(rows):\n"
    "    return sum(r.get('n', 0) for r in rows)\n"
)
BAD_PY = "def broken(:\n    return 1"

# 假交付：语法合法、零实现。全部必须被拦下。
CHEATS = {
    "py-pass 空函数": "def solve(x):\n    pass",
    "py-ellipsis 空函数": "def solve(x):\n    ...",
    "py-NotImplementedError": "def solve(x):\n    raise NotImplementedError",
    "rust-todo 空函数": "fn solve(x: i32) -> i32 {\n    todo!()\n}",
    "rust-unimplemented": "pub fn solve(x: i32) -> i32 {\n    unimplemented!()\n}",
    "hello-world 玩具": "print('Hello World')",
    "纯注释": "# TODO: 实现这个功能\n# 先读文件\n# 再写回去",
    "尖括号占位": "<code here>",
    "三点省略": "...",
}

REAL = {
    "py 多函数实现": GOOD_PY,
    "py 单行真实现": "def double(x):\n    return x * 2 + 1",
    "rust 函数实现": GOOD_RUST_FN,
    "rust 结构体+impl": GOOD_RUST_STRUCT,
}


def rustc_available():
    rc = pathlib.Path.home() / ".cargo" / "bin" / "rustc.exe"
    return str(rc) if rc.exists() else None


def rust_check_broken_way(code):
    """故意用**错的**调用方式（不给 --crate-type lib）编一次。

    这是负向演练的核心：它必须把 GOOD_RUST_FN 判成失败，否则说明"漏 crate-type"
    根本不是个问题，那条修复就成了没有依据的改动。
    """
    rc = rustc_available()
    if not rc:
        return None
    with tempfile.TemporaryDirectory() as td:
        src = pathlib.Path(td) / "probe.rs"
        src.write_text(code, encoding="utf-8")
        r = subprocess.run([rc, "--edition", "2021", "--emit=metadata",
                            "-o", str(pathlib.Path(td) / "o.rmeta"), str(src)],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        return r.returncode == 0, (r.stderr or "")[:120]


def run_normal():
    print("=" * 78)
    print("一、真代码不许被误杀（判据太严 = 另一种骗）")
    print("=" * 78)
    for name, code in REAL.items():
        lang = "rs" if "rust" in name else "py"
        ok, detail = S.compile_check(lang, code)
        rec("真代码判通过：" + name, ok is True, "compiles=%s %s" % (ok, detail[:40]))
        sub, why = S.is_substantive(code, lang)
        rec("真代码判实质：" + name, sub is True, why)

    print()
    print("=" * 78)
    print("二、假代码不许被记成'编译通过'（判据太松 = 数字虚高）")
    print("=" * 78)
    for name, code in CHEATS.items():
        # 走完整判分链路：compile_check -> placeholder/hollow 三态
        j = judge_synthetic(code, lang="rs" if "rust" in name else "py")
        blocked = j["compiles"] is None
        rec("假交付被拦下：" + name, blocked,
            "compiles=%s src=%s" % (j["compiles"], j["compile_source"]))
        # 被拦下还不够，必须归到正确的原因桶，否则下一步动作会治错病
        expect = "placeholder-instead-of-code" if S.is_placeholder(code) else "hollow-pass"
        if not S.is_placeholder(code) and j["compile_source"] == "hollow":
            expect = "hollow-pass"
        rec("  归因正确：" + name, j["failure_reason"] == expect,
            "reason=%s 期望=%s" % (j["failure_reason"], expect))

    print()
    print("=" * 78)
    print("三、真语法错必须判失败（不能把'错'也放过）")
    print("=" * 78)
    ok_py, d_py = S.compile_check("py", BAD_PY)
    rec("py 语法错判失败", ok_py is False, (d_py or "")[:50])
    if rustc_available():
        ok_rs, d_rs = S.compile_check("rs", BAD_RUST)
        rec("rust 语法错判失败", ok_rs is False, (d_rs or "")[:50])
    else:
        skip("rust 语法错判失败", "环境无 rustc")

    print()
    print("=" * 78)
    print("四、端到端：可用交付口径（假交付不能算可用）")
    print("=" * 78)
    j_fake = judge_synthetic("def solve(x):\n    pass", lang="py", delivered=True)
    rec("空函数不算可用交付", S.usable_of(j_fake) is False,
        "usable=%s reason=%s" % (S.usable_of(j_fake), j_fake["failure_reason"]))
    j_real = judge_synthetic(GOOD_PY, lang="py", delivered=True)
    rec("真实现算可用交付", S.usable_of(j_real) is True,
        "usable=%s compiles=%s" % (S.usable_of(j_real), j_real["compiles"]))
    # 反向陷阱：真正的语法错也不能被算成"可用"
    j_bad = judge_synthetic(BAD_PY, lang="py", delivered=True)
    rec("语法错不算可用交付", S.usable_of(j_bad) is False, "compiles=%s" % j_bad["compiles"])


def run_drill():
    """负向演练：证明上面的断言真的会红。

    做法不是改代码，而是**用错的调用方式跑一遍**，看结论是否如预期翻转。
    这正是 2026-11-16 那次修复的原始证据：同样一份合法 Rust 代码，
    不给 --crate-type lib 时 rustc 报 E0601(main not found)。
    """
    print("=" * 78)
    print("演练：把 --crate-type lib 拿掉，合法库代码会不会被判死")
    print("=" * 78)
    if not rustc_available():
        skip("演练 · 漏 crate-type 会不会红", "环境无 rustc")
        return
    got = rust_check_broken_way(GOOD_RUST_FN)
    ok, err = got
    rec("演练：漏 crate-type 时合法库代码被判失败（按预期应为'红'）",
        ok is False, "编译成功=%s 首行=%s" % (ok, err.strip().splitlines()[0][:60] if err.strip() else ""))
    rec("演练：报的正是 E0601(main not found)", "E0601" in err,
        "命中=%s" % ("E0601" in err))
    # 修好后必须通过——两者并排才说明"修复"不是猜的
    ok2, d2 = S.compile_check("rs", GOOD_RUST_FN)
    rec("演练：修好后同一份代码判通过", ok2 is True, d2[:40])


def judge_synthetic(code, lang="py", delivered=True):
    """构造一条最小 run_task 返回，走真实 judge() 链路。

    必须走 judge() 而不是直接调 is_placeholder：门禁要防的是**链路组装出错**
    （谁覆盖谁、顺序反了、新闸忘了接），只测单个函数测不到这些。
    """
    payload = {"mode": "code", "code": code, "code_chars": len(code),
               "delivered_code": delivered, "wants_code": True, "language": lang}
    row = {
        "task_status": "succeeded",
        "steps": [{"agent": "code-agent", "success": True, "attempts": 1,
                   "out": "[[SA]]" + json.dumps(payload, ensure_ascii=False)}],
    }
    j = S.judge(row)
    j["failure_reason"] = S.failure_reason(j)
    return j


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--drill", action="store_true", help="只跑负向演练")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.drill:
        run_drill()
        bad = [r for r in RESULTS if not r[1]]
        print()
        print("演练 %d 项 | 按预期 %d | 未按预期 %d" % (len(RESULTS), len(RESULTS) - len(bad), len(bad)))
        return 0 if not bad else 1

    run_normal()

    print()
    print("=" * 78)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    print("合计 %d 项 | 通过 %d | 失败 %d | 跳过 %d"
          % (len(RESULTS) + len(SKIPPED), passed, len(failed), len(SKIPPED)))
    if failed:
        print()
        for n, d in failed:
            print("  !! %s —— %s" % (n, d))
        print()
        print("判据失去判定力：这比功能报错更该优先处理，因为它会让所有报表一起失真。")
    if args.json:
        print(json.dumps({"passed": passed, "failed": len(failed),
                          "skipped": len(SKIPPED),
                          "items": [{"name": n, "ok": ok, "detail": d} for n, ok, d in RESULTS]},
                         ensure_ascii=False, indent=2))
    if failed:
        return 1
    return 3 if (SKIPPED and not rustc_available()) else 0


if __name__ == "__main__":
    sys.exit(main())
