#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_alerting.py —— 可观测与告警（capability `observability_alerting`）的行为门禁。

为什么要它
----------
告警是最容易"做得像"的功能：接口能返回、字段齐全、文档写着"5 分钟去重"，
但阈值算错、去重窗口没生效、风暴降级时把严重告警一起折掉——这三件事肉眼都看不出来，
只会在真出事的那天暴露。所以本探针不测"接口 200"，而是把阈值压到必然命中，
再逐段断言**数字**：

    命中 → 窗口内抑制 → 过窗口复发 → 风暴只留严重级别 → 指标真的落了点

三个阶段各起一个独立进程：策略参数只能由环境变量在启动时给定，一个进程里改不了，
硬凑在同一个进程里就只能靠"祈祷参数顺序"，那是假门禁。

  A 默认策略（dedup=300s / storm=5）：三条规则命中、去重、指标落点、auto_action 口径
  B 短窗口（dedup=1s）：窗口内压制、窗口后复发、first_fired_at 保留 / last_seen_at 递增
  C 风暴（storm=0）：非严重被折叠、严重仍保留

负向演练（证明判据有牙齿）
--------------------------
    python scripts/probe_alerting.py --drill-no-dedup
把去重窗口置 0（服务端此时必须"不去重"），A8/B3 类断言应当转红。
这是**等效客户端手法**：项目目录没有 git，改 Rust 源码做"去重坏掉的二进制"无法可靠回退，
所以用同一条代码路径上的开关把功能关掉来验判据，并在证据里如实标注手法。

用法：
    python scripts/probe_alerting.py                    # 门禁 19 正常跑（约 12 秒）
    python scripts/probe_alerting.py --keep-logs        # 保留每阶段服务日志（本来就保留）
    python scripts/probe_alerting.py --drill-no-dedup   # 负向演练，期望红
    python scripts/probe_alerting.py --exe <path>       # 指定二进制
退出码：0 = 全过；1 = 有失败项。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _paths import default_exe  # noqa: E402

# 写死过桌面 build_sa1，那个镜像已不存在：单独跑本脚本永远"找不到可执行文件"，
# 报错还指向空目录，看着像没编译。统一走镜像解析规则。
DEFAULT_EXE = default_exe()
METRICS_TOKEN = "alert-probe-metrics-token-0123456789"
# 每次运行独占一个目录：探针要反复起停进程、反复注册同一批用户名，
# 复用固定目录会在第二次运行时撞 409（用户已存在），把"服务坏了"和"数据脏了"混为一谈。
# 只新建不删除，跑过多少轮目录都在，便于事后翻日志。
RUN_ID = time.strftime("%Y%m%d-%H%M%S")

results: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  ->  {detail}" if detail else ""), flush=True)


