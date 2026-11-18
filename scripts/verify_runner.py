#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_runner.py —— 执行后端（Runner）可插拔性的真人实测脚本。

回答一个问题：把执行后端从「确定性模拟」换成「真实跑脚本」，业务代码一行不改，
任务是否真的按新后端执行、治理链路是否仍然生效？

实测 5 件事：
  R1  换后端只需环境变量（SA_RUNNER=process + SA_RUNNER_DIR），无需改代码
  R2  任务真的执行了外部进程：步骤输出里出现脚本自己生成的随机标记
  R3  治理没有被绕过：指标 backend 标签变 process，trace / token 记账照旧
  R4  缺脚本时是"可重试失败"而不是"假成功"
  R5  非法配置拒绝启动，不静默降级

用法：
  python scripts/verify_runner.py
  python scripts/verify_runner.py --exe <绝对路径>
"""

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

METRICS_TOKEN = "verify-metrics-token-0123456789abcdef"
# 与 src/domain/agent.rs 的 AGENTS 同步：collab-agent 已撤并进 doc-agent；
# 运维 agent 已并入 code-agent（task_types 当初与 code 完全重合，不满足立册条件）；
# 本轮新增 narrative / market / qa 三个垂直域，共 14 个。
AGENTS = [
    "super-agent", "code-agent", "doc-agent", "research-agent", "planner-agent",
    "ideation-agent", "game-agent", "finance-agent", "legal-agent", "data-agent",
    "narrative-agent", "market-agent", "qa-agent", "guard-agent",
]
RESULTS = []


def call(base, method, path, body=None, token=None, raw=False):
    url = base + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            text = resp.read().decode("utf-8", "replace")
            code = resp.status
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        code = e.code
    except Exception as e:
        return 0, f"连接失败: {e}"
    if raw:
        return code, text
    try:
        return code, json.loads(text)
    except Exception:
        return code, text


def rec(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("[%s] %s%s" % ("OK  " if ok else "FAIL", name, ("  -> " + str(detail)) if detail else ""))


def expect(name, got, want, detail=""):
    rec(name, got in want, detail or ("实际=%s" % got))


def wait_ready(base, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            code, body = call(base, "GET", "/readyz")
            if code == 200:
                return True, body
        except Exception:
            pass
        time.sleep(0.25)
    return False, None


def write_scripts(dirpath, marker, agents):
    """为每个 agent 写一个真实可执行的 .py，输出带同一枚随机标记。

    用 .py 而不是 .cmd：process 后端默认只跑 python 脚本，shell 脚本必须显式
    设 SA_RUNNER_ALLOW_SHELL=1 才认（安全基线，见三方审查 H1）。
    """
    dirpath.mkdir(parents=True, exist_ok=True)
    body = (
        "import os\n"
        "print('" + marker + " agent=%s task=%s step=%s perm=%s' % (\n"
        "    os.environ.get('SA_AGENT_ID'), os.environ.get('SA_TASK_ID'),\n"
        "    os.environ.get('SA_STEP_INDEX'), os.environ.get('SA_PERMISSION')))\n"
    )
    for a in agents:
        (dirpath / (a + ".py")).write_bytes(body.encode("utf-8"))


def start(exe, port, data_dir, runner, runner_dir):
    data_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["SA_DATA_DIR"] = str(data_dir)
    env["SA_BIND"] = "127.0.0.1:%d" % port
    env["SA_METRICS_TOKEN"] = METRICS_TOKEN
    env["SA_RUNNER"] = runner
    if runner_dir:
        env["SA_RUNNER_DIR"] = str(runner_dir)
    env["SA_SNAPSHOT_INTERVAL_SECS"] = "1"
    logf = open(str(data_dir) + ".log", "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen([exe], env=env, stdout=logf, stderr=subprocess.STDOUT)
    return proc, "http://127.0.0.1:%d" % port, logf


def stop(proc, logf):
    if proc:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    if logf:
        logf.close()


def bootstrap(base):
    stamp = uuid.uuid4().hex[:8]
    code, body = call(
        base, "POST", "/api/v1/auth/register",
        {"email": "runner%s@example.com" % stamp, "username": "runner%s" % stamp,
         "password": "Str0ng-Passw0rd!"},
    )
    if code not in (200, 201) or not isinstance(body, dict):
        return None, "注册失败 %s %s" % (code, str(body)[:160])
    token = body.get("access_token")
    if not token and isinstance(body.get("tokens"), dict):
        token = body["tokens"].get("access_token")
    if not token:
        return None, "注册响应无 access_token: %s" % str(body)[:200]
    call(base, "POST", "/api/v1/onboarding/skip", {}, token=token)
    return token, None


def submit(base, token):
    return call(
        base, "POST", "/api/v1/tasks",
        {"mode": "sync", "goal": "写一个把冒烟测试跑通的脚本并核对返回码", "task_type": "coding",
         "model_profile": "fast", "budget_tokens": 30000},
        token=token,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default="")
    ap.add_argument("--port", type=int, default=18110)
    args = ap.parse_args()

    exe = args.exe or str(
        pathlib.Path(os.environ.get("LOCALAPPDATA", ".")) / "sa-build" / "target" / "debug" / "super-agents.exe"
    )
    if not pathlib.Path(exe).exists():
        print("找不到可执行文件: %s" % exe)
        return 2

    root = pathlib.Path(tempfile.mkdtemp(prefix="sa-runner-verify-"))
    scripts = root / "scripts"
    marker = "RUNNER-REAL-" + uuid.uuid4().hex[:10].upper()
    write_scripts(scripts, marker, AGENTS)

    # ---------- R1~R3 ----------
    proc = logf = None
    try:
        proc, base, logf = start(exe, args.port, root / "data", "process", scripts)
        ok, ready = wait_ready(base)
        rec("R1a 以 process 执行后端启动并就绪", ok, "/readyz %s" % ready)
        if not ok:
            return 1

        token, err = bootstrap(base)
        rec("R1b 账号初始化", bool(token), err or "")
        if not token:
            return 1

        code, task = submit(base, token)
        expect("R2a 提交任务 201", code, [201], "实际=%s %s" % (code, str(task)[:150]))
        task = task if isinstance(task, dict) else {}
        results = task.get("results") or []
        outputs = " ".join(str(r.get("output") or "") for r in results)
        rec("R2b 步骤输出含脚本随机标记（证明真跑了外部进程）", marker in outputs,
            "标记=%s 命中=%s" % (marker, marker in outputs))
        rec("R2c 全部步骤成功且回填了 agent/step 环境变量",
            bool(results) and all(r.get("success") for r in results) and "agent=" in outputs,
            "步骤数=%d 成功=%d" % (len(results), sum(1 for r in results if r.get("success"))))
        rec("R2d 任务状态 succeeded", task.get("status") == "succeeded", "实际=%s" % task.get("status"))

        code, metrics = call(base, "GET", "/metrics", raw=True, token=METRICS_TOKEN)
        expect("R3a 指标可抓取 200", code, [200], "实际=%s" % code)
        metrics = metrics if isinstance(metrics, str) else ""
        rec('R3b 指标 backend 标签为 process', 'backend="process"' in metrics,
            '含 backend="process"' if 'backend="process"' in metrics else "未出现")
        rec("R3c 治理指标照旧上报",
            "sa_tasks_started_total" in metrics and "sa_model_profile_used_total" in metrics, "")

        code, trace = call(base, "GET", "/api/v1/tasks/%s/trace" % task.get("id"), token=token)
        trace = trace if isinstance(trace, list) else []
        rec("R3d trace 仍按阶段落账且 token>0",
            any(e.get("phase") == "execute" for e in trace) and sum(e.get("tokens", 0) for e in trace) > 0,
            "条目=%d tokens=%d" % (len(trace), sum(e.get("tokens", 0) for e in trace)))
    finally:
        stop(proc, logf)

    # ---------- R4：缺脚本 ----------
    partial = root / "scripts_partial"
    partial.mkdir(parents=True, exist_ok=True)
    (partial / "code-agent.py").write_bytes(b"pass\n")
    proc = logf = None
    try:
        proc, base, logf = start(exe, args.port + 1, root / "data2", "process", partial)
        ok, _ = wait_ready(base)
        if not ok:
            rec("R4 服务启动", False, "未就绪")
            return 1
        token, err = bootstrap(base)
        if not token:
            rec("R4 账号初始化", False, err)
            return 1
        code, task = submit(base, token)
        task = task if isinstance(task, dict) else {}
        results = task.get("results") or []
        rec("R4a 缺脚本的步骤被判失败（不假成功）",
            any(r.get("success") is False for r in results),
            "失败步骤=%d" % sum(1 for r in results if r.get("success") is False))
        errs = " ".join(str(r.get("error") or "") for r in results)
        rec("R4b 失败原因写明脚本缺失/可重试", ("脚本" in errs) or ("未找到" in errs), errs[:140])
        rec("R4c 任务整体不是 succeeded", task.get("status") != "succeeded", "实际=%s" % task.get("status"))
    finally:
        stop(proc, logf)

    # ---------- R5：非法配置拒启 ----------
    base_env = {**os.environ, "SA_METRICS_TOKEN": METRICS_TOKEN}
    # 语义按 H2 更新：不设 SA_RUNNER_DIR 不再等于错误——默认 process 会沿
    # cwd/workers → exe 同目录 workers 自动发现随产物分发的脚本目录。
    # 真正该拒启的是"给了目录但目录不可用"，否则就是半启动状态。
    e1 = dict(base_env, SA_DATA_DIR=str(root / "data3"), SA_BIND="127.0.0.1:18112",
              SA_RUNNER="process", SA_RUNNER_DIR=str(root / "no-such-scripts"))
    r1 = subprocess.run([exe], env=e1, capture_output=True, text=True,
                        # 显式 encoding：Windows 不给时按 GBK 解码 exe 的 stderr，
                        # 中文启动报错（"脚本目录不可用"）里的非 GBK 字节会让读线程
                        # 抛 UnicodeDecodeError、把 stderr 丢空。这里只判退出码所以不
                        # 影响结论，但诊断文本一丢，红了也说不清红在哪。
                        encoding="utf-8", errors="replace", timeout=30)
    rec("R5a 脚本目录不存在时拒绝启动", r1.returncode != 0, "退出码=%s" % r1.returncode)

    # R5a2 缺省即真实执行：不配 SA_RUNNER / SA_RUNNER_DIR，服务必须能起来且自报
    # runner=process（脚本目录自动发现），否则"默认做真事"等于"默认起不来"。
    e1b = {k: v for k, v in os.environ.items() if k not in ("SA_RUNNER", "SA_RUNNER_DIR")}
    e1b.update({"SA_METRICS_TOKEN": METRICS_TOKEN, "SA_DATA_DIR": str(root / "data3b"),
                "SA_BIND": "127.0.0.1:18115"})
    logf3 = open(str(root / "default.log"), "w", encoding="utf-8")
    proc3 = subprocess.Popen([exe], env=e1b, stdout=logf3, stderr=subprocess.STDOUT)
    try:
        ok3, _ = wait_ready("http://127.0.0.1:18115", timeout=40)
        logf3.flush()
        seen = (root / "default.log").read_text(encoding="utf-8", errors="replace")
        # 日志带 ANSI 着色，直接找 "runner=process" 会被转义码切断，命中恒为 False。
        plain = re.sub(r"\x1b\[[0-9;]*m", "", seen)
        rec("R5a2 缺省配置即真实执行且脚本目录自动发现",
            ok3 and "runner=process" in plain,
            "就绪=%s runner=process命中=%s" % (ok3, "runner=process" in plain))
    finally:
        stop(proc3, logf3)
    e2 = dict(base_env, SA_DATA_DIR=str(root / "data4"), SA_BIND="127.0.0.1:18113", SA_RUNNER="not-a-runner")
    r2 = subprocess.run([exe], env=e2, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=30)
    rec("R5b 非法 SA_RUNNER 值被拒（不是忽略）", r2.returncode != 0, "退出码=%s" % r2.returncode)

    total = len(RESULTS)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("-" * 62)
    print("合计 %d 项 | 通过 %d | 失败 %d" % (total, passed, total - passed))
    print("临时目录: %s" % root)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
