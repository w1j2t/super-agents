"""自动任务：零依赖的定时调度器（cron 5 段 + 一次性 at）。

设计上的三个自觉选择
------------------
1. **tick 不执行，只派发**：`tick()` 返回"这一分钟该跑的清单"，实际执行交给上层
   （Rust 侧任务执行器 / worker）。这样调度逻辑可以完全离线自测，不必等真任务跑完。
2. **错过不雪崩**：机器关了一整天再开，不该把欠的几十次一口气补跑。默认
   `catch_up=False` 只认准点；要严格补跑得显式开，且同一任务当天只补一次。
3. **幂等靠 last_run**：同一任务同一分钟只派发一次，落盘后崩掉再拉起来也不会重复触发。

cron 语法（够用就好，不做全套）
----------------------------
    分 时 日 月 周      例：0 8 * * *  /  */30 * * * *  /  30 14 * * 1
    支持 *  */n  a,b  a-b  a-b/n；周 0 和 7 都是周日。
不支持的写法（月份名、@daily、L/W 修饰）显式抛错——**宁可报错，不要猜**。

懒加载契约
----------
`Scheduler(...)` 构造不读盘；首次 `due()/list()/tick()` 才加载 store。
`stats()["loaded"]` 可自证。

用法
----
    python workers/_sched.py --add "每日备份" --cron "0 3 * * *" --prompt "备份到 E 盘"
    python workers/_sched.py --add "提醒开会" --at "2026-11-14 15:00"
    python workers/_sched.py --list
    python workers/_sched.py --due              # 打印此刻到期任务（不执行）
    python workers/_sched.py --explain "每日备份"   # 下次何时跑 / 上次为何没跑
    python workers/_sched.py --selftest
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_STORE = os.path.join(ROOT, "data", "scheduled.jsonl")

DOW_ALIAS = {"0": 0, "7": 0}          # 周日两种写法都认


# ---------------------------------------------------------------------------
# cron 解析
# ---------------------------------------------------------------------------
def _parse_field(expr: str, lo: int, hi: int, name: str) -> set[int]:
    """解析一个字段成取值集合。任何看不懂的写法都抛 ValueError。"""
    vals: set[int] = set()
    for part in str(expr).split(","):
        part = part.strip()
        if not part:
            raise ValueError("%s 字段有空段：%r" % (name, expr))
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            if not s.isdigit() or int(s) <= 0:
                raise ValueError("%s 步长非法：%r" % (name, expr))
            step = int(s)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            a_s, b_s = part.split("-", 1)
            if not (a_s.isdigit() and b_s.isdigit()):
                raise ValueError("%s 区间非法：%r" % (name, part))
            a, b = int(a_s), int(b_s)
        elif part.isdigit():
            a = b = int(part)
        else:
            raise ValueError("%s 字段无法解析：%r" % (name, part))
        if not (lo <= a <= hi and lo <= b <= hi) or a > b:
            raise ValueError("%s 越界：%r 允许 %d-%d" % (name, part, lo, hi))
        vals.update(range(a, b + 1, step))
    return vals


def parse_cron(expr: str) -> dict[str, set[int]]:
    """5 段 cron → 取值集合。段数不对、值越界、语法不认识，一律抛错。"""
    fields = str(expr or "").split()
    if len(fields) != 5:
        raise ValueError("cron 必须是 5 段（分 时 日 月 周），收到 %d 段：%r"
                         % (len(fields), expr))
    m = _parse_field(fields[0], 0, 59, "分")
    h = _parse_field(fields[1], 0, 23, "时")
    dom = _parse_field(fields[2], 1, 31, "日")
    mon = _parse_field(fields[3], 1, 12, "月")
    dow = {DOW_ALIAS.get(str(d), d) for d in _parse_field(fields[4], 0, 7, "周")}
    return {"minute": m, "hour": h, "day": dom, "month": mon, "dow": dow}


def cron_match(spec: dict[str, set[int]], when: datetime) -> bool:
    """标准 cron 语义：日与周都被限定时满足任一即可（cron 的老规矩）。"""
    if when.minute not in spec["minute"] or when.hour not in spec["hour"]:
        return False
    if when.month not in spec["month"]:
        return False
    dow = when.isoweekday() % 7                 # 周一=1…周六=6，周日=0
    dom_ok = when.day in spec["day"]
    dow_ok = dow in spec["dow"]
    dom_full = len(spec["day"]) >= 28
    dow_full = len(spec["dow"]) >= 7
    if not dom_full and not dow_full:
        return dom_ok or dow_ok
    return dom_ok and dow_ok


def _parse_at(text: str) -> datetime:
    """一次性任务时间：认 'YYYY-MM-DD HH:MM[:SS]' 与 ISO 的 'T' 写法。"""
    t = str(text or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(t, fmt)
        except ValueError:
            continue
    raise ValueError("无法解析时间：%r（应形如 2026-11-14 15:00）" % text)


# ---------------------------------------------------------------------------
# 任务与调度器
# ---------------------------------------------------------------------------
class Task:
    def __init__(self, raw: dict[str, Any]) -> None:
        self.id = str(raw.get("id") or "s_" + uuid.uuid4().hex[:8])
        self.title = str(raw.get("title") or "未命名任务")
        self.prompt = str(raw.get("prompt") or "")
        self.kind = str(raw.get("kind") or "cron")          # cron / once
        self.enabled = bool(raw.get("enabled", True))
        self.cron = raw.get("cron") or ""
        self.at = raw.get("at") or ""                       # once 用
        self._spec = parse_cron(self.cron) if self.kind == "cron" else None
        self.last_run = raw.get("last_run") or ""
        self.runs = int(raw.get("runs") or 0)
        self.invalid = ""
        self.late_for = ""                      # 本次是补跑哪一次准点
        # 出生时刻：补跑不得追溯任务出生之前的历史，否则新任务会凭空补跑一次
        self.created = raw.get("created") or datetime.now().isoformat(timespec="minutes")

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "prompt": self.prompt,
                "kind": self.kind, "enabled": self.enabled, "cron": self.cron,
                "at": self.at, "last_run": self.last_run, "runs": self.runs,
                "created": self.created}

    def prev_match(self, when: datetime, max_days: int = 8) -> datetime | None:
        """when 之前最近的一个准点时刻（不含 when 本身）。向前逐分钟回溯，够快。"""
        if self._spec is None:
            return None
        cur = when.replace(second=0, microsecond=0)
        for _ in range(max_days * 1440):
            cur -= timedelta(minutes=1)
            if cron_match(self._spec, cur):
                return cur
        return None

    def due_at(self, when: datetime, catch_up: bool = False) -> bool:
        """这一刻是否该跑。once 型跑过一次就不再跑。"""
        if not self.enabled:
            return False
        when = when.replace(second=0, microsecond=0)
        self.late_for = ""
        if self.kind == "once":
            if self.runs > 0:
                return False
            try:
                at = _parse_at(self.at)
            except ValueError:
                return False
            if when == at:
                return True
            if catch_up and when > at:
                self.late_for = at.isoformat(timespec="minutes")
                return True
            return False
        stamp = when.isoformat(timespec="minutes")
        if self.last_run == stamp:              # 同分钟幂等
            return False
        if cron_match(self._spec, when):
            return True
        if not catch_up:
            return False
        # 补跑：只补"上一次准点还没跑过"的那一次，且不追溯出生之前
        prev = self.prev_match(when)
        if prev is None:
            return False
        lower = self.last_run or self.created
        if lower and lower >= prev.isoformat(timespec="minutes"):
            return False
        self.late_for = prev.isoformat(timespec="minutes")
        return True

    def next_match(self, when: datetime, max_days: int = 8) -> datetime | None:
        """when 之后最近的一个准点时刻（不含 when 本身）；无则 None。

        与 prev_match 对称：一个回答"上次该是什么时候"，一个回答"下次什么时候"。
        逐分钟推进，8 天窗口在毫秒级——不值得为此引入 cron 解析库。
        """
        if self.kind == "once":
            try:
                at = _parse_at(self.at)
            except ValueError:
                return None
            return at if at > when else None
        if self._spec is None:
            return None
        cur = when.replace(second=0, microsecond=0)
        for _ in range(max_days * 1440):
            cur += timedelta(minutes=1)
            if cron_match(self._spec, cur):
                return cur
        return None

    def mark(self, when: datetime) -> None:
        self.last_run = when.replace(second=0, microsecond=0).isoformat(timespec="minutes")
        self.runs += 1
        if self.kind == "once":
            self.enabled = False


class Scheduler:
    """任务仓库 + 到期判定。构造零副作用，首次查询才读盘。"""

    def __init__(self, store: str = DEFAULT_STORE, catch_up: bool = False) -> None:
        self.store = store
        self.catch_up = catch_up
        self._tasks: list[Task] = []
        self._loaded = False
        self.load_ms = 0.0
        self.lazy_loads = 0

    def _load(self) -> None:
        t0 = time.perf_counter()
        self._tasks = []
        if os.path.isfile(self.store):
            with open(self.store, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError:
                        continue        # 坏行跳过：不该让调度器因为一行脏数据起不来
                    try:
                        self._tasks.append(Task(raw))
                    except ValueError as exc:
                        # 存了非法 cron：保留条目并标注原因，而不是静默丢掉。
                        # kind 降级成 once + 哨兵时刻 1970 + 停用，是为了让它彻底不外溢触发。
                        # 但**哨兵值不能当用户配置讲出去**：--explain 必须能说出用户当初写的
                        # 是什么表达式，否则"表达式非法"等于让人自己回去翻文件。故把原始的
                        # cron/at 另存成 raw_expr（不参与判定、不落盘）。
                        t = Task({"id": raw.get("id"), "title": raw.get("title"),
                                  "prompt": raw.get("prompt"), "kind": "once",
                                  "at": "1970-01-01 00:00", "enabled": False})
                        t.invalid = str(exc)
                        t.raw_expr = raw.get("cron") or raw.get("at") or ""
                        self._tasks.append(t)
        self._loaded = True
        self.lazy_loads += 1
        self.load_ms = round((time.perf_counter() - t0) * 1000, 1)

    def _save(self) -> None:
        d = os.path.dirname(self.store)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        tmp = self.store + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for t in self._tasks:
                fh.write(json.dumps(t.to_dict(), ensure_ascii=False) + "\n")
        os.replace(tmp, self.store)         # 原子替换：写到一半断电不毁原文件

    # -- 增删查 -------------------------------------------------------------
    def add(self, title: str, prompt: str = "", cron: str = "",
            at: str = "") -> dict[str, Any]:
        if not self._loaded:
            self._load()
        if cron:
            parse_cron(cron)                # 先校验再落盘：非法表达式不进仓库
            t = Task({"title": title, "prompt": prompt, "kind": "cron", "cron": cron})
        elif at:
            _parse_at(at)
            t = Task({"title": title, "prompt": prompt, "kind": "once", "at": at})
        else:
            raise ValueError("必须给 cron 或 at 之一")
        self._tasks.append(t)
        self._save()
        return t.to_dict()

    def remove(self, key: str) -> bool:
        if not self._loaded:
            self._load()
        n = len(self._tasks)
        self._tasks = [t for t in self._tasks if t.id != key and t.title != key]
        if len(self._tasks) != n:
            self._save()
            return True
        return False

    def list(self) -> list[dict[str, Any]]:
        if not self._loaded:
            self._load()
        out = []
        for t in self._tasks:
            d = t.to_dict()
            if t.invalid:
                d["invalid"] = t.invalid
            out.append(d)
        return out

    def due(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """返回此刻到期任务清单并标记已触发。**不执行任何 prompt**。"""
        if not self._loaded:
            self._load()
        now = (now or datetime.now()).replace(second=0, microsecond=0)
        out = []
        for t in self._tasks:
            if not t.due_at(now, catch_up=self.catch_up):
                continue
            item = {"id": t.id, "title": t.title, "prompt": t.prompt,
                    "kind": t.kind, "fired_at": now.isoformat(timespec="minutes")}
            if t.late_for:
                item["late_for"] = t.late_for     # 补跑时说明补的是哪一次准点
            out.append(item)
            t.mark(now)
        if out:
            self._save()
        return out

    def tick(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """`due()` 的别名，语义上强调"被调度器心跳调用"。"""
        return self.due(now)

    def missed_since(self, now: datetime, window_minutes: int = 1440) -> list[dict[str, Any]]:
        """诊断用：过去 window 内本该跑、却一次都没跑的任务。"""
        if not self._loaded:
            self._load()
        now = now.replace(second=0, microsecond=0)
        cutoff = (now - timedelta(minutes=window_minutes)).isoformat(timespec="minutes")
        res = []
        for t in self._tasks:
            if not t.enabled or t.kind != "cron":
                continue
            prev = t.prev_match(now, max_days=8)
            if prev is None:
                continue
            pv = prev.isoformat(timespec="minutes")
            if pv < cutoff:
                continue                          # 该跑的准点不在窗口内，不算漏
            if not t.last_run or t.last_run < pv:
                res.append({"id": t.id, "title": t.title,
                            "should_run_at": pv, "last_run": t.last_run or "从未"})
        return res

    def explain(self, name: str, now: datetime | None = None) -> dict[str, Any]:
        """回答两个最常被追问的问题：**下次什么时候跑**、**上次为什么没跑**。

        调度器最难查的故障是沉默：任务没跑，日志里什么也没有。到底是表达式写错、
        被停用、还是压根没到点？这三件事在库里都有据可查，只是以前要人肉比对
        cron 和 last_run。这里把它们拼成一句话给出来。

        只读，不改任何状态（不 mark、不落盘）——诊断工具绝不能顺手改变被诊断对象。
        """
        if not self._loaded:
            self._load()
        now = (now or datetime.now()).replace(second=0, microsecond=0)
        key = (name or "").strip()

        hit = next((t for t in self._tasks if key and key in (t.id, t.title)), None)
        if hit is None:
            fuzzy = [t for t in self._tasks if key and key in t.title]
            if len(fuzzy) > 1:
                return {"found": False, "reason": "名称匹配到多个任务，请用 id 精确指定",
                        "candidates": [{"id": t.id, "title": t.title} for t in fuzzy]}
            if len(fuzzy) == 1:
                hit = fuzzy[0]
        if hit is None:
            return {"found": False, "reason": "没有这个任务（按 id 或标题都找不到）",
                    "known": [{"id": t.id, "title": t.title} for t in self._tasks]}

        nxt = hit.next_match(now)
        prev = hit.prev_match(now) if hit.kind == "cron" else None
        pv = prev.isoformat(timespec="minutes") if prev else ""
        # 两级结论：problems = 有据可判的故障（非法/停用/时刻已过）；
        # warnings = 事实陈述，是否算故障取决于心跳节奏，工具不替用户下结论。
        # 混成一级会让"每分钟心跳"和"五分钟心跳"得到同样严厉的结论，那是误报。
        problems: list[str] = []
        warnings: list[str] = []

        # 非法表达式一律短路：坏行被降级成「once + 1970 + 停用」，这三样都是内部哨兵，
        # 继续往下判会得到"任务已停用""设定时刻 1970-01-01 已过去"这类**自相矛盾**的
        # 结论——同一个任务被同时说成"不会跑"和"有具体时刻"。用户只该看到一句话：
        # 表达式非法，写出你当初写的是什么。
        if hit.invalid:
            expr = getattr(hit, "raw_expr", "") or hit.cron or hit.at
            problems.append("表达式非法（%s），永远不会被派发：%s" % (hit.invalid, expr))
        elif not hit.enabled:
            if hit.kind == "once" and hit.runs > 0:
                problems.append("一次性任务已经执行过（%s），不会再跑" % (hit.last_run or "?"))
            else:
                problems.append("任务已停用")
        elif hit.kind == "cron" and hit._spec is None:
            problems.append("cron 表达式为空，没有可判定的时刻")
        elif hit.kind == "once" and hit.runs == 0 and nxt is None:
            try:
                if _parse_at(hit.at) <= now:
                    problems.append("设定时刻 %s 已过去，且补跑没开（--catch-up）" % hit.at)
            except ValueError:
                problems.append("at 无法解析：%r" % hit.at)

        # 「上次为什么没跑」：先看有没有跑过，再看该跑的准点有没有被越过。
        if hit.last_run:
            last = "上次已跑 %s（累计 %d 次）" % (hit.last_run, hit.runs)
            if pv and hit.last_run < pv:
                warnings.append("之后的准点 %s 尚未执行（心跳够密的话，这就是漏跑）" % pv)
        else:
            last = "从未跑过"
            if pv:
                last += "；最近一个准点是 %s" % pv
                # 出生早于该准点 → 它本该跑；出生晚于它 → 正常，只是还没到第一次。
                # 停用的任务不参与这一判断：它本来就不该跑，说它"漏跑"是拿正常状态当故障。
                if not hit.enabled:
                    last += "（任务停用中，本就不跑）"
                elif hit.created <= pv:
                    problems.append("任务出生（%s）早于准点 %s，本该跑却没跑（查心跳/进程）"
                                    % (hit.created, pv))
                else:
                    last += "（任务出生晚于它，属正常）"

        nxt_s = nxt.isoformat(timespec="minutes") if nxt else ""
        if problems:
            verdict = "有问题：" + "；".join(problems)
        elif warnings:
            verdict = "基本正常，但注意：" + "；".join(warnings)
        elif nxt_s:
            verdict = "正常，下一次 %s" % nxt_s
        else:
            verdict = "正常，但已无后续时刻"

        return {"found": True, "id": hit.id, "title": hit.title, "kind": hit.kind,
                "expr": getattr(hit, "raw_expr", "") or (hit.cron if hit.kind == "cron" else hit.at),
                "enabled": hit.enabled, "created": hit.created,
                "last_run": hit.last_run or "从未", "runs": hit.runs,
                "prev_should_run": pv, "next_run": nxt_s,
                "last": last, "problems": problems, "warnings": warnings,
                "verdict": verdict}

    def stats(self) -> dict[str, Any]:
        return {"store": self.store, "loaded": self._loaded, "tasks": len(self._tasks),
                "lazy_loads": self.lazy_loads, "load_ms": self.load_ms,
                "catch_up": self.catch_up}


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
def _selftest() -> int:
    import shutil
    import tempfile

    rc = 0
    tmp = tempfile.mkdtemp(prefix="sched_")
    store = os.path.join(tmp, "s.jsonl")

    def chk(cond, ok_msg, fail_msg):
        nonlocal rc
        if cond:
            print("[OK ] " + ok_msg)
        else:
            print("[FAIL] " + fail_msg)
            rc = 1

    try:
        # 1) 懒加载契约
        s = Scheduler(store=store)
        chk(s.stats()["loaded"] is False and s.lazy_loads == 0,
            "懒加载契约：构造后未读盘", "构造即读盘：%s" % s.stats())

        # 2) cron 解析：合法与非法
        good = parse_cron("*/30 8-18 * * 1-5")
        chk(good["minute"] == {0, 30} and len(good["hour"]) == 11
            and good["dow"] == {1, 2, 3, 4, 5},
            "cron 解析：*/n 与 a-b 正确", "cron 解析错误：%s" % good)
        bad_ok = 0
        for bad in ("0 8 * *", "60 8 * * *", "0 8 * * 9", "a b c d e", "*/0 * * * *"):
            try:
                parse_cron(bad)
            except ValueError:
                bad_ok += 1
        chk(bad_ok == 5, "非法 cron 全部被拒（5/5）：宁可报错不猜",
            "非法 cron 有漏网：只拒了 %d/5" % bad_ok)

        # 3) 到期判定与幂等（2026-11-16 是周一）
        t = datetime(2026, 11, 16, 8, 0)
        sch = Scheduler(store=store)
        sch.add("周一早八", prompt="汇报", cron="0 8 * * 1")
        d1, d2 = sch.due(t), sch.due(t)
        chk(len(d1) == 1 and len(d2) == 0,
            "到期判定与幂等：首派 1 条、同分钟再派 0 条", "幂等失效：%s / %s" % (d1, d2))
        chk(sch.due(datetime(2026, 11, 17, 8, 0)) == [],
            "周限定生效：周二同点不触发", "周限定未生效")

        # 4) 落盘往返
        items = Scheduler(store=store).list()
        chk(len(items) == 1 and items[0]["last_run"] == "2026-11-16T08:00",
            "持久化往返：last_run 落盘正确", "持久化异常：%s" % items)

        # 5) 一次性任务：跑过即停
        sch2 = Scheduler(store=store)
        sch2.add("一次性", prompt="提醒", at="2026-11-20 15:30")
        one = sch2.due(datetime(2026, 11, 20, 15, 30))
        again = sch2.due(datetime(2026, 11, 20, 15, 30))
        chk(len(one) == 1 and again == [],
            "一次性任务：命中一次后自动停用", "一次性任务重复触发：%s %s" % (one, again))

        # 6) 错过补偿：默认不雪崩、开了只补一次
        strict = Scheduler(store=os.path.join(tmp, "s2.jsonl"), catch_up=False)
        strict.add("每日三点", cron="0 3 * * *")
        chk(strict.due(datetime(2026, 11, 21, 9, 0)) == [],
            "错过不雪崩：默认只认准点（迟到不补跑）", "默认策略误补跑")
        catch = Scheduler(store=os.path.join(tmp, "s3.jsonl"), catch_up=True)
        catch.add("每日三点", cron="0 3 * * *")
        chk(len(catch.due(datetime(2026, 11, 21, 9, 0))) == 1,
            "显式补跑：catch_up=True 时迟到可补", "补跑模式失效")
        chk(catch.due(datetime(2026, 11, 21, 23, 0)) == [],
            "补跑不连环：同一任务当天只补一次", "补跑连环触发")

        # 7) 脏数据不能让调度器起不来，且非法项要标出来
        p = os.path.join(tmp, "bad.jsonl")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write('{"title":"坏的","kind":"cron","cron":"oops"}\n')
            fh.write("这不是 json\n")
            fh.write('{"title":"好的","kind":"cron","cron":"0 1 * * *"}\n')
        bad = Scheduler(store=p)
        lst = bad.list()
        flagged = [x for x in lst if x.get("invalid")]
        chk(len(lst) == 2 and len(flagged) == 1 and "cron" in flagged[0]["invalid"],
            "脏数据容错：坏行/非法 cron 被隔离并标注，好任务照常可用",
            "脏数据容错失败：%s" % lst)

        # 8) 诊断：漏跑能被发现
        miss = Scheduler(store=os.path.join(tmp, "m.jsonl"))
        miss.add("四点半", cron="30 4 * * *")
        found = miss.missed_since(datetime(2026, 11, 21, 12, 0), window_minutes=1440)
        chk(len(found) == 1 and found[0]["last_run"] == "从未",
            "漏跑诊断：过去窗口内该跑未跑能被捞出", "漏跑诊断失灵：%s" % found)

        # 9) 非法输入必须当场拒绝，不能落盘
        rejected = 0
        for kw in ({"cron": "0 8 * *"}, {"cron": "不行"}, {"at": "明天下午"}):
            try:
                miss.add("坏任务", **kw)
            except ValueError:
                rejected += 1
        chk(rejected == 3 and len(miss.list()) == 1,
            "非法输入当场拒绝且不落盘（3/3）", "非法输入被放行：拒绝 %d/3" % rejected)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return rc


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return _selftest()

    def arg(n, d=""):
        return argv[argv.index(n) + 1] if n in argv else d

    s = Scheduler(store=arg("--store", DEFAULT_STORE), catch_up="--catch-up" in argv)
    if "--add" in argv:
        try:
            t = s.add(arg("--add"), prompt=arg("--prompt"), cron=arg("--cron"),
                      at=arg("--at"))
            print(json.dumps(t, ensure_ascii=False, indent=2))
        except ValueError as exc:
            print("添加失败：%s" % exc)
            return 2
    if "--remove" in argv:
        print("已删除" if s.remove(arg("--remove")) else "未找到该任务")
    if "--list" in argv:
        for it in s.list():
            tag = "一次" if it["kind"] == "once" else it["cron"]
            print("%-12s %-6s %-14s 上次=%s%s   %s"
                  % (it["id"], "启用" if it["enabled"] else "停用", tag,
                     it["last_run"] or "从未", "  ⚠非法" if it.get("invalid") else "",
                     it["title"]))
    if "--due" in argv:
        got = s.due()
        print(json.dumps({"due": got, "count": len(got)}, ensure_ascii=False, indent=2))
    if "--explain" in argv:
        info = s.explain(arg("--explain"))
        if not info.get("found"):
            print(info.get("reason") or "查不到该任务")
            for c in info.get("candidates", []) + info.get("known", []):
                print("  %-12s %s" % (c["id"], c["title"]))
            return 2
        print("任务  %s（%s）" % (info["title"], info["id"]))
        print("规则  %s  %s" % ("一次 · " + info["expr"] if info["kind"] == "once" else info["expr"],
                               "启用" if info["enabled"] else "停用"))
        print("上次  %s" % info["last"])
        print("下次  %s" % (info["next_run"] or "无后续时刻"))
        for p in info["problems"]:
            print("问题  %s" % p)
        for w in info["warnings"]:
            print("提示  %s" % w)
        print("结论  %s" % info["verdict"])
    if not any(k in argv for k in ("--add", "--remove", "--list", "--due", "--explain")):
        print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
