#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""实时探针：验证 worker 真的把本地模型接进来了（端到端，不 mock）。

为什么要这个脚本
----------------
单测与冒烟都是"假 worker"，只能证明管线连通，证明不了"模型真的被调用"。
这个探针跑真模型，取真实的 token 数与真实耗时，并把裁决落在三条硬断言上：

  P1  worker 输出里出现模型名（证明 SA_LLM_MODEL 透传到了子进程）
  P2  该步骤 token 数 > 0 且不再等于规则实现的固定值（证明真的在算）
  P3  路由命中预期的 agent 且该步成功（证明接线对了，不是别的 agent 顶包）

踩坑备忘（都是真跑出来的，别踩第二遍）
--------------------------------------
1. 按 agent 覆盖单步超时的键名是 `SA_RUNNER_TIMEOUT_{AGENT_ID 大写, '-' 换 '_'}`，
   即 doc-agent → `SA_RUNNER_TIMEOUT_DOC_AGENT`。
   写成 `SA_RUNNER_TIMEOUT_DOC-AGENT` 后端**完全不认**，且不报错：
   长任务会每 20 秒被强杀一次、重试三次、最终 partially_succeeded。
2. `POST /api/v1/tasks` 是同步执行：请求会一直挂到任务跑完。
   接了模型意味着单步可能 100 秒以上，客户端超时必须给足，否则任务在后台照跑、
   客户端却先放弃（"假失败"）。
3. `GET /api/v1/tasks/{id}` 实际返回**平铺结构**（顶层就是 task 字段），
   与 openapi.json 里的 TaskDetail{task, trace, memory_refs} 不一致。
   本脚本两种形态都兼容，避免因契约漂移而误判。
4. trace 里的 `at` 是**写入时刻**（约等于该次尝试结束），不是开始时刻：
   `phase="sandbox", outcome="started"` 实际记的也是结束时间。
   要算耗时，请用相邻完成点做差，不要用同一次尝试内的 started/finished 配对。

用法
----
    python scripts/probe_llm_live.py --model minicpm5-2b:f16
    python scripts/probe_llm_live.py --base http://127.0.0.1:11434/v1 --model qwen2.5:7b --port 8171
    python scripts/probe_llm_live.py --keep-data     # 保留数据目录便于事后翻查

前置：Ollama 已启动且模型已拉取（ollama list 能看到），后端二进制已编译。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
GOAL = "写一份把 worker 接入本地小模型的试点报告，说明接入的模型、接线方式和实测数据"


# ---------- HTTP ----------

