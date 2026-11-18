#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
部件互换实测（swap check）：换一个槽位、**不动一行业务代码**，整条链路必须照旧跑通。

为什么单独有这个脚本
--------------------
`smoke.py` 验的是"一个默认装配的实例功能对不对"；
本脚本验的是**模块化改造是否真的成立**——同一份二进制，只改环境变量：

| 槽位 | 环境变量 | 可选实现 |
|------|----------|----------|
| 存储 | SA_STORAGE | json / memory |
| 执行 | SA_RUNNER | sim / process |
| 审计 | SA_AUDIT | memory / file |
| 限流 | SA_LIMITER | memory / noop |
| 指标导出 | SA_METRICS_EXPORT | prometheus / json / noop |

每个组合都跑**同一条业务链路**（注册 → 登录 → 提交任务 → 查详情 → 看概览 → 看部件清单 → 抓指标），
任何一步因为"换了部件"而断掉，脚本立刻 FAIL —— 这就是"换部件不殃及整体"的可执行证明。

另外两组专门验"实话"：
- **持久化真伪**：json 装配重启后数据必须还在；memory 装配重启后数据必须没（且脚本如实打印"符合预期"）。
- **非法值拒启**：`SA_LIMITER=bogus` 必须在启动阶段就退出，并把可选值打在错误里，
  而不是带着"以为有限流其实没限"的状态跑起来。

用法：
    python scripts/swap_check.py                      # 自动找 exe，跑全部用例
    python scripts/swap_check.py --exe <path>         # 指定 exe
    python scripts/swap_check.py --report out.md      # 额外写一份 markdown 报告
    python scripts/swap_check.py --only C2            # 只跑某个用例（调试用）

