#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_hallucination.py —— C25 防幻觉打点的端到端实测（真起进程，不看源码猜）。

为什么要有这个脚本：
  `sa_hallucination_flags_total` 原本只写在能力规格里（docs/02 C25 的 observability_signals），
  指标层却没有任何打点代码——这类"规格声明了、代码没实现"的情况，靠读代码是发现不了的，
  必须真起进程造一个"证据不全"的任务，再去 /metrics 里找那一行。

造什么场景：写 3 条超长共享记忆（合计远超 CONTEXT_BUDGET=6000），再提交命中它们的任务，
装配阶段必然触发"摘要优先、全文裁剪"→ ctx.truncated = true → 打点应 >= 1。

断言：
  H1 /metrics 里存在 sa_hallucination_flags_total
  H2 其值 >= 1（本次任务至少记了一处证据缺口）
  H3 同一任务的 sa_context_truncations_total 也 >= 1（截断确实发生，排除"巧合有值"）

用法：python scripts/probe_hallucination.py [--exe <path>]
退出码：0 = 三条断言全过；1 = 有失败
"""

import argparse
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import pathguard  # 清理一律过守卫：裸删除会把「参数少写一层」放大成删源码
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
try:
    from sync_build import resolve_build as _resolve_build
    DEFAULT_BUILD = _resolve_build()
except Exception:  # noqa: BLE001
    DEFAULT_BUILD = pathlib.Path(os.environ.get("LOCALAPPDATA", ".")) / "sa-build"

PASS = 0
FAIL = 0


def check(tag, name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("[OK  ] %-22s %s  %s" % (tag, name, detail))
    else:
        FAIL += 1
        print("[FAIL] %-22s %s  %s" % (tag, name, detail))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def call(base, method, path, body=None, token=None, timeout=30):
    data = None
    headers = {"Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def wait_ready(base, timeout=45):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            code, _ = call(base, "GET", "/healthz", timeout=3)
            if code == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.25)
    return False


def metric_value(text, name):
    """从 Prometheus 文本里取某指标（无 label）的累计值。"""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or not line:
            continue
        if line.startswith(name + " "):
            try:
                return float(line.split()[-1])
            except ValueError:
                return None
        if line.startswith(name + "{"):
            continue
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default=str(DEFAULT_BUILD / "target-gnu" / "debug" / "super-agents.exe"))
    ap.add_argument("--keep", action="store_true", help="保留临时目录便于排查")
    args = ap.parse_args()

    exe = pathlib.Path(args.exe)
    if not exe.exists():
        print("找不到可执行文件：%s" % exe)
        return 2

    port = free_port()
    base = "http://127.0.0.1:%d" % port
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="sa-halu-"))
    env = dict(os.environ)
    env.update({
        "SA_BIND": "127.0.0.1:%d" % port,
        "SA_DATA_DIR": str(tmp),
        # 显式 sim：默认后端改为 process 后，本门禁仍需确定性模拟（防幻觉打点与执行无关）
        "SA_RUNNER": "sim",
        "SA_JWT_SECRET": "probe-secret-key-0123456789abcdef-32+",
        "SA_ALLOW_REGISTRATION": "true",
        "SA_ENV": "test",
        "PYTHONIOENCODING": "utf-8",
    })
    proc = subprocess.Popen([str(exe)], env=env, cwd=str(tmp),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        if not wait_ready(base):
            check("H0", "服务就绪", False, "45s 内 /healthz 未就绪")
            return 1
        check("H0", "服务就绪", True, base)

        code, raw = call(base, "POST", "/api/v1/auth/register", {
            "username": "halu_boss", "email": "halu_boss@example.com", "password": "Pr0be!Passw0rd"})
        check("H0", "注册", code == 201, "rc=%d" % code)
        code, raw = call(base, "POST", "/api/v1/auth/login", {
            "account": "halu_boss", "password": "Pr0be!Passw0rd"})
        if code != 200:
            check("H0", "登录", False, "rc=%d %s" % (code, raw[:120]))
            return 1
        token = json.loads(raw)["tokens"]["access_token"]
        check("H0", "登录", True, "token 已取得")

        goal = "请写一份本周项目周报，包含进展、风险与下周计划"
        # 注意：MemoryWrite 的 content 上限是 8000 **字节**（.len() 按字节算），
        # 中文每字 3 字节，所以 100 次重复 ≈ 7200 字节，刚好卡在限内、又远超预算。
        filler = "以下为历史沉淀的长上下文内容，用于压缩装配预算。" * 100
        # 为什么是 5 条：assemble 只 take(5)，而每条 ~7300 字节按 4 字节/token 约 1800 tokens，
        # 单条远小于 CONTEXT_BUDGET=6000 —— 只有把 5 个候选位填满才会在第 4 条左右触发裁剪。
        # （第一版只写 3 条，跑出来"没截断"，不是代码没打点，而是注入量不够。）
        for i in range(5):
            code, raw = call(base, "POST", "/api/v1/memories", {
                "scope": "shared",
                "key": "周报沉淀-%d" % i,
                "content": goal + "｜" + filler,
                "tags": ["周报", "项目"],
                "confidence": 0.8,
            }, token=token)
            check("H1", "写入长记忆 %d" % i, code in (200, 201), "rc=%d" % code)

        code, raw = call(base, "POST", "/api/v1/tasks", {"mode": "sync", "goal": goal}, token=token)
        if code not in (200, 201):
            check("H1", "提交任务", False, "rc=%d %s" % (code, raw[:160]))
            return 1
        task = json.loads(raw)
        check("H1", "提交任务", True, "status=%s agreement=%s" % (task.get("status"), task.get("agreement_score")))

        code, metrics = call(base, "GET", "/metrics", token=token)
        if code != 200:
            check("H2", "抓取 /metrics", False, "rc=%d %s" % (code, metrics[:120]))
            return 1
        check("H2", "抓取 /metrics", True, "%d 字节" % len(metrics))

        flags = metric_value(metrics, "sa_hallucination_flags_total")
        check("H2", "sa_hallucination_flags_total 存在", flags is not None,
              "值=%s" % ("（缺失）" if flags is None else flags))
        check("H3", "打点值 >= 1", (flags or 0) >= 1, "值=%s" % flags)

        trunc = metric_value(metrics, "sa_context_truncations_total")
        check("H3", "同任务上下文确实被截断", (trunc or 0) >= 1,
              "sa_context_truncations_total=%s（排除巧合有值）" % trunc)

        # 口径说明：这是"证据缺口计数"，不是"AI 说了假话"的判定器。
        # 语义级判定在 guard-agent 的 hallucination-check 步；这里只保证缺口被记账。
        print("")
        print("说明：该指标统计执行链可客观判定的证据缺口（截断/失败步/低一致性），")
        print("      语义级幻觉判定属 guard-agent 复核职责，二者不混为一谈。")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if not args.keep:
            pathguard.safe_rmtree(tmp, base=pathlib.Path(tempfile.gettempdir()),
                                  allow_prefix="sa-halu-", what="幻觉探针临时目录",
                                  warn_only=True)

    print("")
    print("合计 %d 项 | 通过 %d | 失败 %d" % (PASS + FAIL, PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
