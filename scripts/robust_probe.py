#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""鲁棒性探针：对已编译的 exe 打一批"刁钻但合法客户端可能发出"的请求，
记录真实状态码与响应体，用于核查参数边界行为是否可预期、是否静默改语义。

不做破坏性操作，不删除任何目录（数据目录用唯一时间戳子目录）。

用法：
    python scripts/robust_probe.py --exe "<exe 绝对路径>"

退出码：0 = 全部探测项符合 EXPECTED 表；1 = 有不符合项（会逐条列出）。
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_EXE = pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "sa-build" / "target" / "debug" / "super-agents.exe"


def request(base, method, path, body=None, token=None):
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # 连接失败 / 超时
        return -1, str(e).encode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default=str(DEFAULT_EXE))
    ap.add_argument("--port", type=int, default=18124)
    args = ap.parse_args()

    exe = pathlib.Path(args.exe)
    if not exe.exists():
        print(f"[FAIL] exe 不存在：{exe}")
        return 1

    root = HERE.parent / "docs" / "evidence" / f"robust-{int(time.time())}"
    root.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(
        {
            "SA_BIND": f"127.0.0.1:{args.port}",
            "SA_DATA_DIR": str(root / "data"),
            "SA_JWT_SECRET": "x" * 40,
            "SA_PBKDF2_ITERATIONS": "1000",
            "SA_ENV": "probe",
            "SA_ALLOW_REGISTRATION": "true",
            "SA_AUDIT": "file",
            "SA_STORAGE": "json",
        }
    )
    log_path = root / "run.log"
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen([str(exe)], env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{args.port}"
    rows = []
    try:
        ready = -1
        for _ in range(160):
            ready, _b = request(base, "GET", "/readyz")
            if ready == 200:
                break
            time.sleep(0.25)
        print(f"[ready] {ready}")

        uname = f"probe{int(time.time()) % 100000}"
        s, b = request(base, "POST", "/api/v1/auth/register",
                       {"username": uname, "password": "Probe#12345", "email": f"{uname}@example.com"})
        text = b.decode("utf-8", "replace")
        print(f"[register] {s} body={text[:200]}")
        try:
            token = (json.loads(text).get("tokens") or {}).get("access_token")
        except Exception:
            token = None
        if not token:
            print("[FAIL] 注册未拿到 access_token，后续探测无意义，先修这一项")
            return 1

        big_goal = "x" * 100_000          # 100KB，低于默认 max_body（256KB）
        long_q = "a" * 2000

        cases = [
            # (期望状态集合, method, path, body, 说明)
            # —— 分页越界：第四轮升级后口径为「422 + 允许范围」，不再静默夹持
            ({422}, "GET", "/api/v1/memories?limit=0", None, "limit=0 应 422 并给出范围"),
            ({422}, "GET", "/api/v1/memories?limit=-1", None, "limit=-1 应 422 并给出范围"),
            ({422}, "GET", "/api/v1/memories?limit=100000", None, "limit 超上限应 422 并给出范围"),
            ({200}, "GET", "/api/v1/memories?offset=999999", None, "超大 offset 是否空页而非 panic"),
            ({200}, "GET", "/api/v1/memories?q=" + long_q, None, "超长 q 是否仍可用"),
            ({422}, "GET", "/api/v1/tasks?status=BOGUS", None, "非法枚举是否 422"),
            ({422}, "GET", "/api/v1/tasks?limit=abc", None, "非数字 limit 是否 422"),
            ({404}, "GET", "/api/v1/memories/not-a-uuid", None, "非法 id 是否 404 而非 500"),
            ({404}, "GET", "/api/v1/memories/00000000-0000-0000-0000-000000000000", None, "不存在 id 是否 404"),
            ({422}, "GET", "/api/v1/observability/audit?limit=0", None, "审计 limit=0 应 422 并给出范围"),
            ({404}, "GET", "/api/v1/nonexistent/path", None, "未知路径是否 404 而非 500"),
            ({422}, "POST", "/api/v1/tasks", {"mode": "sync", "goal": ""}, "空目标是否 422"),
            ({413, 422}, "POST", "/api/v1/tasks", {"mode": "sync", "goal": big_goal}, "100KB 目标必须明确拒绝"),
            ({422}, "POST", "/api/v1/tasks", {"mode": "sync", "goal": "  x  "}, "纯空白目标必须 422"),
            ({200}, "GET", "/api/v1/memories?limit=1&offset=0", None, "正常分页基线"),
        ]
        ok = 0
        bad = []
        # 错误体必须是「中文可直接展示」的字段级形状：不含 serde/英文栈信息
        leaks = []
        for exp, method, path, body, desc in cases:
            s, b = request(base, method, path, body, token)
            got = s in exp
            ok += 1 if got else 0
            snip = b[:120].decode("utf-8", "replace")
            if s >= 400 and any(k in snip for k in ("invalid digit", "expected ", "usize", "at line 1")):
                leaks.append(f"{path} -> {snip}")
            print(f"{'PASS' if got else 'FAIL'} {s:>4} {method:5} {path[:46]:<46} {desc} | {snip}")
            rows.append({"desc": desc, "method": method, "path": path, "status": s,
                         "expected": sorted(exp), "pass": got, "body": snip})
            if not got:
                bad.append(desc)
        print(f"\n合计 {len(cases)} 项 | 符合预期 {ok} | 不符合 {len(bad)}")
        for d in bad:
            print("  不符合：", d)
        print(f"英文/内部细节泄漏 {len(leaks)} 处")
        for d in leaks:
            print("  泄漏：", d)
        (root / "probe.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"明细已留档：{root / 'probe.json'}")
        return 0 if not bad and not leaks else 1
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except Exception:
            proc.kill()
        log.close()


if __name__ == "__main__":
    sys.exit(main())
