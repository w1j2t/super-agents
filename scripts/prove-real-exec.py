#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prove-real-exec.py —— 证明 process 后端**真的**执行了外部脚本，而不是偷偷回退到模拟器。

为什么非要做这一步：
  "runner=process 且任务 succeeded" 有两种可能——
    (A) 真调起了 Python 脚本，拿到了脚本输出；
    (B) 没调起来，悄悄退回 SimRunner，任务照样 succeeded。
  只看状态码两者分不开，这就是自欺欺人的温床。

本脚本的做法：
  1. 复制一份 workers 目录到临时位置，把 super-agent.py 换成一个"指纹脚本"，
     唯一输出一个随机 MAGIC 串（每次运行都不同，无法命中缓存、无法靠文档伪造）；
  2. 以 SA_RUNNER=process + SA_RUNNER_DIR=临时目录 起真进程，提交一条任务；
  3. 在任务详情 / 轨迹的**完整 JSON** 里搜 MAGIC：
       搜到 → 真执行（证据是脚本自己吐出的随机串，不是状态字段）；
       搜不到 → 判定为假执行，退出码 1。

用法：python scripts/prove-real-exec.py --exe <super-agents.exe>
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
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


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def http(method, url, body=None, token=None, timeout=15.0):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


FINGERPRINT_TPL = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""指纹脚本：唯一职责是吐出一个本次运行独有、无法预测的串。"""
import json, os, sys

MAGIC = {magic!r}

def main():
    summary = "指纹步骤已真实执行 MAGIC=" + MAGIC
    payload = {{
        "magic": MAGIC,
        "goal_len": len(os.environ.get("SA_GOAL", "")),
        "step": os.environ.get("SA_STEP_INDEX", ""),
        "pid": os.getpid(),
    }}
    print(summary)
    print("[[SA]]" + json.dumps(payload, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())
'''


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", required=True)
    args = ap.parse_args()
    exe = Path(args.exe).resolve()
    if not exe.exists():
        print(f"[FAIL] 找不到 exe: {exe}")
        return 2

    magic = "PROOF-" + secrets.token_hex(8).upper()
    work = Path(tempfile.mkdtemp(prefix="sa-proof-"))
    wdir = work / "workers"
    # 先铺一套**真实**脚本，保证整条链路能走完；再把 super-agent.py 换成指纹版。
    # 只放一个脚本的做法试过：任务会因后续步骤缺件而 failed，指纹虽然能命中，
    # 但"终态 failed"说明不了链路完整，证明力不足。
    shutil.copytree(ROOT / "workers", wdir, dirs_exist_ok=True)
    (wdir / "super-agent.py").write_text(FINGERPRINT_TPL.format(magic=magic), encoding="utf-8")

    port = free_port()
    env = {
        **os.environ,
        "SA_BIND": f"127.0.0.1:{port}",
        "SA_DATA_DIR": str(work / "data"),
        "SA_ENV": "test",
        "SA_ALLOW_REGISTRATION": "true",
        "SA_FLUSH_ON_WRITE": "true",
        "SA_RUNNER": "process",
        "SA_RUNNER_DIR": wdir.as_posix(),
        "RUST_LOG": "info",
    }
    log = work / "server.log"
    base = f"http://127.0.0.1:{port}"
    fh = log.open("w", encoding="utf-8")
    proc = subprocess.Popen([str(exe)], env=env, stdout=fh, stderr=subprocess.STDOUT, cwd=str(work))

    try:
        deadline = time.time() + 25
        while time.time() < deadline:
            if proc.poll() is not None:
                print(f"[FAIL] 进程提前退出：{log.read_text(encoding='utf-8', errors='replace')[-500:]}")
                return 1
            try:
                if http("GET", base + "/readyz", timeout=1.5)[0] == 200:
                    break
            except Exception:
                time.sleep(0.15)
        else:
            print("[FAIL] 25s 未就绪")
            return 1

        u = f"proof_{uuid.uuid4().hex[:8]}"
        http("POST", base + "/api/v1/auth/register",
             {"username": u, "email": f"{u}@example.com", "password": "ProofTest!2026"})
        code, body = http("POST", base + "/api/v1/auth/login",
                          {"account": u, "password": "ProofTest!2026"})
        if code != 200:
            print(f"[FAIL] 登录失败 {code}")
            return 1
        token = body["tokens"]["access_token"]

        code, task = http("POST", base + "/api/v1/tasks",
                          {"mode": "sync", "goal": "验证 process 后端真实执行外部脚本"}, token=token)
        if code not in (200, 201):
            print(f"[FAIL] 提交失败 {code} {str(task)[:200]}")
            return 1
        tid = task["id"]

        code, detail = http("GET", base + f"/api/v1/tasks/{tid}", token=token)
        blob = json.dumps(detail, ensure_ascii=False)
        code2, trace = http("GET", base + f"/api/v1/tasks/{tid}/trace", token=token)
        blob += json.dumps(trace, ensure_ascii=False)

        hit = magic in blob
        status = detail.get("status")
        print(f"注入指纹：{magic}")
        print(f"任务 {tid} 终态：{status}")
        if status != "succeeded":
            print("失败原因（截取）：" + json.dumps(detail, ensure_ascii=False)[:600])

        # 附一条独立旁证：runner 自己的日志里应当出现脚本路径
        slog = log.read_text(encoding="utf-8", errors="replace")
        path_seen = "super-agent.py" in slog

        if hit and status == "succeeded":
            print("[OK  ] 指纹命中 + 任务成功 → 全链路真的跑了我们的脚本（非模拟回退）")
            print(f"[{'OK  ' if path_seen else 'WARN'}] 服务日志{'中' if path_seen else '未'}出现脚本文件名"
                  f"（仅作旁证，不作为判定依据）")
            return 0

        if hit and status != "succeeded":
            print(f"[FAIL] 指纹命中但终态={status} → 脚本被执行了，链路没走完")
        else:
            print("[FAIL] 未找到指纹 → process 后端没有真正执行我们提供的脚本")
        print("服务日志尾部：")
        print("\n".join(slog.splitlines()[-12:]))
        return 1
    finally:
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(6)
                except subprocess.TimeoutExpired:
                    proc.kill()
        finally:
            fh.close()
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
