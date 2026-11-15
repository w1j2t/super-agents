"""门禁 25 探针：自动任务调度器（workers/_sched.py）的接缝与纪律。

为什么零件自检不够
----------------
`_sched.py --selftest` 13 项全绿，只证明"调度逻辑算得对"。真正会出事的地方在接缝上，
而且全都是"看起来正常"的坏法：

  * **把执行写进派发里**。调度器最诱人的一句改动是"反正 due() 已经知道要跑什么，
    顺手跑了吧"。一旦如此，tick 变成阻塞调用，一个慢任务把整个心跳拖死，
    而所有断言仍然绿——因为谁也没规定 due() 不许干活。
  * **幂等只在内存里成立**。due() 里 mark 了但没落盘，进程一崩重启，
    同一分钟的任务再来一遍。自检里同一个实例连查两次是绿的，跨进程就穿帮。
  * **两个视图互相打脸**。due() 认定"这一分钟改补跑"，missed_since() 却说"漏了几天"——
    一个功能给两个答案，运维只能放弃其中一个。
  * **日历边界靠猜**。闰日、月末 31 日、周日写 0 还是写 7，这些不该在线上第一次遇到。

本探针守七件事，并对其中最易退化的三条配**负向演练**（改坏 → 必须变红）：

  G1 零件自检：`workers/_sched.py --selftest` rc=0（13 项）
  G2 只派发不执行：源码无执行类调用；运行时桩未被触发；哨兵文件不存在
  G3 跨实例幂等：同一分钟换一个实例（模拟重启）不得重复派发
  G4 视图一致：补跑过的任务不再报漏跑；从未跑过的必须报出来（双向）
  G5 日历接缝：闰日 / 月末 31 / 周日 0≡7 / 跨年首分钟
  G6 同库与原子写：CLI 加的任务库层读得到；无 .tmp 残留；每行都是合法 JSON
  G7 坏数据隔离：非法 cron 与坏行存在时，好任务照常派发（派发层，不只是列表层）

用法：
    python scripts/probe_sched.py               # 常规，退出码 0/1
    python scripts/probe_sched.py --drill       # 负向演练，三种坏法各自必须红
    python scripts/probe_sched.py --json        # 机读结果
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKERS = os.path.join(ROOT, "workers")
TARGET = os.path.join(WORKERS, "_sched.py")
PY = sys.executable or "python"

RESULTS: list[tuple[str, bool, str]] = []

try:                                    # 门禁在 cmd 管道里跑，别让中文把输出搞崩
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print("[%s] %-44s %s" % ("OK  " if ok else "FAIL", name, detail))
    return bool(ok)


def _load_module(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# G1 零件自检
# ---------------------------------------------------------------------------
def g1_selftest() -> bool:
    r = subprocess.run([PY, TARGET, "--selftest"], cwd=WORKERS, capture_output=True,
                       timeout=180, text=True, encoding="utf-8", errors="replace")
    tail = [ln for ln in (r.stdout or "").splitlines() if ln.startswith("[")]
    bad = [ln for ln in tail if ln.startswith("[FAIL]")]
    return check("G1 _sched --selftest", r.returncode == 0 and not bad,
                 "rc=%d，%d 项断言，失败 %d" % (r.returncode, len(tail), len(bad)))


# ---------------------------------------------------------------------------
# G2 只派发不执行
# ---------------------------------------------------------------------------
_EXEC_ATTRS = {"system", "popen", "execv", "execve", "execvp", "spawnv", "spawnl",
               "Popen", "call", "run", "check_output"}
_EXEC_NAMES = {"eval", "exec", "compile", "__import__"}


def _scan_exec_calls(src: str) -> list[str]:
    """静态找"会真正跑起来什么东西"的调用。原子写的 os.replace 不在此列。"""
    hits: list[str] = []
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in ("subprocess", "multiprocessing"):
                    hits.append("import %s" % a.name)
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in (
                "subprocess", "multiprocessing"):
            hits.append("from %s import ..." % node.module)
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr in _EXEC_ATTRS:
                # threading/os 的常见误报按模块名收窄：只认 os.* 与 subprocess.*
                base = getattr(f.value, "id", getattr(f.value, "attr", ""))
                if base in ("os", "subprocess", "shutil", "_sp"):
                    hits.append("%s.%s()" % (base, f.attr))
            elif isinstance(f, ast.Name) and f.id in _EXEC_NAMES:
                hits.append("%s()" % f.id)
    return hits


def g2_dispatch_only(mod, src: str, tmp: str) -> bool:
    hits = _scan_exec_calls(src)
    ok_static = check("G2a 源码无执行类调用", not hits,
                      "命中：%s" % ", ".join(hits) if hits else "干净")

    # 运行时桩：即便哪天换种写法执行了，这里也要能抓到
    sentinel = os.path.join(tmp, "sentinel.txt")
    fired = {"n": 0}

    def boom(*a, **kw):
        fired["n"] += 1
        return 0

    saved = (mod.os.system, mod.os.popen)
    mod.os.system, mod.os.popen = boom, boom
    try:
        store = os.path.join(tmp, "g2.jsonl")
        s = mod.Scheduler(store=store)
        s.add("危险的活", prompt="echo hi > " + sentinel, cron="0 9 * * *")
        out = s.due(datetime(2026, 11, 16, 9, 0))
    finally:
        mod.os.system, mod.os.popen = saved

    ok_runtime = check("G2b 运行时未触发执行", fired["n"] == 0 and not os.path.exists(sentinel),
                       "执行被调用 %d 次，哨兵文件%s" % (fired["n"],
                                                        "存在" if os.path.exists(sentinel) else "不存在"))
    # 只不执行还不够——得证明 prompt 确实交给了上层，否则"不执行"可以靠什么都不做实现
    ok_handoff = check("G2c prompt 已交给上层", len(out) == 1 and out[0].get("prompt", "").startswith("echo hi"),
                       "派发 %d 条，prompt=%r" % (len(out), (out[0].get("prompt", "") if out else "")))
    return ok_static and ok_runtime and ok_handoff


# ---------------------------------------------------------------------------
# G3 跨实例幂等
# ---------------------------------------------------------------------------
def g3_restart_idempotent(mod, tmp: str) -> bool:
    store = os.path.join(tmp, "g3.jsonl")
    t = datetime(2026, 11, 16, 8, 0)
    s1 = mod.Scheduler(store=store)
    s1.add("早八", prompt="汇报", cron="0 8 * * 1")
    first = s1.due(t)
    del s1                                   # 模拟进程崩掉：内存全丢，只剩磁盘
    s2 = mod.Scheduler(store=store)
    again = s2.due(t)
    return check("G3 重启后同分钟不重复派发", len(first) == 1 and again == [],
                 "首派 %d 条，重启后同分钟再派 %d 条" % (len(first), len(again)))


# ---------------------------------------------------------------------------
# G4 两个视图口径一致（双向）
# ---------------------------------------------------------------------------
def g4_views_agree(mod, tmp: str) -> bool:
    """两个视图必须给同一个答案。**双向**测：补跑过的不许再报漏，从未跑的必须报出来。

    注意别把这两件事塞进同一个实例：09:00 的心跳会把 03:00 和 05:00 都补掉，
    于是"从未跑"根本不存在——那是用例设计错，不是实现错（本轮踩过）。
    """
    now = datetime(2026, 11, 21, 9, 0)

    # A 组：补跑一次，之后不许再被当成"漏跑"
    s = mod.Scheduler(store=os.path.join(tmp, "g4a.jsonl"), catch_up=True)
    s.add("已补跑", cron="0 3 * * *")
    late = s.due(now)
    late_ok = check("G4a 补跑发生且标注补的是哪一次",
                    len(late) == 1 and late[0].get("late_for") == "2026-11-21T03:00",
                    "派发 %d 条：%s" % (len(late), json.dumps(late, ensure_ascii=False)))
    again = s.due(now)
    missed_a = [x["title"] for x in s.missed_since(now, window_minutes=1440)]
    view_ok = check("G4b 补跑过的不再报漏跑，且不连环补",
                    "已补跑" not in missed_a and again == [],
                    "missed=%s，同分钟再派 %d 条" % (missed_a, len(again)))

    # B 组：心跳压根没发生（不调 due），漏跑必须被诊断出来
    s2 = mod.Scheduler(store=os.path.join(tmp, "g4b.jsonl"), catch_up=False)
    s2.add("从未跑", cron="0 5 * * *")
    missed_b = [x["title"] for x in s2.missed_since(now, window_minutes=1440)]
    view_ok2 = check("G4c 从未跑的必须报出来", missed_b == ["从未跑"],
                     "missed=%s" % missed_b)
    return late_ok and view_ok and view_ok2


# ---------------------------------------------------------------------------
# G5 日历接缝（真实日期，不靠推演）
# ---------------------------------------------------------------------------
def g5_calendar(mod, tmp: str) -> bool:
    cases = [
        ("月末 31 日命中", "0 0 31 * *", datetime(2026, 12, 31, 0, 0), True),
        ("月末 31 日不误命中 30 日", "0 0 31 * *", datetime(2026, 12, 30, 0, 0), False),
        ("闰日 2028-02-29 命中", "0 0 29 2 *", datetime(2028, 2, 29, 0, 0), True),
        ("闰日规则不误命中平年", "0 0 29 2 *", datetime(2027, 2, 28, 0, 0), False),
        ("周日写 0 命中", "0 0 * * 0", datetime(2026, 11, 15, 0, 0), True),
        ("周日写 7 同样命中", "0 0 * * 7", datetime(2026, 11, 15, 0, 0), True),
        ("周一不被周日规则抓走", "0 0 * * 0", datetime(2026, 11, 16, 0, 0), False),
        ("跨年首分钟命中", "0 0 1 1 *", datetime(2027, 1, 1, 0, 0), True),
    ]
    ok = True
    for label, expr, when, want in cases:
        spec = mod.parse_cron(expr)
        got = mod.cron_match(spec, when)
        ok = check("G5 %s" % label, got == want,
                   "%s @ %s → %s（期望 %s）" % (expr, when.strftime("%Y-%m-%d %H:%M"),
                                               got, want)) and ok
    return ok


# ---------------------------------------------------------------------------
# G6 CLI 与库同库 + 原子写
# ---------------------------------------------------------------------------
def g6_cli_same_store(mod, tmp: str) -> bool:
    store = os.path.join(tmp, "g6.jsonl")
    r = subprocess.run([PY, TARGET, "--store", store, "--add", "CLI加的任务",
                        "--cron", "0 7 * * *", "--prompt", "打个招呼"],
                       cwd=WORKERS, capture_output=True, timeout=120, text=True,
                       encoding="utf-8", errors="replace")
    titles = [x["title"] for x in mod.Scheduler(store=store).list()]
    ok1 = check("G6a CLI 加的任务库层读得到", r.returncode == 0 and "CLI加的任务" in titles,
                "rc=%d，库内=%s" % (r.returncode, titles))

    tmp_left = [f for f in os.listdir(tmp) if f.endswith(".tmp")]
    lines_ok = True
    for ln in open(store, "r", encoding="utf-8"):
        ln = ln.strip()
        if not ln:
            continue
        try:
            json.loads(ln)
        except json.JSONDecodeError:
            lines_ok = False
    ok2 = check("G6b 原子写无 .tmp 残留、每行合法 JSON", not tmp_left and lines_ok,
                "残留=%s，JSON=%s" % (tmp_left or "无", "合法" if lines_ok else "有坏行"))
    return ok1 and ok2


# ---------------------------------------------------------------------------
# G7 坏数据隔离（派发层）
# ---------------------------------------------------------------------------
def g7_bad_rows_isolated(mod, tmp: str) -> bool:
    store = os.path.join(tmp, "g7.jsonl")
    with open(store, "w", encoding="utf-8") as fh:
        fh.write("這不是 json\n")
        fh.write('{"title":"坏 cron","kind":"cron","cron":"不是表达式"}\n')
        fh.write('{"title":"好任务","kind":"cron","cron":"0 6 * * *"}\n')
    s = mod.Scheduler(store=store)
    out = s.due(datetime(2026, 11, 16, 6, 0))
    names = [x["title"] for x in out]
    flagged = [x for x in s.list() if x.get("invalid")]
    return check("G7 坏行/非法 cron 不影响派发", names == ["好任务"] and len(flagged) == 1,
                 "派发=%s，标注非法=%d 条" % (names, len(flagged)))


# ---------------------------------------------------------------------------
def g8_explain(mod, tmp: str) -> bool:
    """G8：--explain 是说人话，不是把内部状态倒出来。

    这里刻意断言"**只有一条**问题"和"**露得出原始表达式**"两件事：
    坏 cron 的行会被降级成 once + 1970 + 停用（内部哨兵），如果判定不短路，
    同一个任务会被同时说成"不会跑"和"设定时刻 1970-01-01 已过去"——自相矛盾；
    而只报"非法"却不说是哪个表达式，等于让用户回去翻文件。这两点都是纸面上
    看着对、一跑就穿帮的地方。
    """
    store = os.path.join(tmp, "g8.jsonl")
    rows = [
        {"title": "正常夜备", "kind": "cron", "cron": "0 3 * * *", "created": "2026-11-15T08:00"},
        {"title": "坏表达式", "kind": "cron", "cron": "99 99 * * *"},
        {"title": "停用巡检", "kind": "cron", "cron": "*/30 * * * *", "enabled": False,
         "created": "2026-11-12T00:00"},
    ]
    with open(store, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    before = open(store, encoding="utf-8").read()
    s = mod.Scheduler(store=store)
    now = datetime(2026, 11, 15, 13, 30)

    a = s.explain("正常夜备", now=now)
    ok_a = check("G8a 正常任务答得出下次时刻",
                 bool(a.get("found")) and a.get("next_run") == "2026-11-16T03:00"
                 and not a.get("problems"),
                 "下次=%s 问题=%s" % (a.get("next_run"), a.get("problems")))

    b = s.explain("坏表达式", now=now)
    ok_b = check("G8b 非法表达式只报一条且露原表达式",
                 len(b.get("problems") or []) == 1 and "非法" in (b.get("verdict") or "")
                 and b.get("expr") == "99 99 * * *",
                 "问题数=%d expr=%r" % (len(b.get("problems") or []), b.get("expr")))

    c = s.explain("停用巡检", now=now)
    ok_c = check("G8c 停用任务不被判成漏跑",
                 not c.get("warnings") and len(c.get("problems") or []) == 1,
                 "问题=%s 提示=%s" % (c.get("problems"), c.get("warnings")))

    after = open(store, encoding="utf-8").read()
    ok_d = check("G8d explain 只读：store 逐字不变", before == after, "%d 字节" % len(after))

    e = s.explain("根本不存在", now=now)
    ok_e = check("G8e 查不到时给候选而不是猜",
                 e.get("found") is False and len(e.get("known") or []) == 3,
                 "候选 %d 条" % len(e.get("known") or []))
    # ---- G8f~G8i：沉默故障的三种真实形态。
    # 单开一个 store，避免把上面「候选 3 条」的断言搅乱——断言之间互相污染，
    # 是这类探针最常见的假绿来源。
    store2 = os.path.join(tmp, "g8b.jsonl")
    rows2 = [
        # 一次性任务：设定时刻已过、补跑没开。用户最常问的那句"我设的提醒没响"。
        {"title": "过期提醒", "kind": "once", "at": "2026-11-15T09:00",
         "created": "2026-11-14T00:00", "enabled": True},
        # cron 跑过，但之后该跑的准点被越过：心跳断了，任务本身没写错。
        {"title": "断点夜备", "kind": "cron", "cron": "0 3 * * *",
         "created": "2026-11-10T00:00", "last_run": "2026-11-13T03:00", "runs": 3},
        # 名字撞车：只给"备份"必须给候选，不许替用户挑一个。
        {"title": "夜备份", "kind": "cron", "cron": "0 4 * * *"},
        {"title": "日备份", "kind": "cron", "cron": "0 5 * * *"},
    ]
    with open(store2, "w", encoding="utf-8") as fh:
        for r in rows2:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    before2 = open(store2, encoding="utf-8").read()
    s2 = mod.Scheduler(store=store2)

    f = s2.explain("过期提醒", now=now)
    ok_f = check("G8f 一次性时刻已过必须点名补跑开关",
                 bool(f.get("found"))
                 and any("已过去" in p and "补跑" in p for p in (f.get("problems") or [])),
                 "问题=%s" % (f.get("problems"),))

    g = s2.explain("断点夜备", now=now)
    ok_g = check("G8g 准点被越过要报漏跑，且不误判成故障",
                 bool(g.get("found")) and not (g.get("problems") or [])
                 and any("2026-11-15T03:00" in w for w in (g.get("warnings") or []))
                 and "基本正常" in (g.get("verdict") or ""),
                 "问题=%s 提示=%s" % (g.get("problems"), g.get("warnings")))

    h = s2.explain("备份", now=now)
    ok_h = check("G8h 名称撞多个任务时给候选不猜",
                 h.get("found") is False
                 and len(h.get("candidates") or []) == 2
                 and "多个" in (h.get("reason") or ""),
                 "候选=%s" % ([c.get("title") for c in (h.get("candidates") or [])],))

    after2 = open(store2, encoding="utf-8").read()
    ok_i = check("G8i 上面三次追问都只读：store 逐字不变",
                 before2 == after2, "%d 字节" % len(after2))

    return all([ok_a, ok_b, ok_c, ok_d, ok_e, ok_f, ok_g, ok_h, ok_i])


# ---------------------------------------------------------------------------
# 负向演练：把源码改坏，断言必须变红
# ---------------------------------------------------------------------------
DRILLS = {
    # 在派发里顺手执行 —— 最常见也最隐蔽的退化
    "exec_in_due": ("            out.append(item)",
                    "            os.system(\"echo sched-drill\")\n            out.append(item)",
                    ("G2",)),
    # mark 了但不落盘 —— 内存里幂等，重启后重复
    "no_persist": ("        if out:\n            self._save()",
                   "        if False:\n            self._save()",
                   ("G3",)),
    # explain 不短路 —— 坏行会同时被说成"已停用"和"1970-01-01 已过去"，
    # 两条自相矛盾的结论并排输出，功能没崩、话却全错，只有断言数得出来
    "explain_no_shortcircuit": ("        if hit.invalid:\n            expr = getattr(hit, \"raw_expr\", \"\")",
                                "        if False:\n            expr = getattr(hit, \"raw_expr\", \"\")",
                                ("G8b",)),
    # 漏跑诊断恒空 —— 一个功能两个答案里最省事的那一半
    "silent_missed": ("    def missed_since(self, now: datetime, window_minutes: int = 1440)"
                      " -> list[dict[str, Any]]:",
                      "    def missed_since(self, now: datetime, window_minutes: int = 1440)"
                      " -> list[dict[str, Any]]:\n        return []",
                      ("G4c",)),
}


# 坏法 → 断言组的路由表（键是断言名前缀的前两位，如 G2/G3/G4/G8）
DRILL_RUNNERS = {
    "G2": lambda mod, src, tmp: g2_dispatch_only(mod, src, tmp),
    "G3": lambda mod, src, tmp: g3_restart_idempotent(mod, tmp),
    "G4": lambda mod, src, tmp: g4_views_agree(mod, tmp),
    "G8": lambda mod, src, tmp: g8_explain(mod, tmp),
}


def _sabotage(src: str, old: str, new: str) -> str:
    if src.count(old) != 1:
        raise RuntimeError("演练锚点不唯一或不存在（出现 %d 次）：%r" % (src.count(old), old[:60]))
    return src.replace(old, new)


def run_drill() -> int:
    print("--- 负向演练：每种坏法都必须让对应断言变红 ---")
    src = open(TARGET, "r", encoding="utf-8").read()
    all_ok = True
    for name, (old, new, expect_prefix) in DRILLS.items():
        tmp = tempfile.mkdtemp(prefix="drill_%s_" % name)
        try:
            bad_src = _sabotage(src, old, new)
            bad_path = os.path.join(tmp, "sched_bad.py")
            open(bad_path, "w", encoding="utf-8").write(bad_src)
            RESULTS.clear()
            mod = _load_module(bad_path, "sched_bad_" + name)
            # 只跑与坏法相关的断言组，避免噪声。
            # 这里按期望组名自动路由：早期版本是 if/elif 硬编码，新增一种坏法时
            # 忘了加分支就会被悄悄送进 g4 —— 演练照样"变红"、结论却落在错的组，
            # 属于"演练本身假绿"。路由失败就直接报错，不猜。
            grp = expect_prefix[0][:2]
            runner = DRILL_RUNNERS.get(grp)
            if runner is None:
                raise RuntimeError("演练 %s 指向的断言组 %s 没有登记 runner" % (name, grp))
            runner(mod, bad_src, tmp)
            reds = [n for n, ok, _ in RESULTS if not ok]
            hit = [n for n in reds if n.startswith(expect_prefix)]
            good = check("演练 %-12s 变红且落在 %s" % (name, "/".join(expect_prefix)),
                         bool(hit), "红项：%s" % (", ".join(reds) or "无（说明这条门禁是恒绿的）"))
            all_ok = all_ok and good
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    RESULTS.clear()
    print("DRILL-OK" if all_ok else "DRILL-FAIL")
    return 0 if all_ok else 1


# ---------------------------------------------------------------------------
def main() -> int:
    if "--drill" in sys.argv:
        return run_drill()

    src = open(TARGET, "r", encoding="utf-8").read()
    mod = _load_module(TARGET, "sched_under_test")
    tmp = tempfile.mkdtemp(prefix="probe_sched_")
    try:
        oks = [g1_selftest(),
               g2_dispatch_only(mod, src, tmp),
               g3_restart_idempotent(mod, tmp),
               g4_views_agree(mod, tmp),
               g5_calendar(mod, tmp),
               g6_cli_same_store(mod, tmp),
               g7_bad_rows_isolated(mod, tmp),
               g8_explain(mod, tmp)]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    n_fail = sum(1 for _, ok, _ in RESULTS if not ok)
    print("\n合计 %d 项断言 | 通过 %d | 失败 %d | 分组 %d/8"
          % (len(RESULTS), len(RESULTS) - n_fail, n_fail, sum(1 for o in oks if o)))
    if "--json" in sys.argv:
        print(json.dumps({"results": [{"name": n, "ok": ok, "detail": d}
                                      for n, ok, d in RESULTS]}, ensure_ascii=False))
    return 0 if n_fail == 0 and all(oks) else 1


if __name__ == "__main__":
    sys.exit(main())
