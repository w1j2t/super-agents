#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""可插拔部件矩阵：逐个换掉一个部件，验证"换一个不殃及全部"。

为什么需要这个脚本：
    模块化不能只写在文档里。老板的要求是"可单独替换部件而不殃及整体"，
    唯一能证明这一点的办法就是：把每个部件真换成另一个实现，起一个真实实例，
    把核心链路（注册 → 登录 → 提交任务 → 记忆读写 → 部件视图 → 指标）跑一遍。

判定口径：
    1. 每个用例都必须让核心链路全绿——换件不能让别的功能坏掉；
    2. `/observability/components` 里 **显式设置的那个槽位** 的 active 必须等于设定值
       （active 取自运行中实例本身，不是读配置猜的，所以这条同时验证了"视图不说谎"）；
    3. 每个槽位的 active 必须落在它自己的 available 清单里（不存在"装了不存在的部件"）；
    4. 默认组额外验证"重启后数据还在"——持久化不是装饰。

用法：
    python scripts/matrix.py --exe target-gnu/debug/super-agents.exe
退出码 0 = 全部通过；1 = 有失败项（逐项打印）。
"""

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import pathguard  # 清理一律过守卫：裸删除会把「参数少写一层」放大成删源码
import time
import urllib.error
import urllib.request
import uuid

OK, FAIL = "PASS", "FAIL"
rows = []


def rec(case, name, ok, detail=""):
    rows.append((case, name, OK if ok else FAIL, detail))
    mark = "OK  " if ok else "FAIL"
    print(f"[{mark}] {case} | {name}" + (f"  ->  {detail}" if detail else ""), flush=True)


class Client:
    def __init__(self, base):
        self.base = base.rstrip("/")

    def __call__(self, method, path, body=None, token=None):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        r = urllib.request.Request(self.base + path, data=data, method=method)
        if body is not None:
            r.add_header("Content-Type", "application/json")
        if token:
            r.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                raw, status = resp.read(), resp.status
        except urllib.error.HTTPError as e:
            raw, status = e.read(), e.code
        except Exception as e:
            return 0, {"transport_error": repr(e)}
        text = raw.decode("utf-8", "replace")
        try:
            return status, (json.loads(text) if text else None)
        except Exception:
            return status, text


def make_runner_scripts(dirpath):
    """给 process 后端造 14 个白名单脚本（命名 = agent_id，扩展名 .py）。

    扩展名必须是 .py：process 后端默认只跑 python 脚本，ps1/bat/cmd 需显式
    设 SA_RUNNER_ALLOW_SHELL=1 才认（安全基线，见三方审查 H1）。

    名单必须与 `src/domain/agent.rs` 的 AGENTS 逐项一致：少一个 id，落到该 agent
    的步骤会因为找不到脚本而失败，报错写的是"脚本不存在"，根因却是名单漂移。
    """
    dirpath.mkdir(parents=True, exist_ok=True)
    agents = ["super-agent", "code-agent", "doc-agent", "research-agent",
              "planner-agent", "ideation-agent", "game-agent",
              "finance-agent", "legal-agent", "data-agent",
              "narrative-agent", "market-agent", "qa-agent", "guard-agent"]
    for a in agents:
        (dirpath / f"{a}.py").write_text(
            f'print("{a} 真实脚本执行完毕")\n', encoding="utf-8")
    return dirpath


def start(exe, port, data_dir, extra_env):
    env = dict(os.environ)
    env["SA_DATA_DIR"] = str(data_dir)
    env["SA_BIND"] = f"127.0.0.1:{port}"
    env["SA_METRICS_TOKEN"] = "matrix-metrics-token-0123456789abcdef"
    # 默认 sim：本矩阵测的是"部件可插拔后治理口径是否一致"，不是真实执行；
    # 真实执行由用例 G 显式 SA_RUNNER=process 覆盖（默认后端改动后必须钉住口径）。
    env["SA_RUNNER"] = "sim"
    env.update(extra_env)
    log = open(data_dir.with_suffix(f".{port}.log"), "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen([exe], env=env, stdout=log, stderr=subprocess.STDOUT)
    return proc


def wait_ready(c, seconds=60):
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            if c("GET", "/readyz")[0] == 200:
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def stop(proc):
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()


def core_path(case, base, expected_slots, stamp):
    """核心链路 + 部件视图校验。返回 (ok_all, owner_token, task_id)。"""
    c = Client(base)
    if not wait_ready(c):
        rec(case, "实例就绪", False, "/readyz 未在 60 秒内就绪")
        return False, None, None
    rec(case, "实例就绪", True)

    uname = f"mx_{stamp}"
    pw = "MatrixPass!2026"
    s, b = c("POST", "/api/v1/auth/register",
             {"username": uname, "email": f"{uname}@example.com", "password": pw})
    ok_reg = s == 201
    rec(case, "注册（首个用户 = owner）", ok_reg, f"实际={s}")
    tok = ((b or {}).get("tokens") or {}).get("access_token") if ok_reg else None

    s, b = c("POST", "/api/v1/auth/login", {"account": uname, "password": pw})
    rec(case, "登录", s == 200 and bool(((b or {}).get("tokens") or {}).get("access_token")),
        f"实际={s}")
    tok = ((b or {}).get("tokens") or {}).get("access_token") or tok

    s, b = c("GET", "/api/v1/observability/components", token=tok)
    items = (b or {}).get("items") or []
    rec(case, "部件视图 200 且 5 个槽位齐全", s == 200 and len(items) == 5,
        f"实际={s} 槽位={len(items)}")
    by_slot = {it.get("slot"): it for it in items}
    bad = [it["slot"] for it in items if it.get("active") not in (it.get("available") or [])]
    rec(case, "每个槽位 active 都在自己的 available 清单内", not bad, f"异常槽位={bad}")
    mismatch = []
    for slot, want in expected_slots.items():
        got = (by_slot.get(slot) or {}).get("active")
        if got != want:
            mismatch.append(f"{slot}: 期望{want} 实际{got}")
    rec(case, "显式设置的部件真的生效（视图取自实例）", not mismatch,
        "；".join(mismatch) if mismatch else f"{expected_slots}")

    s, b = c("POST", "/api/v1/tasks", {"mode": "sync", "goal": f"换件矩阵任务 {stamp}", "task_type": "coding",
                                       "model_profile": "fast", "budget_tokens": 20000}, token=tok)
    status = (b or {}).get("status")
    rec(case, "提交任务跑通且未被判失败", s == 201 and status in
        ("succeeded", "partially_succeeded"), f"实际={s} status={status}")
    tid = (b or {}).get("id")

    key = f"mx.{stamp}"
    s, _ = c("POST", "/api/v1/memories", {"scope": "private", "key": key, "content": "矩阵写入"}, token=tok)
    rec(case, "记忆写入", s == 201, f"实际={s}")
    s, b = c("GET", "/api/v1/memories?limit=50", token=tok)
    hit = any(m.get("key") == key for m in ((b or {}).get("items") or []))
    rec(case, "记忆读回", s == 200 and hit, f"实际={s} 命中={hit}")

    s, _ = c("GET", "/metrics", token="matrix-metrics-token-0123456789abcdef")
    rec(case, "指标端点可用", s == 200, f"实际={s}")
    return True, tok, tid


def quota_case(case, base, port, exe, data_dir, env_extra, stamp):
    """配置项必须真的生效：把每小时配额定成 2，第 3 次提交就该被 429 挡住，
    而且 429 文案里写的数字必须就是 2（文案与实际规则不一致比没有限流更坑）。"""
    c = Client(base)
    if not wait_ready(c):
        rec(case, "实例就绪", False)
        return
    uname = f"mx_{stamp}"
    c("POST", "/api/v1/auth/register",
      {"username": uname, "email": f"{uname}@example.com", "password": "MatrixPass!2026"})
    s, b = c("POST", "/api/v1/auth/login", {"account": uname, "password": "MatrixPass!2026"})
    tok = ((b or {}).get("tokens") or {}).get("access_token")
    codes = []
    body = None
    for i in range(3):
        s, body = c("POST", "/api/v1/tasks", {"mode": "sync", "goal": f"配额用例 {i}", "task_type": "coding",
                                              "budget_tokens": 20000}, token=tok)
        codes.append(s)
    rec(case, "配额定 2/h 时第 3 次提交被 429 挡住", codes == [201, 201, 429], f"实际={codes}")
    msg = ((body or {}).get("message") or "") if isinstance(body, dict) else ""
    rec(case, "429 文案里的数字与配置一致（说的就是 2）", "2 次" in msg, msg[:70])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default="target-gnu/debug/super-agents.exe")
    ap.add_argument("--work", default="")
    args = ap.parse_args()

    exe = str(pathlib.Path(args.exe).resolve())
    if not pathlib.Path(exe).exists():
        print(f"找不到可执行文件: {exe}")
        return 2
    work = pathlib.Path(args.work or (pathlib.Path(os.environ.get("TEMP", ".")) / f"sa-matrix-{int(time.time())}"))
    work.mkdir(parents=True, exist_ok=True)
    scripts = make_runner_scripts(work / "runner-scripts")

    cases = [
        ("A 全默认", {}, {}),
        ("B 存储换内存", {"SA_STORAGE": "memory"}, {"storage": "memory"}),
        ("C 审计换内存", {"SA_AUDIT": "memory"}, {"audit": "memory"}),
        ("D 审计换文件", {"SA_AUDIT": "file"}, {"audit": "file"}),
        ("E 限流换空实现", {"SA_LIMITER": "noop"}, {"limiter": "noop"}),
        ("F 指标换空实现", {"SA_METRICS_EXPORT": "noop"}, {"metrics": "noop"}),
        ("G 执行换真实脚本", {"SA_RUNNER": "process", "SA_RUNNER_DIR": str(scripts)}, {"runner": "process"}),
        ("H 四件同时换", {"SA_STORAGE": "memory", "SA_AUDIT": "memory",
                          "SA_LIMITER": "noop", "SA_METRICS_EXPORT": "noop"},
         {"storage": "memory", "audit": "memory", "limiter": "noop", "metrics": "noop"}),
    ]

    cases.append(("I 配额调到 2/h", {"SA_TASK_SUBMIT_QUOTA_PER_HOUR": "2"}, {"__quota__": True}))

    port = 18200
    for i, (case, env_extra, expected) in enumerate(cases):
        port += i
        data_dir = work / f"case{chr(65+i)}"
        data_dir.mkdir(parents=True, exist_ok=True)
        stamp = uuid.uuid4().hex[:6]
        print(f"\n===== {case} | {env_extra or '无额外环境变量'} =====")
        proc = start(exe, port, data_dir, env_extra)
        try:
            if expected.get("__quota__"):
                quota_case(case, f"http://127.0.0.1:{port}", port, exe, data_dir, env_extra, stamp)
                continue
            ok, tok, tid = core_path(case, f"http://127.0.0.1:{port}", expected, stamp)
            # 默认组额外验一次重启后数据仍在（持久化不是装饰）
            if case.startswith("A") and ok:
                # 先验"写穿"：提交一个新任务后**零等待**直接强杀进程（模拟任务管理器结束进程），
                # 重启后它必须还在。这是 500ms 定时落盘窗口覆盖不到的真实缺口。
                c0 = Client(f"http://127.0.0.1:{port}")
                s, b = c0("POST", "/api/v1/tasks",
                          {"mode": "sync", "goal": f"写穿验证 {uuid.uuid4().hex[:6]}", "task_type": "coding",
                           "budget_tokens": 20000}, token=tok)
                tid_wt = (b or {}).get("id")
                rec(case, "写穿验证前置：任务已创建", s == 201 and bool(tid_wt), f"实际={s}")
                stop(proc)
                proc = start(exe, port, data_dir, env_extra)
                c = Client(f"http://127.0.0.1:{port}")
                ready = wait_ready(c)
                s, b = c("POST", "/api/v1/auth/login",
                         {"account": f"mx_{stamp}", "password": "MatrixPass!2026"})
                rec(case, "重启后账号仍在（json 落盘生效）", ready and s == 200, f"实际={s}")
                tok2 = ((b or {}).get("tokens") or {}).get("access_token")
                s, b = c("GET", f"/api/v1/tasks/{tid}", token=tok2)
                rec(case, "重启后任务仍在", s == 200 and (b or {}).get("id") == tid, f"实际={s}")
                s, b = c("GET", f"/api/v1/tasks/{tid_wt}", token=tok2)
                rec(case, "写穿生效：提交后零等待强杀，重启后任务仍在（不是 404）",
                    s == 200 and (b or {}).get("id") == tid_wt, f"实际={s}")
        finally:
            stop(proc)

    total = len(rows)
    passed = sum(1 for r in rows if r[2] == OK)
    failed = [r for r in rows if r[2] == FAIL]
    print("\n" + "=" * 70)
    print(f"矩阵合计 {total} 项 | 通过 {passed} | 失败 {len(failed)}")
    for case, name, _, detail in failed:
        print(f"  - [{case}] {name}   {detail}")
    print("=" * 70)
    pathguard.safe_rmtree(work, base=pathlib.Path(os.environ.get("TEMP", ".")),
                          allow_prefix="sa-matrix-", what="矩阵临时工作区", warn_only=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