def call(base: str, method: str, path: str, body=None, token: str | None = None,
         timeout: float = 30.0):
    """单次 HTTP 调用；任何异常都转成 (0, {"_err": ...})，不让探针自己崩。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        return exc.code, {"_err": exc.read()[:400].decode("utf-8", "replace")}
    except Exception as exc:                                    # noqa: BLE001
        return 0, {"_err": f"{type(exc).__name__}: {exc}"}


def task_of(detail: dict) -> dict:
    """兼容两种详情结构：包一层 {task:...} 或平铺 {id, status, results...}。"""
    if isinstance(detail.get("task"), dict) and detail["task"]:
        merged = dict(detail["task"])
        merged.setdefault("trace", detail.get("trace", []))
        return merged
    return detail


def wait_ready(base: str, budget: float = 60.0) -> bool:
    deadline = time.time() + budget
    while time.time() < deadline:
        if call(base, "GET", "/readyz", timeout=3)[0] == 200:
            return True
        time.sleep(0.5)
    return False


# ---------- 数据与进程 ----------

def prepare_data(data_dir: Path, keep: bool) -> Path:
    """镜像 workers 到数据目录，让后端从镜像里执行脚本（不碰源码）。"""
    if data_dir.exists() and not keep:
        shutil.rmtree(data_dir, ignore_errors=True)
    runner = data_dir / "runner"
    runner.mkdir(parents=True, exist_ok=True)
    src = ROOT / "workers"
    if not src.is_dir():
        raise SystemExit(f"找不到 workers 目录：{src}")
    shutil.copytree(src, runner, dirs_exist_ok=True)
    return runner


def resolve_exe(explicit: str | None) -> Path:
    """按优先级找后端二进制：显式参数 → SA_EXE → 项目内常见构建目录。"""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        if path.exists():
            return path
        raise SystemExit(f"--exe 指向的文件不存在：{path}")

    env = os.environ.get("SA_EXE")
    if env and Path(env).exists():
        return Path(env)

    for rel in ("target-gnu/debug/super-agents.exe", "target/debug/super-agents.exe",
                "target-gnu/release/super-agents.exe", "target/release/super-agents.exe"):
        candidate = ROOT / rel
        if candidate.exists():
            return candidate

    raise SystemExit(
        "找不到 super-agents.exe。\n"
        f"  已查过：{ROOT / 'target-gnu/debug'}、{ROOT / 'target/debug'}\n"
        "  如果编译产物在别的目录（例如单独的构建工作区），两种办法：\n"
        "    python scripts/probe_llm_live.py --exe \"D:\\某处\\target-gnu\\debug\\super-agents.exe\"\n"
        "    set SA_EXE=D:\\某处\\target-gnu\\debug\\super-agents.exe\n"
        "  还没编译过就先 cargo build。"
    )


def launch(exe: Path, port: int, data_dir: Path, runner_dir: Path, env_extra: dict) -> subprocess.Popen:
    if not exe.exists():
        raise SystemExit(f"找不到 {exe}，先 cargo build")

    env = os.environ.copy()
    env.pop("SA_CONFIG", None)
    env.update({
        "SA_DATA_DIR": str(data_dir),
        "SA_BIND": f"127.0.0.1:{port}",
        "SA_METRICS_TOKEN": "t" * 32,
        "SA_RUNNER": "process",
        "SA_RUNNER_DIR": str(runner_dir),
    })
    env.update(env_extra)

    log = open(data_dir / "server.log", "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen([str(exe)], stdout=log, stderr=subprocess.STDOUT,
                            env=env, cwd=str(ROOT), close_fds=True)
    proc._sa_log = log                                          # type: ignore[attr-defined]
    return proc


def stop(proc, data_dir: Path):
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:                                           # noqa: BLE001
        try:
            proc.kill()
        except Exception:                                       # noqa: BLE001
            pass
    log = getattr(proc, "_sa_log", None)
    if log:
        log.close()
    print(f"  服务已停止，日志留在 {data_dir / 'server.log'}")


# ---------- 断言 ----------

def steps_of(task: dict) -> list[dict]:
    return task.get("results") or task.get("steps") or []


def judge(task: dict, model: str, expect_agent: str) -> list[dict]:
    checks = []

    def add(pid, ok, detail):
        checks.append({"id": pid, "ok": bool(ok), "detail": detail})

    hits = [s for s in steps_of(task) if s.get("agent_id") == expect_agent]
    add("P3", bool(hits) and all(s.get("success") for s in hits),
        f"路由到 {expect_agent}：命中 {len(hits)} 次，"
        f"成功 {sum(1 for s in hits if s.get('success'))} 次")

    target = hits[0] if hits else {}
    output = target.get("output") or ""
    add("P1", model in output, f"{expect_agent} 输出{'含' if model in output else '不含'}模型名 {model}")

    tokens = int(target.get("tokens") or 0)
    add("P2", tokens > 0, f"{expect_agent} tokens={tokens}（0 说明没走到模型）")

    return checks


def print_timeline(task: dict):
    """把 trace 还原成时间轴：at 是完成时刻，所以耗时＝与上一条的差。"""
    trace = sorted(task.get("trace") or [], key=lambda x: x.get("at") or "")
    if not trace:
        print("  （无 trace）")
        return
    print("\n  时间轴（相邻完成点做差，即上一步的真实耗时）：")
    prev = None
    for item in trace:
        stamp = item.get("at") or ""
        clock = stamp[11:23]
        delta = ""
        if prev:
            try:
                from datetime import datetime
                a = datetime.fromisoformat(prev.replace("Z", "+00:00"))
                b = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                delta = f"+{(b - a).total_seconds():8.3f}s"
            except Exception:                                   # noqa: BLE001
                delta = " " * 9
        prev = stamp
        print(f"    {clock} {delta}  step{item.get('step_index')} "
              f"{item.get('agent_id'):<12} {item.get('phase'):<8} {item.get('outcome'):<16} "
              f"tok={item.get('tokens')} {(item.get('detail') or '')[:44]}")


# ---------- 主流程 ----------

def main() -> int:
    ap = argparse.ArgumentParser(description="验证 worker 接入本地模型的端到端探针")
    ap.add_argument("--base", default="http://127.0.0.1:11434/v1", help="OpenAI 兼容地址")
    ap.add_argument("--model", default="minicpm5-2b:f16", help="模型名（须与 ollama list 一致）")
    ap.add_argument("--api-key", default="ollama", help="本地模型随便填")
    ap.add_argument("--port", type=int, default=8171, help="后端监听端口")
    ap.add_argument("--data", default="target/live-probe", help="数据目录（相对项目根）")
    ap.add_argument("--agent", default="doc-agent", help="期望命中的 agent")
    ap.add_argument("--llm-timeout", type=int, default=300, help="worker 等模型的秒数")
    ap.add_argument("--step-timeout", type=int, default=300, help="该 agent 单步超时秒数")
    ap.add_argument("--task-timeout", type=int, default=600, help="POST /tasks 客户端超时")
    ap.add_argument("--exe", default=None,
                    help="后端二进制路径；缺省按 target-gnu/debug → target/debug 顺序探测")
    ap.add_argument("--keep-data", action="store_true", help="保留数据目录")
    args = ap.parse_args()

    data_dir = (ROOT / args.data).resolve()
    base = f"http://127.0.0.1:{args.port}"
    runner = prepare_data(data_dir, args.keep_data)

    # 键名规则：agent id 大写且 '-' 换 '_'
    override_key = "SA_RUNNER_TIMEOUT_" + args.agent.replace("-", "_").upper()
    env_extra = {
        "SA_LLM_BASE": args.base,
        "SA_LLM_MODEL": args.model,
        "SA_LLM_KEY": args.api_key,
        "SA_LLM_TIMEOUT": str(args.llm_timeout),
        "SA_LLM_MAX_TOKENS": "1600",
        override_key: str(args.step_timeout),
    }

    exe = resolve_exe(args.exe)
    print(f"[1/6] 启动后端 {base}（二进制 {exe.name}，数据目录 {data_dir}）")
    proc = launch(exe, args.port, data_dir, runner, env_extra)
    try:
        if not wait_ready(base):
            print("  /readyz 未就绪，看日志：", data_dir / "server.log")
            return 2
        print("      /readyz OK")

        print("[2/6] 注册临时 owner 账号")
        # 注意：注册字段是 username，登录字段是 account —— 接口两端命名不一致，
        # 照着 openapi 抄容易两边都写错，这里两个字段各按各的来。
        status, body = call(base, "POST", "/api/v1/auth/register", {
            "username": "probe_owner", "password": "Pr0be!Passw0rd",
            "email": "probe@example.com", "display_name": "Probe",
        }, timeout=30)
        if status not in (200, 201) and status != 409:
            print("      注册失败：", status, body)
            return 2
        if status == 409:
            status, body = call(base, "POST", "/api/v1/auth/login", {
                "account": "probe_owner", "password": "Pr0be!Passw0rd"}, timeout=30)
        token = (body.get("tokens") or {}).get("access_token")
        if not token:
            print("      没拿到令牌：", body)
            return 2

        print(f"[3/6] 提交任务（同步等待，最多 {args.task_timeout}s）")
        started = time.time()
        status, submitted = call(base, "POST", "/api/v1/tasks",
                                 {"mode": "sync", "goal": GOAL, "task_type": "writing"},
                                 token=token, timeout=args.task_timeout)
        elapsed = time.time() - started
        print(f"      POST 返回 {status}，耗时 {elapsed:.1f}s")

        print("[4/6] 取任务详情与轨迹")
        task_id = (submitted or {}).get("id")
        if not task_id:
            status, listing = call(base, "GET", "/api/v1/tasks?limit=1", token=token, timeout=30)
            items = (listing or {}).get("items") or []
            task_id = items[0]["id"] if items else None
        if not task_id:
            print("      拿不到任务 id，终止")
            return 2
        status, detail = call(base, "GET", f"/api/v1/tasks/{task_id}", token=token, timeout=30)
        task = task_of(detail)

        print(f"      状态={task.get('status')} tokens={task.get('tokens_used')} "
              f"步数={len(steps_of(task))}")
        for step in steps_of(task):
            print(f"      - {step.get('agent_id'):<12} success={step.get('success')} "
                  f"attempts={step.get('attempts')} tokens={step.get('tokens')} "
                  f"err={(step.get('error') or '')[:60]}")
            print(f"        out: {(step.get('output') or '')[:160].replace(chr(10), ' | ')}")
        print_timeline(task)

        print("[5/6] 断言")
        checks = judge(task, args.model, args.agent)
        for check in checks:
            print(f"      [{check['id']}] {'PASS' if check['ok'] else 'FAIL'}  {check['detail']}")

        print("[6/6] 取证")
        evidence = data_dir / "evidence.json"
        evidence.write_text(json.dumps({
            "model": args.model, "base": args.base, "elapsed_s": round(elapsed, 1),
            "checks": checks, "task": task,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print("      ", evidence)

        ok = task.get("status") == "succeeded" and all(c["ok"] for c in checks)
        print("\n结论：", "全绿，worker 确实跑在本地模型上" if ok else "未全绿，按 FAIL 项排查")
        return 0 if ok else 1
    finally:
        stop(proc, data_dir)


if __name__ == "__main__":
    sys.exit(main())
