#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
swap-test.py —— 换件实测：证明"换一个部件只换那一个，其余照常工作"。

它回答的不是"代码里有没有 trait"，而是三件真事：
    换成另一个实现，进程能不能起来？注册表说的是不是实话？业务流还通不通？

做法：
  0. 先跑一次**基线**（不注入任何槽位变量），从注册表读出各槽位的真实默认实现；
     —— 基线不写死。写死默认值等于替程序宣布"默认应该是什么"，那是猜，不是测。
  1. 再逐个枚举 5 个槽位的全部可用实现，每次只改一个环境变量，
     独立数据目录 + 随机端口起一个真进程，然后校验：
       a) `/readyz` 200（起得来）
       b) 该槽位 `active` == 注入值（注册表不撒谎）
       c) 该槽位 `health` 落在约定集合（不出现自造字符串）
       d) **其余槽位 active 必须等于基线**（换件不牵连别人，这是模块化的核心承诺）
       e) 跑最小业务流：注册 → 登录 → 提交任务 → 查详情，任务必须到达终态
  2. 输出矩阵；任何一格不符即退出码 1。

用法：
    python scripts/swap-test.py --exe <super-agents.exe>
    python scripts/swap-test.py --exe ... --only runner      # 只测某个槽位
"""
from __future__ import annotations

import argparse
import json
import os
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

# health 的合法取值：ok 表示完全正常，其余都表示"能用但有前提"
HEALTH_OK = {
    "ok",
    "simulated",
    "no_persistence",
    "not_enforcing",
    "not_recording",
    "unknown_backend",
}

# 槽位 -> (注册表字段, 环境变量, 可选值)
SLOTS: dict[str, tuple[str, str, list[str]]] = {
    "storage": ("storage", "SA_STORAGE", ["json", "memory"]),
    "runner": ("runner", "SA_RUNNER", ["sim", "process"]),
    "audit": ("audit", "SA_AUDIT", ["file", "memory"]),
    "limiter": ("limiter", "SA_LIMITER", ["memory", "noop"]),
    "metrics": ("metrics", "SA_METRICS_EXPORT", ["prometheus", "json", "noop"]),
}

TERMINAL = {
    "succeeded",
    "partially_succeeded",
    "failed",
    "aborted_budget",
    "cancelled",
}


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def http(method: str, url: str, body=None, token=None, timeout=10.0):
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


class Server:
    def __init__(self, exe: Path, env_extra: dict[str, str]):
        self.exe = exe
        self.port = free_port()
        self.dir = Path(tempfile.mkdtemp(prefix="sa-swap-"))
        self.log = self.dir / "server.log"
        self.env = {
            **os.environ,
            "SA_BIND": f"127.0.0.1:{self.port}",
            "SA_DATA_DIR": str(self.dir / "data"),
            "SA_ENV": "test",
            "SA_ALLOW_REGISTRATION": "true",
            "SA_FLUSH_ON_WRITE": "true",
            "SA_METRICS_TOKEN": "swap-test-token-0123456789abcdef",
            "RUST_LOG": "warn",
            **env_extra,
        }
        self.proc = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self.fh = self.log.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [str(self.exe)], env=self.env, stdout=self.fh, stderr=subprocess.STDOUT, cwd=str(self.dir)
        )
        deadline = time.time() + 25
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"进程提前退出 (code={self.proc.returncode})：{self.tail()}")
            try:
                code, _ = http("GET", self.base + "/readyz", timeout=1.5)
                if code == 200:
                    return self
            except Exception:
                time.sleep(0.15)
        raise RuntimeError("25s 内未就绪：" + self.tail())

    def __exit__(self, *a):
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(6)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        finally:
            try:
                self.fh.close()
            except Exception:
                pass
            shutil.rmtree(self.dir, ignore_errors=True)
        return False

    def tail(self, n=6) -> str:
        try:
            return " | ".join(self.log.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
        except Exception:
            return "<无日志>"


def bootstrap_token(base: str) -> str:
    """注册并登录首用户。首用户即 owner，具备 Admin 权限，才能读部件注册表。"""
    u = f"swap_{uuid.uuid4().hex[:10]}"
    code, _ = http(
        "POST",
        base + "/api/v1/auth/register",
        {"username": u, "email": f"{u}@example.com", "password": "SwapTest!2026"},
    )
    if code != 201:
        raise RuntimeError(f"注册失败 {code}")
    code, body = http("POST", base + "/api/v1/auth/login", {"account": u, "password": "SwapTest!2026"})
    if code != 200:
        raise RuntimeError(f"登录失败 {code}")
    return body["tokens"]["access_token"]


def read_components(base: str, token: str) -> dict[str, dict]:
    code, comps = http("GET", base + "/api/v1/observability/components", token=token)
    if code != 200:
        raise RuntimeError(f"components 端点 {code}")
    items = comps if isinstance(comps, list) else comps.get("items", [])
    return {c["slot"]: c for c in items}


def business_flow(base: str, token: str) -> tuple[bool, str]:
    """提交 → 查详情。返回 (是否通过, 说明)"""
    code, task = http(
        "POST",
        base + "/api/v1/tasks",
        {"goal": "帮我整理这周的会议要点并生成一份结构化摘要"},
        token=token,
    )
    if code not in (200, 201):
        return False, f"提交任务失败 {code} {str(task)[:70]}"
    # TaskDetail 用 serde(flatten) 平铺，顶层就是任务字段
    tid = task["id"]
    code, detail = http("GET", base + f"/api/v1/tasks/{tid}", token=token)
    if code != 200:
        return False, f"查详情失败 {code}"
    status = detail["status"]
    if status not in TERMINAL:
        return False, f"任务未到终态：{status}"
    return True, f"任务终态={status}"


def baseline(exe: Path) -> dict[str, str]:
    """不注入任何槽位变量，读出各槽位的真实默认实现。"""
    with Server(exe, {}) as s:
        token = bootstrap_token(s.base)
        by = read_components(s.base, token)
        ok, detail = business_flow(s.base, token)
        if not ok:
            raise RuntimeError(f"基线业务流就不通：{detail}")
        print("基线（不注入任何变量）：" + "  ".join(f"{k}={v['active']}" for k, v in sorted(by.items())))
        print(f"基线业务流：{detail}\n")
        return {k: v["active"] for k, v in by.items()}


def probe(exe: Path, slot: str, value: str, base_active: dict[str, str]) -> dict:
    _, env_var, _ = SLOTS[slot]
    env_extra = {env_var: value}
    if slot == "runner" and value == "process":
        env_extra["SA_RUNNER_DIR"] = str((Path(__file__).resolve().parent.parent / "workers").as_posix())

    r = {"slot": slot, "value": value, "ok": False, "detail": "", "health": ""}
    try:
        with Server(exe, env_extra) as s:
            token = bootstrap_token(s.base)
            by = read_components(s.base, token)
            if slot not in by:
                r["detail"] = f"注册表里没有 {slot} 槽位"
                return r
            r["health"] = by[slot]["health"]
            if by[slot]["active"] != value:
                r["detail"] = f"注册表说 active={by[slot]['active']}，注入的是 {value}"
                return r
            if by[slot]["health"] not in HEALTH_OK:
                r["detail"] = f"health={by[slot]['health']} 不在约定集合"
                return r
            # 其余槽位必须与基线一致：这是"换件不牵扯别处"的硬证据
            drift = [
                f"{k}:{by[k]['active']}≠基线{base_active[k]}"
                for k in SLOTS
                if k != slot and k in by and k in base_active and by[k]["active"] != base_active[k]
            ]
            if drift:
                r["detail"] = "其它槽位被牵连：" + "; ".join(drift)
                return r
            ok, detail = business_flow(s.base, token)
            r["ok"] = ok
            r["detail"] = detail if ok else f"业务流：{detail}"
    except Exception as e:  # noqa: BLE001
        r["detail"] = f"启动/探针异常：{e}"
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", required=True)
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    exe = Path(args.exe).resolve()
    if not exe.exists():
        print(f"[FAIL] 找不到 exe: {exe}")
        return 2

    base_active = baseline(exe)

    slots = [args.only] if args.only else list(SLOTS)
    rows: list[dict] = []
    print(f"换件实测：{len(slots)} 个槽位 / 共 {sum(len(SLOTS[s][2]) for s in slots)} 次独立启动\n")
    for slot in slots:
        for value in SLOTS[slot][2]:
            t0 = time.time()
            r = probe(exe, slot, value, base_active)
            r["secs"] = time.time() - t0
            rows.append(r)
            flag = "OK  " if r["ok"] else "FAIL"
            print(
                f"[{flag}] {slot:9s} = {value:11s} health={r['health']:15s} "
                f"{r['secs']:4.1f}s  {r['detail']}"
            )

    bad = [r for r in rows if not r["ok"]]
    print("\n" + "=" * 78)
    print(f"合计 {len(rows)} 次换件 | 通过 {len(rows) - len(bad)} | 失败 {len(bad)}")
    if bad:
        print("失败明细：")
        for r in bad:
            print(f"  - {r['slot']}={r['value']}: {r['detail']}")
    print("=" * 78)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