退出码：0 = 全部通过；1 = 有失败项。
"""

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

OK, FAIL = "PASS", "FAIL"
results = []       # [(case, name, ok, detail)]
METRICS_TOKEN = "swapcheck-metrics-token-0123456789"
PASSWORD = "Sw4p!CheckPass"
STATUSES = {"queued", "running", "succeeded", "failed",
            "partially_succeeded", "aborted_budget", "cancelled"}


def rec(case, name, ok, detail=""):
    results.append((case, name, OK if ok else FAIL, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {case} / {name}" + (f"  ->  {detail}" if detail else ""),
          flush=True)


def expect(case, name, actual, allowed, detail=""):
    """actual 落在 allowed 内即通过，同时**必须**打印实际值（禁止含糊）。"""
    allowed = allowed if isinstance(allowed, (list, tuple, set)) else [allowed]
    shown = f"实际={actual}" + (f" 期望={list(allowed)}" if len(allowed) > 1 else "")
    if detail:
        shown += f" | {detail}"
    rec(case, name, actual in allowed, shown)


# ---------------------------------------------------------------------------
# HTTP 小客户端（只用标准库）
# ---------------------------------------------------------------------------
class Client:
    def __init__(self, base):
        self.base = base.rstrip("/")

    def __call__(self, method, path, body=None, token=None):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read().decode("utf-8", "replace")
                ctype = r.headers.get("Content-Type", "")
                try:
                    return r.status, raw, json.loads(raw) if raw else None, ctype
                except json.JSONDecodeError:
                    return r.status, raw, None, ctype
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                parsed = None
            return e.code, raw, parsed, e.headers.get("Content-Type", "")
        except Exception as e:                                   # 连不上 = 明确失败，不猜
            return -1, f"{type(e).__name__}: {e}", None, ""


# ---------------------------------------------------------------------------
# 起停实例
# ---------------------------------------------------------------------------
class Instance:
    def __init__(self, exe, envs, data_dir, port, tag):
        self.exe, self.envs, self.data_dir, self.port, self.tag = exe, envs, data_dir, port, tag
        self.proc, self.log = None, None

    def start(self, timeout=20.0):
        self.log = self.data_dir.parent / f"server-{self.tag}.log"
        env = dict(os.environ)
        env.pop("SA_STORAGE", None)
        env.pop("SA_RUNNER", None)
        env.pop("SA_AUDIT", None)
        env.pop("SA_LIMITER", None)
        env.pop("SA_METRICS_EXPORT", None)
        env.update({
            "SA_DATA_DIR": str(self.data_dir),
            "SA_BIND": f"127.0.0.1:{self.port}",
            "SA_METRICS_TOKEN": METRICS_TOKEN,   # config 强制 >= 24 字符
            "SA_JWT_SECRET": "swap-check-fixed-secret-0123456789abcdef",
            "RUST_LOG": "warn",
        })
        env.update(self.envs)
        self.proc = subprocess.Popen([self.exe], env=env, stdout=self.log.open("w", encoding="utf-8"),
                                     stderr=subprocess.STDOUT, cwd=str(self.data_dir.parent))
        base = f"http://127.0.0.1:{self.port}"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                return False, base          # 提前退出：由调用方判定"是否预期"
            if Client(base)("GET", "/healthz")[0] == 200:
                return True, base
            time.sleep(0.15)
        return False, base

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def exit_log(self):
        try:
            return self.log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


# ---------------------------------------------------------------------------
# 期望值：由"给进去的环境变量"推出，不看跑出来的结果
# ---------------------------------------------------------------------------
def expected(envs):
    return {
        "storage": envs.get("SA_STORAGE") or "json",
        "runner": envs.get("SA_RUNNER") or "sim",
        "audit": envs.get("SA_AUDIT") or "memory",
        "limiter": envs.get("SA_LIMITER") or "memory",
        "metrics": envs.get("SA_METRICS_EXPORT") or "prometheus",
    }


def metrics_ctype_prefix(kind):
    return {"prometheus": "text/plain", "json": "application/json", "noop": "text/plain"}[kind]


# ---------------------------------------------------------------------------
# 单个组合的完整体检
# ---------------------------------------------------------------------------
def run_case(ctx, case_name, envs):
    exp = expected(envs)
    tag = f"{case_name.split()[0].replace('（', '').replace('）', '')}"
    data_dir = ctx["work"] / f"data-{tag}"
    data_dir.mkdir(parents=True, exist_ok=True)
    inst = Instance(ctx["exe"], envs, data_dir, ctx["port"] + ctx["seq"], tag)
    ctx["seq"] += 1
    started, base = inst.start()
    try:
        expect(case_name, "进程启动并 /healthz 200", started, [True],
               f"env={envs or '默认装配'}")

        s, _, b, _ = Client(base)("GET", "/readyz")
        expect(case_name, "/readyz 200", s, [200])
        expect(case_name, "readyz 报告的存储后端 = 装配值", (b or {}).get("storage"), [exp["storage"]],
               f"durable={(b or {}).get('storage_durable')}")

        uid = uuid.uuid4().hex[:8]
        c = Client(base)
        s, _, b, _ = c("POST", "/api/v1/auth/register",
                       {"username": f"swap_{uid}", "email": f"swap_{uid}@example.com",
                        "password": PASSWORD, "display_name": "Swap Owner"})
        expect(case_name, "注册 201", s, [201], json.dumps(b, ensure_ascii=False)[:120] if s != 201 else "")
        s, _, b, _ = c("POST", "/api/v1/auth/login", {"account": f"swap_{uid}", "password": PASSWORD})
        expect(case_name, "登录 200", s, [200])
        tok = ((b or {}).get("tokens") or {}).get("access_token")
        rec(case_name, "拿到 access_token", bool(tok), f"长度={len(tok or '')}")

        # --- 业务链路：提交任务必须真的被执行 ---
        s, _, b, _ = c("POST", "/api/v1/tasks",
                       {"mode": "sync", "goal": "部件互换后业务链路是否照旧跑通", "task_type": "coding",
                        "model_profile": "fast", "budget_tokens": 20000}, token=tok)
        expect(case_name, "提交任务 201", s, [201], json.dumps(b, ensure_ascii=False)[:120] if s != 201 else "")
        task = b or {}
        tid = task.get("id")
        rec(case_name, "任务状态落在合法枚举内", task.get("status") in STATUSES, f"实际={task.get('status')}")
        rec(case_name, "任务带非空计划 plan", len(task.get("plan") or []) > 0,
            f"步骤数={len(task.get('plan') or [])}")
        s, _, b, _ = c("GET", f"/api/v1/tasks/{tid}", token=tok)
        expect(case_name, "任务详情可回读 200", s, [200])
        rec(case_name, "回读到的就是刚提交那条", (b or {}).get("id") == tid, f"id={tid}")

        # --- 概览：五个槽位的"当前实现"必须与装配值一致 ---
        s, _, b, _ = c("GET", "/api/v1/observability/overview", token=tok)
        expect(case_name, "overview 200", s, [200])
        ov = b or {}
        for key, env_key in (("storage", "storage"), ("runner_backend", "runner"),
                             ("audit_backend", "audit"), ("limiter_backend", "limiter"),
                             ("metrics_export", "metrics")):
            expect(case_name, f"overview.{key} = {exp[env_key]}", ov.get(key), [exp[env_key]])
        rec(case_name, "overview.limiter_enforcing 与限流实现自洽",
            ov.get("limiter_enforcing") is (exp["limiter"] != "noop"),
            f"limiter={exp['limiter']} enforcing={ov.get('limiter_enforcing')}")
        rec(case_name, "overview.store.tasks >= 1（数据真的进库了）",
            (ov.get("store") or {}).get("tasks", 0) >= 1, f"stats={ov.get('store')}")

        # --- 部件清单：注册表说的一定是运行中的实话 ---
        s, _, b, _ = c("GET", "/api/v1/observability/components", token=tok)
        expect(case_name, "components 200", s, [200])
        items = (b or {}).get("items") or []
        slots = {i.get("slot"): i for i in items}
        rec(case_name, "部件清单覆盖 5 个槽位",
            set(slots) == {"storage", "runner", "audit", "limiter", "metrics"}, f"实际={sorted(slots)}")
        for slot, exp_key in (("storage", "storage"), ("runner", "runner"), ("audit", "audit"),
                              ("limiter", "limiter"), ("metrics", "metrics")):
            it = slots.get(slot) or {}
            rec(case_name, f"components[{slot}].active = {exp[exp_key]}",
                it.get("active") == exp[exp_key],
                f"active={it.get('active')} health={it.get('health')} available={it.get('available')}")
            rec(case_name, f"components[{slot}] 的 active 在 available 清单内",
                it.get("active") in (it.get("available") or []), "")
        rec(case_name, "组件清单如实标注「换件需重启」",
            (b or {}).get("restart_required_for_change") is True, "")

        # --- 指标端点：Content-Type 由导出器自报，handler 不认识具体实现 ---
        req = urllib.request.Request(base + "/metrics")
        req.add_header("Authorization", f"Bearer {METRICS_TOKEN}")
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read().decode("utf-8", "replace")
            ctype = r.headers.get("Content-Type", "")
            code = r.status
        expect(case_name, "/metrics 200", code, [200])
        rec(case_name, f"Content-Type 随导出器切换（期望 {metrics_ctype_prefix(exp['metrics'])}）",
            ctype.startswith(metrics_ctype_prefix(exp["metrics"])), f"实际={ctype}")
        if exp["metrics"] == "noop":
            rec(case_name, "noop 导出器如实说明「指标已关闭」", "已关闭" in body, f"正文={body.strip()[:60]}")
        else:
            rec(case_name, "指标正文记录了 HTTP 请求计数",
                "sa_http_requests_total" in body, f"正文 {len(body)} 字节")

        return {"case": case_name, "envs": envs, "exp": exp, "task_id": tid,
                "log": str(inst.log), "ok": all(r[2] == OK for r in results if r[0] == case_name)}
    finally:
        inst.stop()


# ---------------------------------------------------------------------------
# 持久化真伪：重启之后数据还在不在
# ---------------------------------------------------------------------------
def run_persistence(ctx, storage, expect_persist):
    case = f"P{'1' if expect_persist else '2'} 持久化实测（{storage}）"
    envs = {"SA_STORAGE": storage}
    data_dir = ctx["work"] / f"data-{case.split()[0]}"
    data_dir.mkdir(parents=True, exist_ok=True)
    port = ctx["port"] + ctx["seq"]
    ctx["seq"] += 1
    uid = uuid.uuid4().hex[:8]
    username = f"persist_{uid}"

    inst = Instance(ctx["exe"], envs, data_dir, port, f"persist-{storage}-1")
    ok, base = inst.start()
    tid = None
    try:
        rec(case, "第一次启动成功", ok, f"env={envs}")
        if ok:
            c = Client(base)
            c("POST", "/api/v1/auth/register",
              {"username": username, "email": f"{username}@example.com",
               "password": PASSWORD, "display_name": "Persist"})
            s, _, b, _ = c("POST", "/api/v1/auth/login", {"account": username, "password": PASSWORD})
            tok = ((b or {}).get("tokens") or {}).get("access_token")
            s, _, b, _ = c("POST", "/api/v1/tasks",
                           {"mode": "sync", "goal": "重启后这条任务应该还在（json）或消失（memory）",
                            "task_type": "writing", "budget_tokens": 20000}, token=tok)
            tid = (b or {}).get("id")
            rec(case, "重启前已写入一条任务", s == 201 and bool(tid), f"id={tid}")
            time.sleep(1.2)     # 留给后台 flush（SA_FLUSH_INTERVAL_MS 默认 500ms）
    finally:
        inst.stop()

    inst2 = Instance(ctx["exe"], envs, data_dir, port, f"persist-{storage}-2")
    ok2, base2 = inst2.start()
    try:
        rec(case, "第二次启动成功（同一数据目录）", ok2, "")
        if ok2:
            c = Client(base2)
            s, _, b, _ = c("POST", "/api/v1/auth/login", {"account": username, "password": PASSWORD})
            tok = ((b or {}).get("tokens") or {}).get("access_token")
            s, _, b, _ = c("GET", "/api/v1/tasks?limit=50", token=tok)
            ids = [t.get("id") for t in ((b or {}).get("items") or [])]
            if expect_persist:
                rec(case, "json 装配：重启后任务仍在（真落盘）", tid in ids,
                    f"重启后共 {len(ids)} 条，含 {tid}={tid in ids}")
                rec(case, "json 装配：账号也还在（能登录）", s == 200 and bool(tok), f"状态={s}")
            else:
                rec(case, "memory 装配：重启后任务消失（符合预期，不假装持久化）",
                    tid not in ids, f"重启后共 {len(ids)} 条")
    finally:
        inst2.stop()


# ---------------------------------------------------------------------------
# 非法值：必须在启动阶段就拒启
# ---------------------------------------------------------------------------
def run_reject(ctx, envs, expect_key):
    case = f"R 非法配置拒启（{expect_key}）"
    data_dir = ctx["work"] / "data-R"
    data_dir.mkdir(parents=True, exist_ok=True)
    inst = Instance(ctx["exe"], envs, data_dir, ctx["port"] + ctx["seq"], "reject")
    ctx["seq"] += 1
    started, _ = inst.start(timeout=12)
    log = inst.exit_log()
    inst.stop()
    rec(case, "非法值不会带病启动", not started, f"started={started}")
    rec(case, f"错误信息点名 {expect_key}", expect_key in log, f"日志片段={log.strip().splitlines()[-1][:160] if log.strip() else '空'}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
CASES = [
    ("C1 基线（默认装配）", {}),
    ("C2 存储换纯内存", {"SA_STORAGE": "memory"}),
    ("C3 审计落盘+指标换 JSON", {"SA_AUDIT": "file", "SA_METRICS_EXPORT": "json"}),
    ("C4 限流换成 noop", {"SA_LIMITER": "noop"}),
    ("C5 指标导出关闭", {"SA_METRICS_EXPORT": "noop"}),
    ("C6 全内存极简装配", {"SA_STORAGE": "memory", "SA_AUDIT": "memory",
                          "SA_LIMITER": "noop", "SA_METRICS_EXPORT": "noop"}),
]


def find_exe():
    here = pathlib.Path(__file__).resolve().parent.parent
    for cand in [here / "target/debug/super-agents.exe", here / "target/release/super-agents.exe",
                 here / "target/debug/super-agents"]:
        if cand.exists():
            return str(cand)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", help="可执行文件路径")
    ap.add_argument("--port", type=int, default=18200, help="起始端口（每个用例顺延）")
    ap.add_argument("--report", help="额外输出 markdown 报告到该路径")
    ap.add_argument("--only", help="只跑名字里含该字符串的用例")
    ap.add_argument("--keep", action="store_true", help="保留临时数据目录")
    args = ap.parse_args()

    exe = args.exe or find_exe()
    if not exe or not pathlib.Path(exe).exists():
        print("找不到可执行文件，请用 --exe 指定（先 cargo build）", file=sys.stderr)
        return 2

    work = pathlib.Path(__file__).resolve().parent.parent / "target" / f"swapcheck-{int(time.time())}"
    work.mkdir(parents=True, exist_ok=True)
    ctx = {"exe": exe, "work": work, "port": args.port, "seq": 0}
    print(f"exe = {exe}\n临时目录 = {work}\n")

    cases = [(n, e) for n, e in CASES if not args.only or args.only in n]
    for name, envs in cases:
        run_case(ctx, name, envs)
        print()
    if not args.only or "P" in args.only:
        run_persistence(ctx, "json", expect_persist=True)
        print()
        run_persistence(ctx, "memory", expect_persist=False)
        print()
    if not args.only or "R" in args.only:
        run_reject(ctx, {"SA_LIMITER": "bogus"}, "SA_LIMITER")
        print()

    total = len(results)
    passed = sum(1 for r in results if r[2] == OK)
    failed = [r for r in results if r[2] == FAIL]
    print("=" * 64)
    print(f"合计 {total} 项 | 通过 {passed} | 失败 {len(failed)}")
    for case, name, _, detail in failed:
        print(f"  FAIL  {case} / {name}  {detail}")

    if args.report:
        lines = ["# 部件互换实测报告", "",
                 f"- 可执行文件：`{exe}`",
                 f"- 断言总数：{total}，通过 {passed}，失败 {len(failed)}",
                 f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
        for name, envs in cases:
            rows = [r for r in results if r[0] == name]
            if not rows:
                continue
            lines += [f"## {name}", "", f"环境变量：`{envs or '默认装配'}`", "",
                      "| 断言 | 结果 | 实测值 |", "|------|------|--------|"]
            for _, n, st, d in rows:
                lines.append(f"| {n} | {st} | {d.replace('|', '/')} |")
            lines.append("")
        pathlib.Path(args.report).write_text("\n".join(lines), encoding="utf-8")
        print(f"报告已写入 {args.report}")

    if not args.keep:
        pathguard.safe_rmtree(work, base=pathlib.Path(__file__).resolve().parent.parent / "target",
                              allow_prefix="swapcheck-", what="互换检查目录", warn_only=True)

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
