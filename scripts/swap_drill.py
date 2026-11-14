#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
swap_drill.py —— 「换一个部件不殃及全部」的行为级演练。

和前两份脚本的分工：
  verify_runner.py  只证明"执行后端能换";
  smoke.py          证明"功能对不对";
  本脚本            证明"换完之后，**上层看到的东西没变**"。

三段演练：
  D1 存储部件互换 (json ↔ memory)：同一套 18 个请求，状态码序列必须完全一致，
     响应体结构签名（字段集合而非值）必须完全一致 —— 说明业务层对后端无感。
  D2 执行部件互换 (sim ↔ process)：同一任务，状态、步骤名序列、权限档位、
     trace 事件类型序列必须一致，只有"输出内容"允许不同 —— 说明治理链路与后端解耦。
  D3 静态守护：调用 check-modularity.py，确认业务层没偷偷依赖具体后端。
  D4 审计部件互换 (memory ↔ file)：同一套请求状态码/结构签名一致、台账口径一致，
     且 file 后端确实落盘、overview 能报出当前装的是哪个部件、坏部件必须拒启而不是静默降级。

用法：
  python scripts/swap_drill.py [--exe <path>]
退出码：0 = 全部通过。
"""

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import verify_runner as vr  # noqa: E402  复用 call / wait_ready / write_scripts / start / stop

RESULTS = []
LAST_TOKEN = None
PORT = 18190


def rec(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("[%s] %s%s" % ("OK  " if ok else "FAIL", name, ("  -> " + str(detail)) if detail else ""))


def shape(v):
    """只留结构，不留值：字段集合 / 类型，值一律折叠。"""
    if isinstance(v, dict):
        return "{" + ",".join("%s:%s" % (k, shape(v[k])) for k in sorted(v)) + "}"
    if isinstance(v, list):
        return "[" + (shape(v[0]) if v else "") + "]"
    if v is None:
        return "null"
    return type(v).__name__


def probe_sequence(base):
    """18 个请求，覆盖每个业务模块；返回 [(label, status, shape)]。"""
    out = []
    stamp = uuid.uuid4().hex[:8]

    global LAST_TOKEN

    def do(label, method, path, body=None, token=None):
        code, resp = vr.call(base, method, path, body, token=token)
        out.append((label, code, shape(resp)))
        return code, resp

    do("healthz", "GET", "/healthz")
    do("readyz", "GET", "/readyz")
    code, reg = do("register", "POST", "/api/v1/auth/register",
                   {"email": "swap%s@example.com" % stamp, "username": "swap%s" % stamp,
                    "password": "Str0ng-Passw0rd!"})
    token = None
    if isinstance(reg, dict):
        token = reg.get("access_token") or (reg.get("tokens") or {}).get("access_token")
    if not token:
        return out, None, "注册失败，无法继续"
    LAST_TOKEN = token
    do("me", "GET", "/api/v1/auth/me", token=token)
    do("onboarding_overview", "GET", "/api/v1/onboarding", token=token)
    do("onboarding_skip", "POST", "/api/v1/onboarding/skip", {}, token=token)
    do("memory_upsert", "POST", "/api/v1/memories",
       {"scope": "private", "key": "swap.key", "content": "演练用记忆", "tags": ["drill"]}, token=token)
    do("memory_list", "GET", "/api/v1/memories?limit=5", token=token)
    do("agents", "GET", "/api/v1/agents", token=token)
    do("capabilities", "GET", "/api/v1/capabilities", token=token)
    code, task = do("task_create", "POST", "/api/v1/tasks",
                    {"mode": "sync", "goal": "演练：部件互换后上层无感", "task_type": "coding", "budget_tokens": 30000},
                    token=token)
    tid = task.get("id") if isinstance(task, dict) else None
    do("task_list", "GET", "/api/v1/tasks?limit=5", token=token)
    if tid:
        code, detail = do("task_detail", "GET", "/api/v1/tasks/" + tid, token=token)
        do("task_trace", "GET", "/api/v1/tasks/%s/trace" % tid, token=token)
    do("overview", "GET", "/api/v1/observability/overview", token=token)
    do("audit", "GET", "/api/v1/observability/audit?limit=5", token=token)
    do("task_404", "GET", "/api/v1/tasks/00000000-0000-0000-0000-000000000000", token=token)
    do("route_404", "GET", "/definitely-not-a-route")
    return out, task, None


def run_profile(exe, storage, runner, runner_dir, port):
    root = pathlib.Path(tempfile.mkdtemp(prefix="sa-swap-%s-" % storage))
    proc = logf = None
    try:
        proc, base, logf = vr.start(exe, port, root / "data", runner, runner_dir)
        # verify_runner.start 不设 SA_STORAGE，这里补一次环境（单独起进程，避免污染）
        if storage != "json":
            vr.stop(proc, logf)
            env = dict(os.environ)
            env.update({"SA_DATA_DIR": str(root / "data"), "SA_BIND": "127.0.0.1:%d" % port,
                        "SA_METRICS_TOKEN": vr.METRICS_TOKEN, "SA_RUNNER": runner,
                        "SA_SNAPSHOT_INTERVAL_SECS": "1", "SA_STORAGE": storage})
            if runner_dir:
                env["SA_RUNNER_DIR"] = str(runner_dir)
            logf = open(str(root / "data") + ".log", "a", encoding="utf-8", errors="replace")
            proc = subprocess.Popen([exe], env=env, stdout=logf, stderr=subprocess.STDOUT)
        ok, _ = vr.wait_ready(base)
        if not ok:
            return None, None, "/readyz 未就绪"
        seq, task, err = probe_sequence(base)
        return seq, task, err
    finally:
        vr.stop(proc, logf)


def start_custom(exe, port, data_dir, extra_env):
    """按自定义环境起一个独立进程（不依赖 verify_runner.start 的固定开关）。"""
    data_dir = pathlib.Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "SA_DATA_DIR": str(data_dir),
        "SA_BIND": "127.0.0.1:%d" % port,
        "SA_METRICS_TOKEN": vr.METRICS_TOKEN,
        "SA_RUNNER": "sim",
        "SA_SNAPSHOT_INTERVAL_SECS": "1",
    })
    env.update(extra_env)
    logf = open(str(data_dir) + ".log", "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen([exe], env=env, stdout=logf, stderr=subprocess.STDOUT)
    return proc, logf, "http://127.0.0.1:%d" % port


def run_audit_profile(exe, port, data_dir, extra_env):
    """跑一轮完整序列，额外返回 overview 里的 audit_backend 值。"""
    proc = logf = None
    try:
        proc, logf, base = start_custom(exe, port, data_dir, extra_env)
        ok, _ = vr.wait_ready(base)
        if not ok:
            return None, None, "/readyz 未就绪（%s）" % extra_env, None
        seq, task, err = probe_sequence(base)
        backend = None
        if LAST_TOKEN:
            code, ov = vr.call(base, "GET", "/api/v1/observability/overview", token=LAST_TOKEN)
            if isinstance(ov, dict) and code == 200:
                backend = ov.get("audit_backend")
        return seq, task, err, backend
    finally:
        vr.stop(proc, logf)


def audit_total_probe(exe, port, data_dir, extra_env, token=None, settle=1.2):
    """起一轮进程，返回 (审计总条数, 可用 token)。token 可跨进程复用（JWT 密钥持久化在数据目录）。

    settle：注册后等待多久再停进程，用于验证「写后落盘窗口」是否真的按配置生效。
    """
    proc = logf = None
    try:
        proc, logf, base = start_custom(exe, port, data_dir, extra_env)
        ok, _ = vr.wait_ready(base)
        if not ok:
            return None, token
        if token is None:
            stamp = uuid.uuid4().hex[:8]
            code, reg = vr.call(base, "POST", "/api/v1/auth/register",
                                {"email": "audit%s@example.com" % stamp,
                                 "username": "audit%s" % stamp,
                                 "password": "Str0ng-Passw0rd!"})
            if isinstance(reg, dict):
                token = reg.get("access_token") or (reg.get("tokens") or {}).get("access_token")
        if not token:
            return None, None
        if settle:
            time.sleep(settle)
        code, body = vr.call(base, "GET", "/api/v1/observability/audit?limit=1", token=token)
        total = body.get("total") if isinstance(body, dict) and code == 200 else None
        return total, token
    finally:
        vr.stop(proc, logf)


def start_expect_fail(exe, port, data_dir, extra_env):
    """期望启动失败：进程在 6 秒内退出且非 0，即视为正确拒启。"""
    proc = logf = None
    try:
        proc, logf, base = start_custom(exe, port, data_dir, extra_env)
        ok, _ = vr.wait_ready(base, timeout=8)
        if ok:
            return False, "/readyz 仍返回 200 —— 坏配置被静默接受了"
        for _ in range(20):
            if proc.poll() is not None:
                break
            import time
            time.sleep(0.2)
        code = proc.poll()
        if code is None:
            return False, "进程仍在运行，未拒启"
        return True, "退出码=%s（预期非 0）" % code
    finally:
        vr.stop(proc, logf)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default="")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()

    exe = args.exe or str(pathlib.Path(os.environ.get("LOCALAPPDATA", ".")) /
                          "sa-build" / "target" / "debug" / "super-agents.exe")
    if not pathlib.Path(exe).exists():
        print("找不到可执行文件: %s" % exe)
        return 2

    root = pathlib.Path(tempfile.mkdtemp(prefix="sa-swap-scripts-"))
    runner_dir = root / "scripts"
    vr.write_scripts(runner_dir, "SWAP-" + uuid.uuid4().hex[:8].upper(), vr.AGENTS)

    # ---------------- D1 存储互换 ----------------
    print("\n[D1] 存储部件互换：json ↔ memory，上层是否无感")
    json_seq, json_task, err1 = run_profile(exe, "json", "sim", None, args.port)
    mem_seq, mem_task, err2 = run_profile(exe, "memory", "sim", None, args.port + 1)
    rec("D1a 两种存储后端都能起服务并跑完整序列", bool(json_seq) and bool(mem_seq),
        "%s / %s" % (err1 or "json ok", err2 or "memory ok"))
    if json_seq and mem_seq:
        same_len = len(json_seq) == len(mem_seq)
        rec("D1b 请求条数一致", same_len, "%d vs %d" % (len(json_seq), len(mem_seq)))
        code_diff = [(a[0], a[1], b[1]) for a, b in zip(json_seq, mem_seq) if a[1] != b[1]]
        rec("D1c 状态码序列完全一致（接口行为不随存储改变）", not code_diff, code_diff[:4] or "18/18 一致")
        shape_diff = [(a[0]) for a, b in zip(json_seq, mem_seq) if a[2] != b[2]]
        rec("D1d 响应结构签名完全一致（字段集合不随存储改变）", not shape_diff,
            shape_diff[:4] or "18/18 一致")

    # ---------------- D2 执行互换 ----------------
    print("\n[D2] 执行部件互换：sim ↔ process，治理链路是否与后端解耦")
    sim_seq, sim_task, _ = run_profile(exe, "memory", "sim", None, args.port + 2)
    proc_seq, proc_task, err3 = run_profile(exe, "memory", "process", runner_dir, args.port + 3)

    def sig(task):
        if not isinstance(task, dict):
            return None
        return {
            "status": task.get("status"),
            "steps": [s.get("name") for s in (task.get("plan") or [])],
            "perms": [s.get("permission") for s in (task.get("plan") or [])],
            "result_keys": sorted((task.get("results") or [{}])[0].keys()) if task.get("results") else [],
            "trace_kinds": [t.get("kind") or t.get("event") for t in (task.get("trace") or [])],
            "tokens_nonzero": (task.get("tokens_used") or 0) > 0,
        }

    s_sig, p_sig = sig(sim_task), sig(proc_task)
    rec("D2a 两种执行后端都能跑完任务", bool(s_sig) and bool(p_sig), err3 or "ok")
    if s_sig and p_sig:
        rec("D2b 任务终态一致", s_sig["status"] == p_sig["status"],
            "%s vs %s" % (s_sig["status"], p_sig["status"]))
        rec("D2c 计划步骤名与顺序一致", s_sig["steps"] == p_sig["steps"],
            "%s vs %s" % (s_sig["steps"], p_sig["steps"]))
        rec("D2d 每步权限档位一致（治理判定不依赖后端）", s_sig["perms"] == p_sig["perms"],
            "%s vs %s" % (s_sig["perms"], p_sig["perms"]))
        rec("D2e trace 事件类型序列一致（审计口径不依赖后端）",
            s_sig["trace_kinds"] == p_sig["trace_kinds"],
            "%d vs %d 条" % (len(s_sig["trace_kinds"] or []), len(p_sig["trace_kinds"] or [])))
        rec("D2f 两种后端都记了 token 用量（预算不会被换件绕过）",
            s_sig["tokens_nonzero"] and p_sig["tokens_nonzero"],
            "sim=%s process=%s" % (s_sig["tokens_nonzero"], p_sig["tokens_nonzero"]))
        rec("D2g 步骤结果字段集合一致（前端不必按后端分支）",
            s_sig["result_keys"] == p_sig["result_keys"],
            "%s vs %s" % (s_sig["result_keys"], p_sig["result_keys"]))

    # ---------------- D4 审计互换 ----------------
    print("\n[D4] 审计部件互换：memory ↔ file，安全台账是否与后端解耦")
    mem_root = pathlib.Path(tempfile.mkdtemp(prefix="sa-audit-mem-"))
    file_root = pathlib.Path(tempfile.mkdtemp(prefix="sa-audit-file-"))
    audit_file = file_root / "audit.jsonl"

    mem_seq, mem_task2, err_m, mem_bk = run_audit_profile(
        exe, args.port + 4, mem_root / "data", {"SA_AUDIT": "memory"})
    fil_seq, fil_task2, err_f, fil_bk = run_audit_profile(
        exe, args.port + 5, file_root / "data",
        {"SA_AUDIT": "file", "SA_AUDIT_FILE": str(audit_file)})

    rec("D4a 两种审计后端都能起服务并跑完整序列", bool(mem_seq) and bool(fil_seq),
        "%s / %s" % (err_m or "memory ok", err_f or "file ok"))
    if mem_seq and fil_seq:
        rec("D4b 请求条数一致", len(mem_seq) == len(fil_seq), "%d vs %d" % (len(mem_seq), len(fil_seq)))
        code_diff = [(a[0], a[1], b[1]) for a, b in zip(mem_seq, fil_seq) if a[1] != b[1]]
        rec("D4c 状态码序列完全一致（换审计后端不改接口行为）", not code_diff, code_diff[:4] or "18/18 一致")
        shape_diff = [(a[0]) for a, b in zip(mem_seq, fil_seq) if a[2] != b[2]]
        rec("D4d 响应结构签名完全一致（字段集合不随审计后端改变）", not shape_diff,
            shape_diff[:4] or "18/18 一致")
        rec("D4e overview 报出当前审计部件身份（运维可核对）",
            mem_bk == "memory" and fil_bk == "file",
            "memory 模式=%r / file 模式=%r" % (mem_bk, fil_bk))

    # 落盘证据：file 后端必须真的写出可解析的 JSONL，否则"落盘"只是名字
    lines, bad = 0, 0
    if audit_file.exists():
        for ln in audit_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if not ln.strip():
                continue
            lines += 1
            try:
                json.loads(ln)
            except Exception:
                bad += 1
    rec("D4f file 审计后端确实落盘且每行都是合法 JSON",
        audit_file.exists() and lines > 0 and bad == 0,
        "文件=%s 行数=%d 非法行=%d" % (audit_file.exists(), lines, bad))

    # 重启后历史台账必须还在 —— 否则 file 后端只是"改了名字的内存"
    restart_root = pathlib.Path(tempfile.mkdtemp(prefix="sa-audit-restart-"))
    rfile = restart_root / "audit.jsonl"
    renv = {"SA_AUDIT": "file", "SA_AUDIT_FILE": str(rfile)}
    t1, tok1 = audit_total_probe(exe, args.port + 8, restart_root / "data", renv)
    t2, tok2 = audit_total_probe(exe, args.port + 9, restart_root / "data", renv, token=tok1)
    rec("D4i 硬杀后重启，历史台账仍可查询（file 后端真可追溯）",
        t1 is not None and t2 is not None and t1 > 0 and t2 >= t1 and tok2 == tok1,
        "重启前=%s 条 / 重启后=%s 条 / 同一 token 仍有效=%s" % (t1, t2, tok2 == tok1))

    # 写后落盘窗口必须真的按配置生效：窗口调到 50ms 时，0.25s 后硬杀也不该丢账号
    fast_root = pathlib.Path(tempfile.mkdtemp(prefix="sa-flush-fast-"))
    fast_env = dict(renv)          # 观测手段必须是 file 审计，否则重启后读不到任何台账
    fast_env["SA_FLUSH_INTERVAL_MS"] = "50"
    t3, tok3 = audit_total_probe(exe, args.port + 10, fast_root / "data", fast_env, settle=0.25)
    t4, tok4 = audit_total_probe(exe, args.port + 11, fast_root / "data", fast_env, token=tok3, settle=0.0)
    rec("D4j SA_FLUSH_INTERVAL_MS 生效（写入 0.25s 后硬杀不丢账号）",
        t3 is not None and t4 is not None and tok4 == tok3 and t4 >= t3 > 0,
        "重启前=%s 条 / 重启后=%s 条" % (t3, t4))

    # 坏部件/坏配置必须拒启，不能静默降级 —— 降级会让"审计缺口"变成隐形风险
    ok_bogus, why_bogus = start_expect_fail(exe, args.port + 6, mem_root / "bogus", {"SA_AUDIT": "bogus"})
    rec("D4g SA_AUDIT=非法值 时启动失败（不静默降级到内存）", ok_bogus, why_bogus)
    ok_dir, why_dir = start_expect_fail(
        exe, args.port + 7, mem_root / "badpath",
        {"SA_AUDIT": "file", "SA_AUDIT_FILE": str(mem_root)})
    rec("D4h SA_AUDIT_FILE 指向目录 时启动失败（明确报错，不假装成功）", ok_dir, why_dir)

    # ---------------- D3 静态守护 ----------------
    print("\n[D3] 静态守护：业务层有没有偷偷依赖具体后端")
    r = subprocess.run([sys.executable, "scripts/check-modularity.py"], cwd=str(pathlib.Path(__file__).resolve().parents[1]),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    tail = [l for l in (r.stdout or "").splitlines() if "合计" in l]
    rec("D3 模块化静态守护全通过", r.returncode == 0, tail[-1] if tail else (r.stderr or "")[:120])

    failed = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 70)
    print("换件演练：%d 项 | 通过 %d | 失败 %d" % (len(RESULTS), len(RESULTS) - len(failed), len(failed)))
    if failed:
        for n, _, d in failed:
            print("  - %s  %s" % (n, d))
    print("=" * 70)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