def note(line: str) -> None:
    print(f"       {line}", flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Client:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def req(self, method: str, path: str, body=None, token: str | None = None, timeout=60):
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        r = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                try:
                    return resp.status, json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    return resp.status, raw
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return e.code, raw
        except Exception as e:
            return 0, {"err": f"{type(e).__name__}: {e}"}

    # --- 业务快捷方式 -------------------------------------------------------
    def register(self, name: str):
        st, b = self.req("POST", "/api/v1/auth/register", {
            "username": name, "email": f"{name}@example.test", "password": "Alert-Probe-Passw0rd!",
        })
        tok = ""
        if isinstance(b, dict):
            tok = (b.get("access_token")
                   or (b.get("tokens") or {}).get("access_token")
                   or "")
        return st, tok

    def alerts(self, tok: str):
        st, b = self.req("GET", "/api/v1/observability/alerts", token=tok)
        return st, (b if isinstance(b, dict) else {})

    def metrics_text(self) -> str:
        r = urllib.request.Request(self.base + "/metrics",
                                   headers={"Authorization": f"Bearer {METRICS_TOKEN}"})
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as e:
            note(f"/metrics 抓取失败：{type(e).__name__}: {e}")
            return ""


def metric_sum(text: str, name: str, label_substr: str = "") -> int:
    """求某计数器之和。label_substr 非空时只累加标签行里含该子串的样本。

    坑：无标签样本行是 `name<空格>值`，有标签是 `name{...}<空格>值`，而 `# TYPE name counter`
    这类注释行也以 name 开头。三者必须区分清楚——第一版只放行 `{` 开头的 rest，
    把无标签样本整行跳过，于是所有"全局计数器"都读成 0，看起来像"打点没生效"。
    """
    total = 0
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        if not line.startswith(name):
            continue
        rest = line[len(name):]
        if rest.startswith("_"):
            continue  # 名字只是前缀（sa_alerts_total vs sa_alerts_total_xxx）
        if rest and not (rest.startswith("{") or rest.startswith(" ")):
            continue
        if label_substr and label_substr not in rest:
            continue
        try:
            total += int(float(line.rsplit(" ", 1)[1]))
        except (ValueError, IndexError):
            continue
    return total


def by_rule(payload: dict) -> dict:
    return {a.get("rule"): a for a in (payload.get("active") or [])}


def stats_of(payload: dict) -> dict:
    return payload.get("stats") or {}


def evaluated_of(payload: dict) -> dict:
    return payload.get("evaluated") or {}


# --------------------------------------------------------------------------- #
# 进程托管
# --------------------------------------------------------------------------- #
class Server:
    def __init__(self, exe: pathlib.Path, stage: str, extra_env: dict):
        self.port = free_port()
        run_dir = ROOT / "temp" / "_probe_alerting" / f"{RUN_ID}"
        self.data_dir = run_dir / stage
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = run_dir / f"{stage}-server.log"
        self.logf = open(self.log_path, "w", encoding="utf-8", errors="replace")
        env = dict(os.environ)
        env.update({
            "SA_DATA_DIR": str(self.data_dir),
            "SA_BIND": f"127.0.0.1:{self.port}",
            "SA_ENV": "dev",
            "SA_JWT_SECRET": "alert-probe-secret-0123456789abcdef012345",
            "SA_METRICS_TOKEN": METRICS_TOKEN,
            "SA_ALLOW_REGISTRATION": "true",
            "SA_STORAGE": "json",
            "SA_RUNNER": "sim",
            "SA_AUDIT": "memory",
            "SA_LIMITER": "memory",
            "SA_TASK_SUBMIT_QUOTA_PER_HOUR": "600",
        })
        env.update(extra_env)
        for k in [k for k in env if k.startswith("SA_LLM_")]:
            env.pop(k)  # 探针不能因为开发机配了真模型就变成分钟级
        self.proc = subprocess.Popen(
            [str(exe)], env=env, stdout=self.logf, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        self.base = f"http://127.0.0.1:{self.port}"
        note(f"[{stage}] 端口 {self.port} | 数据 {self.data_dir} | 日志 {self.log_path}")

    def wait_ready(self, timeout=60.0) -> bool:
        c = Client(self.base)
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < timeout:
            st, b = c.req("GET", "/readyz", timeout=5)
            if st == 200 and isinstance(b, dict) and b.get("ready"):
                return True
            if self.proc.poll() is not None:
                return False
            time.sleep(0.3)
        return False

    def stop(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=15)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        try:
            self.logf.close()
        except Exception:
            pass

    def tail_log(self, n=1600) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")[-n:]
        except Exception:
            return ""


# --------------------------------------------------------------------------- #
# 改造流量：429 / 失败任务 / 403 —— 都是真请求真落点，不 mock
# --------------------------------------------------------------------------- #
def make_429(c: Client, tok: str, quota: int):
    """提交 quota+2 次：前 quota 次受理，后面必须 429。返回状态码序列。"""
    codes = []
    for i in range(quota + 2):
        st, _ = c.req("POST", "/api/v1/tasks",
                      {"goal": f"告警门禁：造第 {i + 1} 次提交（用于验证限流告警）",
                       "task_type": "research"}, token=tok)
        codes.append(st)
    return codes


def make_failures(c: Client, tok: str, n: int):
    """预算压到 1 token 的任务必然 aborted_budget，会计入 sa_task_failures_total。
    这比"伪造一条失败记录"诚实：它走的是真实的降级路径。"""
    out = []
    for i in range(n):
        st, b = c.req("POST", "/api/v1/tasks",
                      {"goal": f"告警门禁：预算超支任务 {i + 1}", "task_type": "writing",
                       "mode": "sync"}, token=tok, timeout=120)
        out.append((st, (b or {}).get("status") if isinstance(b, dict) else None))
    return out


def make_403(c: Client, other_tok: str):
    """普通成员访问管理员面，产生真 403（同时也是 sa_authz_denied_total 的落点）。"""
    codes = []
    for path in ("/api/v1/observability/alerts", "/api/v1/observability/overview"):
        st, _ = c.req("GET", path, token=other_tok)
        codes.append(st)
    return codes


# --------------------------------------------------------------------------- #
# 阶段 A：默认策略 —— 命中 / 去重 / 指标 / auto_action
# --------------------------------------------------------------------------- #
def stage_a(exe: pathlib.Path) -> None:
    srv = Server(exe, "A-default", {
        "SA_TASK_SUBMIT_QUOTA_PER_HOUR": "3",
        "SA_TASK_BUDGET_TOKENS": "1",
        "SA_ALERT_RATE_LIMITED_HITS": "1",
        "SA_ALERT_AUTHZ_DENIED_HITS": "1",
        "SA_ALERT_CIRCUIT_OPEN_HITS": "1",
        "SA_ALERT_MIN_TASKS": "1",
        "SA_ALERT_TASK_FAILURE_RATIO": "0.01",
        "SA_ALERT_MIN_HTTP": "1",
        "SA_ALERT_5XX_RATIO": "0.01",
        "SA_ALERT_DEDUP_SECS": "300",
        "SA_ALERT_STORM_THRESHOLD": "5",
    })
    try:
        ready = srv.wait_ready()
        rec("A0 服务就绪（/readyz ready=true）", ready)
        if not ready:
            note(srv.tail_log())
            return
        c = Client(srv.base)

        st, admin = c.register("alert_admin")
        st2, member = c.register("alert_member")
        rec("A0b 注册管理员与普通成员", st == 201 and st2 == 201 and bool(admin) and bool(member),
            f"status={st}/{st2}")

        # A1 造流量前先抓一次：空白基线。任何"一启动就报警"的实现都会在这里露馅。
        st, base = c.alerts(admin)
        rec("A1 无流量时无告警", st == 200 and base.get("count") == 0 and not base.get("active"),
            f"status={st} count={base.get('count')}")

        # A2 策略回显：门禁自己压的阈值必须真的生效，否则后面全是假绿
        rules = base.get("rules") or []
        sev = {r.get("id"): r.get("severity") for r in rules if isinstance(r, dict)}
        pol = base.get("policy") or {}
        rec("A2 策略回显（5 条规则 / dedup 300s / storm 5）",
            len(rules) == 5 and pol.get("dedup_window_secs") == 300 and pol.get("storm_threshold") == 5,
            f"rules={len(rules)} dedup={pol.get('dedup_window_secs')} storm={pol.get('storm_threshold')}")
        rec("A2b 严重级别口径（3 条 critical / 2 条 warning）",
            sev.get("http_5xx_ratio") == "critical" and sev.get("authz_denied_spike") == "critical"
            and sev.get("circuit_open") == "critical" and sev.get("task_failure_ratio") == "warning"
            and sev.get("rate_limited_spike") == "warning",
            json.dumps(sev, ensure_ascii=False))

        # A3 造失败任务（真实降级路径 aborted_budget，不是伪造状态）。
        # 注意顺序：这一步会**正好用光**本进程的 3 次提交配额（quota=3），
        # 所以紧接着的提交必然是 429——两件事一批造，不用第二次起进程。
        fails = make_failures(c, admin, 3)
        statuses = [s for _, s in fails]
        rec("A3 预算超支任务收敛为 aborted_budget", statuses == ["aborted_budget"] * 3, str(statuses))

        # A4 配额用光后必须 429（限流的真实落点）
        codes = []
        for _ in range(2):
            st, _ = c.req("POST", "/api/v1/tasks",
                          {"goal": "告警门禁：配额已用光，本次必须被限流", "task_type": "research"},
                          token=admin)
            codes.append(st)
        rec("A4 配额用光后提交被限流（429×2）", codes == [429, 429], str(codes))

        # A5 造 403
        c403 = make_403(c, member)
        rec("A5 越权访问管理面被拒（403×2）", c403 == [403, 403], str(c403))

        # A6 新补的 authz 打点真的落了地（此前该指标零落点，规则永远哑）
        mt = c.metrics_text()
        denied = metric_sum(mt, "sa_authz_denied_total")
        rec("A6 sa_authz_denied_total 有真实落点（≥2）", denied >= 2, f"值={denied}")

        # A7 首次评估：三条规则同时命中，且 observed/threshold 是算出来的不是抄的
        st, a1 = c.alerts(admin)
        r = by_rule(a1)
        ev = evaluated_of(a1)
        ok7 = (st == 200 and ev.get("fired") == 3
               and set(r) == {"rate_limited_spike", "task_failure_ratio", "authz_denied_spike"})
        rec("A7 首抓命中三条规则（fired=3）", ok7,
            f"status={st} fired={ev.get('fired')} active={sorted(r)}")
        rl = r.get("rate_limited_spike") or {}
        tf = r.get("task_failure_ratio") or {}
        rec("A7b 限流告警数值正确（observed=2 / threshold=1 / hits=1）",
            rl.get("observed") == 2.0 and rl.get("threshold") == 1.0 and rl.get("hits") == 1,
            f"observed={rl.get('observed')} threshold={rl.get('threshold')} hits={rl.get('hits')}")
        rec("A7c 失败率告警数值正确（observed=1.0=100% / threshold=0.01）",
            tf.get("observed") == 1.0 and tf.get("threshold") == 0.01,
            f"observed={tf.get('observed')} threshold={tf.get('threshold')}")
        rec("A7d 告警带可读归因文字", bool(rl.get("detail")) and "限流" in (rl.get("detail") or ""),
            str(rl.get("detail"))[:80])

        # A8 去重：窗口内再抓两次，必须一条都不新增，但要记"被压制了几条"
        hits_seq = [rl.get("hits")]
        fired_seq, dedup_seq = [], []
        for _ in range(2):
            st, b = c.alerts(admin)
            ev = evaluated_of(b)
            fired_seq.append(ev.get("fired"))
            dedup_seq.append(len(ev.get("deduped") or []))
            hits_seq.append((by_rule(b).get("rate_limited_spike") or {}).get("hits"))
        rec("A8 去重窗口内不重复发（fired=0,0）", fired_seq == [0, 0], str(fired_seq))
        rec("A8b 被压制条目逐个记录（deduped=3,3）", dedup_seq == [3, 3], str(dedup_seq))
        rec("A8c 命中次数继续累加（hits 1→2→3）", hits_seq == [1, 2, 3], str(hits_seq))

        st, a3 = c.alerts(admin)
        stt = stats_of(a3)
        rec("A8d 抑制总数与活跃数口径一致（suppressed=9 / active=3，共 3 次窗口内抓取）",
            stt.get("suppressed_total") == 9 and stt.get("active_total") == 3,
            f"suppressed={stt.get('suppressed_total')} active={stt.get('active_total')} "
            f"fired={stt.get('fired_total')}")

        # A9 指标落点：告警自己也要被监控，否则告警系统是黑盒。
        # 这里验的是**守恒**：命中次数 = 新发出（3） + 被压制（9）。
        # sa_alerts_total 记的是"命中"，不是"发出"——因为去重不等于指标丢失，
        # 若把压下的样本从计数器里删掉，Prometheus 侧就看不出"一直在命中但没发"，
        # 反而更容易误判成告警坏了。这个口径必须写进文档，否则会有人拿它当告警数。
        mt = c.metrics_text()
        fired_warn = metric_sum(mt, "sa_alerts_total", 'severity="warning"')
        fired_crit = metric_sum(mt, "sa_alerts_total", 'severity="critical"')
        deduped = metric_sum(mt, "sa_alerts_deduped_total")
        # 4 轮评估（A7 首抓 + A8 两次 + A8d 一次），每轮命中 2 warning + 1 critical
        rec("A9 sa_alerts_total 按 severity 落点（warning=8 / critical=4 = 4 轮 × 2+1）",
            fired_warn == 8 and fired_crit == 4, f"warning={fired_warn} critical={fired_crit}")
        rec("A9b sa_alerts_deduped_total 落点（=9，与 stats.suppressed_total 一致）",
            deduped == 9 and deduped == stt.get("suppressed_total"), f"指标={deduped} stats={stt.get('suppressed_total')}")
        rec("A9c 命中＝发出＋压制 守恒（12 = 3 + 9）",
            fired_warn + fired_crit == stt.get("fired_total") + deduped,
            f"{fired_warn + fired_crit} vs {stt.get('fired_total')} + {deduped}")

        # A10 auto_action 不能是写死的 none：本进程里有预算超支，必须说得出"谁在兜底"
        aa = a3.get("auto_action") or {}
        rec("A10 auto_action 反映真实兜底机制（budget_downgrade）",
            aa.get("kind") == "budget_downgrade" and "降档" in (aa.get("detail") or ""),
            json.dumps(aa, ensure_ascii=False)[:120])
    finally:
        srv.stop()


# --------------------------------------------------------------------------- #
# 阶段 B：短窗口 —— 窗口内压制 / 窗口后复发 / 首次时间保留
# --------------------------------------------------------------------------- #
def stage_b(exe: pathlib.Path, fast: bool = False) -> None:
    srv = Server(exe, "B-refire", {
        "SA_TASK_SUBMIT_QUOTA_PER_HOUR": "1",
        "SA_ALERT_RATE_LIMITED_HITS": "1",
        "SA_ALERT_AUTHZ_DENIED_HITS": "999",
        "SA_ALERT_MIN_TASKS": "999",
        "SA_ALERT_MIN_HTTP": "999",
        "SA_ALERT_DEDUP_SECS": "1",
        "SA_ALERT_STORM_THRESHOLD": "5",
    })
    try:
        ready = srv.wait_ready()
        rec("B0 服务就绪", ready)
        if not ready:
            note(srv.tail_log())
            return
        c = Client(srv.base)
        _, tok = c.register("alert_refire")
        codes = make_429(c, tok, quota=1)
        rec("B0b 造出一次限流（第二次提交 429）", codes == [202, 429, 429], str(codes))

        st, b1 = c.alerts(tok)
        e1 = evaluated_of(b1)
        a_first = by_rule(b1).get("rate_limited_spike") or {}
        rec("B1 首抓发出（fired=1）", e1.get("fired") == 1, f"fired={e1.get('fired')}")

        st, b2 = c.alerts(tok)
        e2 = evaluated_of(b2)
        a_second = by_rule(b2).get("rate_limited_spike") or {}
        rec("B2 窗口内再抓被压制（fired=0）", e2.get("fired") == 0, f"fired={e2.get('fired')}")

        if not fast:
            time.sleep(1.4)
        st, b3 = c.alerts(tok)
        e3 = evaluated_of(b3)
        a_third = by_rule(b3).get("rate_limited_spike") or {}
        rec("B3 窗口过期后复发（fired=1）", e3.get("fired") == 1, f"fired={e3.get('fired')}")

        rec("B4 复发不篡改首次发生时间（first_fired_at 保持一致）",
            bool(a_first.get("first_fired_at"))
            and a_first.get("first_fired_at") == a_second.get("first_fired_at") == a_third.get("first_fired_at"),
            f"first={a_first.get('first_fired_at')}")
        rec("B4b 最后出现时间随复发推进（last_seen_at 递增）",
            bool(a_third.get("last_seen_at")) and a_third.get("last_seen_at") >= a_first.get("last_seen_at"),
            f"{a_first.get('last_seen_at')} -> {a_third.get('last_seen_at')}")
        rec("B4c 命中累计到 3 次（hits=3）", a_third.get("hits") == 3, f"hits={a_third.get('hits')}")
    finally:
        srv.stop()


# --------------------------------------------------------------------------- #
# 阶段 C：风暴 —— 只留严重级别，其余折叠
# --------------------------------------------------------------------------- #
def stage_c(exe: pathlib.Path) -> None:
    srv = Server(exe, "C-storm", {
        "SA_TASK_SUBMIT_QUOTA_PER_HOUR": "1",
        "SA_ALERT_RATE_LIMITED_HITS": "1",
        "SA_ALERT_AUTHZ_DENIED_HITS": "1",
        "SA_ALERT_MIN_TASKS": "999",
        "SA_ALERT_MIN_HTTP": "999",
        "SA_ALERT_DEDUP_SECS": "300",
        "SA_ALERT_STORM_THRESHOLD": "0",   # 任何非严重告警都算风暴
    })
    try:
        ready = srv.wait_ready()
        rec("C0 服务就绪", ready)
        if not ready:
            note(srv.tail_log())
            return
        c = Client(srv.base)
        _, admin = c.register("alert_storm_admin")
        _, member = c.register("alert_storm_member")

        codes = make_429(c, admin, quota=1)
        c403 = make_403(c, member)
        rec("C0b 同时造出非严重（429）与严重（403）触发源",
            codes == [202, 429, 429] and c403 == [403, 403], f"{codes} / {c403}")

        st, b = c.alerts(admin)
        r = by_rule(b)
        ev = evaluated_of(b)
        rec("C1 风暴时只发严重告警（active 只剩 authz_denied_spike）",
            list(r) == ["authz_denied_spike"], f"active={sorted(r)}")
        rec("C1b 非严重告警被折叠计数（aggregated≥1 / fired=1）",
            (ev.get("aggregated") or 0) >= 1 and ev.get("fired") == 1,
            f"aggregated={ev.get('aggregated')} fired={ev.get('fired')}")
        stt = stats_of(b)
        rec("C1c 聚合总数体现在 stats（aggregated_total≥1）",
            (stt.get("aggregated_total") or 0) >= 1, f"值={stt.get('aggregated_total')}")

        mt = c.metrics_text()
        agg = metric_sum(mt, "sa_alerts_aggregated_total")
        rec("C2 sa_alerts_aggregated_total 落点（≥1）", agg >= 1, f"值={agg}")

        # 折叠不能变成"永久静音"：窗口过后非严重告警要能自己回来
        # （实现里折叠项也刷新 last_fired，正是为了避免风暴期间反复抖动）
        st, b2 = c.alerts(admin)
        ev2 = evaluated_of(b2)
        rec("C2b 风暴下再抓不重复发（fired=0，只有严重项持续在线）",
            ev2.get("fired") == 0 and list(by_rule(b2)) == ["authz_denied_spike"],
            f"fired={ev2.get('fired')} active={sorted(by_rule(b2))}")
    finally:
        srv.stop()


# --------------------------------------------------------------------------- #
# 负向演练：把去重关掉，A8/B2 的判据必须转红
# --------------------------------------------------------------------------- #
def drill_no_dedup(exe: pathlib.Path) -> None:
    print("\n== 负向演练：SA_ALERT_DEDUP_SECS=0（去重关闭）==", flush=True)
    srv = Server(exe, "D-drill-no-dedup", {
        "SA_TASK_SUBMIT_QUOTA_PER_HOUR": "1",
        "SA_ALERT_RATE_LIMITED_HITS": "1",
        "SA_ALERT_AUTHZ_DENIED_HITS": "999",
        "SA_ALERT_MIN_TASKS": "999",
        "SA_ALERT_MIN_HTTP": "999",
        "SA_ALERT_DEDUP_SECS": "0",
        "SA_ALERT_STORM_THRESHOLD": "5",
    })
    try:
        if not srv.wait_ready():
            rec("D0 服务就绪", False, srv.tail_log(600))
            return
        c = Client(srv.base)
        _, tok = c.register("alert_drill")
        make_429(c, tok, quota=1)
        fired = []
        for _ in range(3):
            _, b = c.alerts(tok)
            fired.append(evaluated_of(b).get("fired"))
        # 关闭去重后：三次抓取都应发出告警（1,1,1），与 A8 期望的 (1,0,0) 正相反
        print(f"       drill fired 序列 = {fired}（期望 1,1,1；若出现 0 说明去重没被真正关掉）", flush=True)
        rec("D1 去重关闭后每次都发（判据有牙齿：A8 的 fired=0,0 不是恒真）",
            fired == [1, 1, 1], str(fired))
    finally:
        srv.stop()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default=str(DEFAULT_EXE))
    ap.add_argument("--drill-no-dedup", action="store_true", help="负向演练：关掉去重，期望判据转红")
    ap.add_argument("--fast", action="store_true", help="跳过 B 阶段的 1.4s 等待（不建议）")
    ap.add_argument("--keep-logs", action="store_true", help="兼容参数；日志本来就逐阶段保留")
    args = ap.parse_args()

    exe = pathlib.Path(args.exe)
    if not exe.exists():
        print(f"[FAIL] 找不到可执行文件: {exe}（用 --exe 指定）")
        return 1
    note(f"二进制 {exe}（{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(exe.stat().st_mtime))}）")

    if args.drill_no_dedup:
        drill_no_dedup(exe)
    else:
        stage_a(exe)
        stage_b(exe, fast=args.fast)
        stage_c(exe)

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n合计 {total} 项断言：通过 {passed}，失败 {total - passed}")
    if total - passed:
        for name, ok, detail in results:
            if not ok:
                print(f"  失败项：{name} -> {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
