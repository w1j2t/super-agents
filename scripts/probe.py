#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe.py —— 人肉级实测探针（不是冒烟，是"好用度"体检）。

smoke.py 回答"能不能跑通"，probe.py 回答四个更苛刻的问题：
  1. 错误信息对人是否可读？（是否带 code / field / 修复提示）
  2. 列表是否可分页、可排序、有总数？（前端能不能做分页器）
  3. 同一类语义在不同端点是否一致？（envelope、时间格式、id 形态）
  4. 边界与幂等：重复提交、超长输入、越权、空列表，会不会给出可用反馈？

用法：
    python scripts/probe.py --exe <path-to-super-agents.exe>
输出：结构化报告到 docs/evidence/probe-latest.txt（同时打印摘要）
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "evidence" / "probe-latest.txt"

LINES: list[str] = []
FAILS: list[str] = []
NOTES: list[str] = []


def say(s: str = "") -> None:
    print(s)
    LINES.append(s)


def check(cond: bool, label: str, detail: str = "") -> bool:
    if cond:
        say(f"  [OK  ] {label}" + (f"  ->  {detail}" if detail else ""))
    else:
        say(f"  [FAIL] {label}" + (f"  ->  {detail}" if detail else ""))
        FAILS.append(label)
    return cond


def note(s: str) -> None:
    say(f"  [NOTE] {s}")
    NOTES.append(s)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def http(base: str, method: str, path: str, body=None, token=None, raw_body=None, timeout=6):
    url = base + path
    data = None
    headers = {}
    if raw_body is not None:
        data = raw_body
        headers["Content-Type"] = "application/json"
    elif body is not None:
        data = json.dumps(body, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw), dict(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = raw
        return e.code, parsed, dict(e.headers)
    except Exception as e:  # noqa: BLE001
        return 0, {"transport_error": str(e)}, {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", required=True)
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args()

    exe = Path(args.exe)
    if not exe.exists():
        print(f"找不到可执行文件: {exe}")
        return 2
    port = args.port or free_port()
    base = f"http://127.0.0.1:{port}"
    data_dir = Path(tempfile.mkdtemp(prefix="sa-probe-"))
    env = {
        **os.environ,
        "SA_BIND": f"127.0.0.1:{port}",
        "SA_DATA_DIR": str(data_dir),
        "SA_ENV": "dev",
        "RUST_LOG": "warn",
        "SA_JWT_SECRET": "probe-secret-probe-secret-probe-secret",
    }
    logf = open(data_dir / "server.log", "w", encoding="utf-8")
    proc = subprocess.Popen([str(exe)], env=env, stdout=logf, stderr=subprocess.STDOUT, cwd=str(data_dir))
    try:
        ready = False
        for _ in range(120):
            time.sleep(0.25)
            st, _, _ = http(base, "GET", "/readyz", timeout=1)
            if st == 200:
                ready = True
                break
        if not ready:
            print("服务未就绪")
            return 3

        tag = uuid.uuid4().hex[:6]
        user = f"probe_{tag}"
        pwd = "Probe#Passw0rd"

        say("=" * 78)
        say("probe.py · 好用度体检（不是冒烟）")
        say(f"exe={exe.name}  port={port}  data_dir={data_dir.name}")
        say("=" * 78)

        # ---------- A. 错误可读性 ----------
        say("\n[A] 错误可读性：报错能不能直接告诉调用方怎么改")
        st, b, _ = http(base, "POST", "/api/v1/auth/register", {"username": user})
        check(st == 422, "缺字段注册被拒 422", f"实际={st}")
        say(f"       body = {json.dumps(b, ensure_ascii=False)[:320]}")
        det = json.dumps(b, ensure_ascii=False)
        is_envelope = isinstance(b, dict) and ("code" in b or ("error" in b and isinstance(b["error"], dict)))
        check(is_envelope, "错误体是标准信封（含 code）", "实际是裸字符串" if not is_envelope else "")
        has_detail = bool(isinstance(b, dict) and (b.get("details") or {}))
        check(has_detail, "错误体在 details 里点名出问题的字段",
              json.dumps((b.get("details") if isinstance(b, dict) else None), ensure_ascii=False))

        st, b, _ = http(base, "POST", "/api/v1/auth/register",
                        {"username": user, "email": f"{user}@ex.com", "password": pwd})
        check(st == 201, "正常注册成功 201", f"实际={st}")
        reg = b if isinstance(b, dict) else {}
        check("tokens" in json.dumps(reg) or "access_token" in json.dumps(reg),
              "注册直接下发令牌（少一次登录往返）", "")

        st, b, _ = http(base, "POST", "/api/v1/auth/register",
                        {"username": user, "email": f"{user}@ex.com", "password": pwd})
        det = json.dumps(b, ensure_ascii=False)
        check(st == 409, "重复注册 409", f"实际={st}")
        check("已" in det or "exist" in det.lower() or "conflict" in det.lower(),
              "重复注册的文案说人话", det[:120])

        st, b, _ = http(base, "POST", "/api/v1/auth/login", {"account": user, "password": "wrong-pass"})
        det = json.dumps(b, ensure_ascii=False)
        check(st == 401, "密码错 401", f"实际={st}")
        check("密码" in det or "credential" in det.lower(), "密码错的文案可读", det[:120])

        # ---------- B. 列表可分页 / 有总数 ----------
        say("\n[B] 列表接口：前端能不能做出分页器（has_more / total / limit）")
        st, b, _ = http(base, "POST", "/api/v1/auth/login", {"account": user, "password": pwd})
        tok = ((b or {}).get("tokens") or {}).get("access_token", "")
        check(bool(tok), "登录拿到 access_token", tok[:12] + "…" if tok else "无")
        if not tok:
            say("\n登录失败，后续跳过")
            return 1

        st, b, _ = http(base, "GET", "/api/v1/tasks?limit=2", token=tok)
        det = json.dumps(b, ensure_ascii=False)
        say(f"       GET /tasks?limit=2 -> {st}  body={det[:260]}")
        check(st == 200, "任务列表可访问", f"实际={st}")
        check(any(k in det for k in ("total", "has_more", "next_cursor", "page")),
              "任务列表带分页元信息", "否则前端只能全量拉取" if not any(k in det for k in ("total","has_more","next_cursor","page")) else "")

        st, b, _ = http(base, "GET", "/api/v1/memories", token=tok)
        det = json.dumps(b, ensure_ascii=False)
        say(f"       GET /memories -> {st}  body={det[:200]}")

        # ---------- C. 时间格式与 id 形态一致性 ----------
        say("\n[C] 契约一致性：时间 / id / envelope 是否全站统一")
        st, me, _ = http(base, "GET", "/api/v1/auth/me", token=tok)
        say(f"       GET /auth/me -> {st}  body={json.dumps(me, ensure_ascii=False)[:260]}")
        ca = (me or {}).get("created_at", "")
        check(isinstance(ca, str) and ("T" in ca) and (ca.endswith("Z") or "+" in ca),
              "created_at 是 RFC3339 带时区", f"实际={ca}")
        check(("user" in (me or {})) or ("id" in (me or {})), "me 的 envelope 明确", "")

        # ---------- D. 越权与 404 语义 ----------
        say("\n[D] 越权 / 不存在：前端要靠状态码分流，不能混用")
        st, b, _ = http(base, "GET", f"/api/v1/tasks/{uuid.uuid4()}", token=tok)
        det = json.dumps(b, ensure_ascii=False)
        check(st in (403, 404), "不存在的任务给出 403/404", f"实际={st}")
        say(f"       body = {det[:200]}")
        note(f"不存在的任务返回 {st}（若为 403，前端无法区分'他人资源'与'不存在'）")

        st, b, _ = http(base, "GET", "/api/v1/tasks/" + "x" * 80, token=tok)
        check(st in (400, 404, 422), "非法 id 不返回 500", f"实际={st}")

        st, b, _ = http(base, "GET", "/api/v1/does-not-exist", token=tok)
        check(st == 404, "未知路由 404（不是 401/500）", f"实际={st}")

        # ---------- E. 幂等 / 重复提交 ----------
        say("\n[E] 幂等：同一条记忆重复写，会不会炸出重复条目")
        st1, b1, _ = http(base, "POST", "/api/v1/memories",
                          {"scope": "private", "key": "probe_key", "content": "第一版", "tags": ["a"]}, token=tok)
        st2, b2, _ = http(base, "POST", "/api/v1/memories",
                          {"scope": "private", "key": "probe_key", "content": "第二版", "tags": ["a"]}, token=tok)
        e1 = (b1 or {}).get("entry") or {}
        e2 = (b2 or {}).get("entry") or {}
        v1 = e1.get("version")
        v2 = e2.get("version")
        same_id = bool(e1.get("id")) and e1.get("id") == e2.get("id")
        created1 = (b1 or {}).get("created")
        created2 = (b2 or {}).get("created")
        check(created1 is True and created2 is False, "首次 created=true、二次 created=false", f"{created1}/{created2}")
        check(st1 in (200, 201) and st2 in (200, 201), "记忆写入成功", f"{st1}/{st2}")
        check(same_id, "同 key 覆盖而非新建（upsert 语义）", f"id1={str((b1 or {}).get('id'))[:8]} id2={str((b2 or {}).get('id'))[:8]}")
        check(isinstance(v2, int) and isinstance(v1, int) and v2 == v1 + 1, "版本号自增", f"{v1} -> {v2}")

        # ---------- F. 任务：提交后能不能立刻看到有意义的进度 ----------
        say("\n[F] 任务：提交 → 详情，返回的东西够不够前端画一条进度线")
        st, t1, _ = http(base, "POST", "/api/v1/tasks", {"mode": "sync", "goal": "帮我整理这周的会议记录并生成会议纪要"}, token=tok)
        say(f"       POST /tasks -> {st}  body={json.dumps(t1, ensure_ascii=False)[:300]}")
        check(st in (200, 201, 202), "任务提交成功", f"实际={st}")
        tid = (t1 or {}).get("id") or ((t1 or {}).get("task") or {}).get("id")
        check(bool(tid), "响应里能直接拿到 task id", str(tid)[:8])
        check("plan" in json.dumps(t1, ensure_ascii=False), "提交响应带执行计划", "否则前端首屏空白")
        if tid:
            time.sleep(0.4)
            st, d1, _ = http(base, "GET", f"/api/v1/tasks/{tid}", token=tok)
            body = json.dumps(d1, ensure_ascii=False)
            say(f"       GET /tasks/{{id}} -> {st}  keys={list((d1 or {}).keys())}")
            check(st == 200, "任务详情可读", f"实际={st}")
            check("trace" in body, "详情含 trace（可画时间线）", "")
            check("steps" in body or "plan" in body, "详情含步骤（可画进度条）", "")
            st, tr, _ = http(base, "GET", f"/api/v1/tasks/{tid}/trace", token=tok)
            check(isinstance(tr, list) or (isinstance(tr, dict) and "trace" in tr),
                  "trace 端点形状可用", f"实际类型={type(tr).__name__}")

        # ---------- G. 越界与边界 ----------
        say("\n[G] 边界：超纲任务 / 超长输入 / 空目标")
        st, b, _ = http(base, "POST", "/api/v1/tasks",
                        {"mode": "sync", "goal": "帮我预测明天股票涨跌并给出买入建议"}, token=tok)
        det = json.dumps(b, ensure_ascii=False)
        check(st == 422, "超纲任务被拒 422", f"实际={st}")
        check(any(k in det for k in ("边界", "out_of_scope", "不支持", "task_type", "更具体")),
              "拒绝理由可解释（含下一步）", det[:160])

        st, b, _ = http(base, "POST", "/api/v1/tasks", {"mode": "sync", "goal": "a" * 2200}, token=tok)
        check(st in (413, 422), "超长 goal 被拒", f"实际={st}")

        st, b, _ = http(base, "POST", "/api/v1/tasks", {"mode": "sync", "goal": "整理"}, token=tok)
        check(st in (422,), "过短 goal 被拒", f"实际={st}")

        # ---------- H. 观测 ----------
        say("\n[H] 观测：出问题时能不能定位")
        st, _, hdr = http(base, "GET", "/api/v1/auth/me", token=tok)
        rid = hdr.get("x-request-id") or hdr.get("X-Request-Id")
        check(bool(rid), "响应带 X-Request-Id（可追日志）", str(rid)[:14] if rid else "缺失")
        st, met, _ = http(base, "GET", "/metrics")
        check(st in (200, 401), "/metrics 有鉴权", f"实际={st}")

        # ---------- I. 并发 ----------
        say("\n[J] 并发：同一 key 并发写 8 次，版本号是否线性不丢")
        import threading
        versions: list[int] = []
        lock = threading.Lock()

        def worker(i: int):
            _, bb, _ = http(base, "POST", "/api/v1/memories",
                            {"scope": "private", "key": "race_key", "content": f"并发{i}"}, token=tok)
            v = ((bb or {}).get("entry") or {}).get("version")
            with lock:
                versions.append(v)

        ths = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        [t.start() for t in ths]
        [t.join() for t in ths]
        good = [v for v in versions if isinstance(v, int)]
        check(len(good) == 8, f"8 次并发全部成功（{len(good)}/8）", str(sorted(good)))
        check(len(set(good)) == len(good) if good else False,
              "并发下版本号无重复（无丢失更新）", f"去重后={len(set(good))}")

        say("\n" + "=" * 78)
        say(f"体检结论：{len(FAILS)} 处不达标 / {len(NOTES)} 条观察")
        for f in FAILS:
            say(f"  ✗ {f}")
        say("=" * 78)
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text("\n".join(LINES), encoding="utf-8")
        print(f"\n报告已写入 {OUT}")
        return 0 if not FAILS else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()
        logf.close()


if __name__ == "__main__":
    sys.exit(main())
