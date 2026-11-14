#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe.py —— 手工探测：找一个"能跑通"之外的真实问题。

与 smoke.py 的区别：
  smoke.py 验证"设计内的路径是否通"（断言已知契约）；
  probe.py 专门去踩**边角与不讲理输入**，输出的是"观察到什么"而非"是否等于期望"。

用法：
    python scripts/probe.py --exe <path/to/super-agents.exe>
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HOST = "127.0.0.1"
results: list[tuple[str, str]] = []


def note(tag: str, msg: str) -> None:
    results.append((tag, msg))
    print(f"[{tag}] {msg}")


def req(method, path, base, body=None, token=None, raw=None, headers=None, timeout=15):
    url = base + path
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    if token:
        h["Authorization"] = f"Bearer {token}"
    r = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            txt = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(txt), dict(resp.headers), txt
            except json.JSONDecodeError:
                return resp.status, None, dict(resp.headers), txt
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(txt), dict(e.headers), txt
        except json.JSONDecodeError:
            return e.code, None, dict(e.headers), txt
    except Exception as e:  # 连接失败等
        return -1, None, {}, repr(e)


def wait_ready(base, proc, limit=40):
    for _ in range(limit * 2):
        if proc.poll() is not None:
            return False
        s, _, _, _ = req("GET", "/readyz", base, timeout=2)
        if s == 200:
            return True
        time.sleep(0.25)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", required=True)
    args = ap.parse_args()

    work = tempfile.mkdtemp(prefix="sa-probe-")
    port = 18111
    base = f"http://{HOST}:{port}"
    env = {
        **os.environ,
        "SA_BIND": f"{HOST}:{port}",
        "SA_DATA_DIR": os.path.join(work, "data"),
        "SA_ENV": "dev",
        "SA_METRICS_TOKEN": "probe-metrics-token-0123456789",
        "SA_JWT_SECRET": "probe-jwt-secret-0123456789abcdef",
    }
    log = open(os.path.join(work, "srv.log"), "wb")
    proc = subprocess.Popen([args.exe], env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        if not wait_ready(base, proc):
            print("服务未能就绪，日志尾部：")
            print(open(log.name, "rb").read().decode("utf-8", "replace")[-2000:])
            return 1

        # ---------- 0. 建号 + 登录 ----------
        s, b, _, _ = req("POST", "/api/v1/auth/register", base, {
            "username": "probe_user", "email": "probe@example.com",
            "password": "Probe#2026pass", "display_name": "Probe"})
        note("register", f"status={s} onboarding={b.get('onboarding_status') if b else None}")
        s, b, _, _ = req("POST", "/api/v1/auth/login", base,
                         {"account": "probe_user", "password": "Probe#2026pass"})
        tok = b["tokens"]["access_token"] if s == 200 else ""
        rtok = b["tokens"]["refresh_token"] if s == 200 else ""
        note("login", f"status={s} access_len={len(tok)}")

        # ---------- 1. 未知路由 / 错误方法 ----------
        s, b, _, txt = req("GET", "/api/v1/does-not-exist", base, token=tok)
        note("未知路由", f"status={s} body={txt[:120]}")
        s, b, _, txt = req("DELETE", "/api/v1/auth/login", base, token=tok)
        note("错误方法(DELETE 登录)", f"status={s} body={txt[:120]}")
        s, b, _, txt = req("GET", "/tasks/xxx/events", base, token=tok)
        note("契约里没有的 /tasks/{id}/events", f"status={s}")

        # ---------- 2. 分页边界 ----------
        for q in ["", "?limit=0", "?limit=-1", "?limit=99999", "?offset=-5", "?limit=abc"]:
            s, b, _, txt = req("GET", f"/api/v1/memories{q}", base, token=tok)
            n = len(b.get("items", [])) if isinstance(b, dict) and "items" in b else "n/a"
            note("memories 分页", f"{q or '(空)'} -> status={s} items={n} {'' if s==200 else txt[:80]}")
        s, b, _, _ = req("GET", "/api/v1/tasks?limit=3&offset=0", base, token=tok)
        note("tasks 分页字段", f"status={s} keys={sorted(b.keys()) if isinstance(b, dict) else b}")
        s, b, _, _ = req("GET", "/api/v1/observability/audit?limit=2", base, token=tok)
        note("audit 分页字段", f"status={s} keys={sorted(b.keys()) if isinstance(b,dict) else b}")

        # ---------- 3. 错误体一致性 ----------
        s, b, _, _ = req("GET", "/api/v1/auth/me", base)  # 无 token
        note("无 token /auth/me", f"status={s} code={b.get('code') if b else None}")
        s, b, _, _ = req("GET", "/api/v1/auth/me", base, token="garbage.token.here")
        note("坏 token /auth/me", f"status={s} code={b.get('code') if b else None}")
        s, b, _, _ = req("GET", "/api/v1/memories/nope-id", base, token=tok)
        note("不存在 memory", f"status={s} code={b.get('code') if b else None}")
        s, b, _, _ = req("POST", "/api/v1/tasks", base, {"goal": "ab"}, token=tok)
        note("goal 过短", f"status={s} code={b.get('code') if b else None} msg={(b or {}).get('message','')[:60]}")

        # ---------- 4. 非 JSON / 空 body / 超大 body ----------
        s, b, _, txt = req("POST", "/api/v1/auth/login", base, raw=b"not-json")
        note("body 非 JSON", f"status={s} body={txt[:100]}")
        s, b, _, txt = req("POST", "/api/v1/auth/login", base, raw=b"")
        note("body 为空", f"status={s} body={txt[:100]}")
        s, b, _, txt = req("POST", "/api/v1/auth/login", base,
                           raw=json.dumps({"account": "probe_user", "password": "x" * 20000}).encode())
        note("超大 body(20KB)", f"status={s} body={txt[:100]}")

        # ---------- 5. 幂等 / 重复提交 ----------
        payload = {"goal": "整理本周会议纪要并列出三条待办事项", "task_type": "text"}
        s1, b1, _, _ = req("POST", "/api/v1/tasks", base, payload, token=tok)
        s2, b2, _, _ = req("POST", "/api/v1/tasks", base, payload, token=tok)
        id1 = (b1 or {}).get("task", {}).get("id") or (b1 or {}).get("id")
        id2 = (b2 or {}).get("task", {}).get("id") or (b2 or {}).get("id")
        note("重复提交同 goal", f"{s1}/{s2} 是否产生两个任务: {id1 != id2}（无 Idempotency-Key 即预期如此）")

        # ---------- 6. 并发刷新同一 refresh_token ----------
        import threading
        got = []
        def refresh():
            got.append(req("POST", "/api/v1/auth/refresh", base, {"refresh_token": rtok})[0])
        ts = [threading.Thread(target=refresh) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        note("并发 refresh 同一 token", f"4 个并发结果={sorted(got)}（期望恰好 1 个 200，其余 401）")

        # ---------- 7. CORS ----------
        s, b, h, _ = req("OPTIONS", "/api/v1/auth/login", base,
                         headers={"Origin": "http://localhost:5173",
                                  "Access-Control-Request-Method": "POST"})
        note("CORS 预检", f"status={s} allow-origin={h.get('Access-Control-Allow-Origin')} "
                          f"allow-methods={h.get('Access-Control-Allow-Methods')}")
        s, b, h, _ = req("GET", "/healthz", base, headers={"Origin": "http://localhost:5173"})
        note("普通响应带 AC-Allow-Origin", f"status={s} -> {h.get('Access-Control-Allow-Origin')}")

        # ---------- 8. 指标与可观测 ----------
        s, _, _, txt = req("GET", "/metrics", base)
        note("/metrics 无令牌", f"status={s}")
        s, _, _, txt = req("GET", "/metrics", base, headers={"Authorization": "Bearer probe-metrics-token-0123456789"})
        lines = [l for l in txt.splitlines() if l.startswith("sa_") and not l.startswith("sa_http")]
        note("/metrics 静态令牌", f"status={s} sa_* 指标行数={len(lines)}")
        note("  sa_http_requests_total 是否存在", str("sa_http_requests_total" in txt))
        s, b, _, _ = req("GET", "/api/v1/observability/overview", base, token=tok)
        note("overview 键", f"status={s} keys={sorted(b.keys()) if isinstance(b,dict) else b}")

        # ---------- 9. 时间与时区 ----------
        s, b, _, _ = req("GET", "/api/v1/auth/me", base, token=tok)
        note("/auth/me 字段", f"keys={sorted(b.keys()) if isinstance(b,dict) else b}")
        ts_fields = [k for k in (b or {}) if "at" in k or "time" in k]
        for k in ts_fields:
            note(f"  时间字段 {k}", f"{b[k]}（带 Z？{str(b[k]).endswith('Z')}）")

        # ---------- 10. 列表端点是否免鉴权 ----------
        for p in ["/api/v1/agents", "/api/v1/capabilities"]:
            s, b, _, _ = req("GET", p, base)
            cnt = len(b) if isinstance(b, list) else (len(b.get("items", [])) if isinstance(b, dict) else "n/a")
            note(f"无 token {p}", f"status={s} 条数={cnt}")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()

    print("\n" + "=" * 72)
    print(f"共观察 {len(results)} 条")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
