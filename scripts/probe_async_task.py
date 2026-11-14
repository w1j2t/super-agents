#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_async_task.py —— 提交任务那段"同步阻塞 vs 异步受理"的行为探针。

为什么要它
----------
`POST /tasks` 支持 `mode=sync`（201，跑完才返回）与 `mode=async`（202，受理即返回）。
两条分支的差异不是"风格问题"，而是**客户端会不会误判失败**：
真实模型下同步实测 129.9 秒，而多数 HTTP 客户端默认 30 秒超时——
任务在服务端跑得好好的，客户端却抛超时并把它当成失败。

这条探针只做一件事：**真起 exe、真发请求，把两条分支的时序与状态流摊开量一遍**。
它不改任何东西，所以既能当诊断工具，也能被门禁直接调用。

覆盖七件事（每条都对应一种会真伤到前端的形态）
  A1 缺省 mode 的实际行为           —— 契约说 async，代码若给 sync，前端就挂住
  A2 mode=async 是否"受理即返回"     —— 202 却要等 12 秒，等于没异步
  A3 异步任务最终能否收敛到终态       —— 只受理不执行，是最难发现的一类空壳
  A4 SSE 有没有中间态（queued→running→终态）
                                    —— 只有首尾两帧，时间轴就退化成两个端点
  A5 并发上限被顶到时是否 429，且不留"永远不会动的"幽灵任务
  A6 异步任务能否被取消并收敛为 cancelled
  A7 断点续传（Last-Event-ID）      —— 断线期间漏掉的帧补不回来，前端时间轴就永久缺格；
                                        且要区分"补到帧"与"无事可补时主动 EOF"两种合法形态

用法：
    python scripts/probe_async_task.py                       # 自起 exe（默认 build_sa1 产物）
    python scripts/probe_async_task.py --base http://127.0.0.1:18080   # 对已起实例跑
    python scripts/probe_async_task.py --keep                # 跑完不删数据目录，留着查日志
退出码：0 = 七项全过；1 = 有失败项（含"取不到证据"）。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_EXE = pathlib.Path(r"C:\Users\94355\Desktop\build_sa1\target-gnu\debug\super-agents.exe")

results: list[tuple[str, bool, str]] = []

# /metrics 抓取令牌（与启动 env 里的一致，A7 靠它读续传指标）
METRICS_TOKEN = "async-probe-metrics-token-0123456789"



def rec(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  ->  {detail}" if detail else ""), flush=True)


def note(line: str) -> None:
    print(f"       {line}", flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Client:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.last_headers: dict[str, str] = {}

    def req(self, method: str, path: str, body=None, token: str | None = None, timeout=60):
        """返回 (status, parsed_json_or_text, elapsed_seconds)。"""
        url = self.base + path
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        r = urllib.request.Request(url, data=data, headers=headers, method=method)
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                self.last_headers = {k.lower(): v for k, v in resp.headers.items()}
                raw = resp.read().decode("utf-8", "replace")
                el = time.perf_counter() - t0
                try:
                    return resp.status, json.loads(raw) if raw else {}, el
                except json.JSONDecodeError:
                    return resp.status, raw, el
        except urllib.error.HTTPError as e:
            self.last_headers = {k.lower(): v for k, v in e.headers.items()}
            raw = e.read().decode("utf-8", "replace")
            el = time.perf_counter() - t0
            try:
                return e.code, json.loads(raw) if raw else {}, el
            except json.JSONDecodeError:
                return e.code, raw, el


# --------------------------------------------------------------------------- #
# SSE：只读前若干帧，够看清"有没有中间态"就退出
# --------------------------------------------------------------------------- #
def read_sse(base: str, path: str, token: str, max_events=12, idle_timeout=25.0,
             last_id=None, with_ids=False):
    """返回 [(event_name, data_dict), ...]；任何异常都收敛成已有帧，不抛。

    last_id 非 None 时按断点续传连（带 Last-Event-ID 头）；with_ids=True 时每帧返回
    (id, event_name, data)，用于校验"续传从哪一帧接上"。
    """
    frames: list = []
    url = base.rstrip("/") + path
    headers = {
        "Accept": "text/event-stream",
        "Authorization": f"Bearer {token}",
    }
    if last_id is not None:
        headers["Last-Event-ID"] = str(last_id)
    req = urllib.request.Request(url, headers=headers)
    deadline = time.time() + idle_timeout
    try:
        with urllib.request.urlopen(req, timeout=idle_timeout) as resp:
            name = "message"
            cur_id = None
            buf: list[str] = []
            while len(frames) < max_events and time.time() < deadline:
                line = resp.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip("\r\n")
                if text.startswith("id:"):
                    cur_id = text[3:].strip()
                elif text.startswith("event:"):
                    name = text[6:].strip()
                elif text.startswith("data:"):
                    buf.append(text[5:].strip())
                elif text == "":
                    if buf:
                        try:
                            data = json.loads("\n".join(buf))
                        except json.JSONDecodeError:
                            data = {}
                        frames.append((cur_id, name, data) if with_ids else (name, data))
                        buf = []
                    name = "message"
                    cur_id = None
    except Exception as e:  # 读流出任何岔子都只影响本项，不拖垮整条探针
        note(f"SSE 读取提前结束：{type(e).__name__}: {e}")
    return frames


def metric_total(base: str, tok: str, name: str):
    """读 /metrics 里某个计数器的当前累计值（多行带标签时求和）；拿不到返回 None。

    判"续传到底走没走 replay 分支"靠帧数不可靠：客户端断线期间任务若已收尾，
    replay 会返回空集，服务端**主动 EOF**（不白挂一条永不更新的连接）——那是设计。
    所以真正的判据在指标上：走续传分支必然 `sa_sse_replay_total` +1。
    """
    req = urllib.request.Request(base.rstrip("/") + "/metrics",
                                 headers={"Authorization": f"Bearer {tok}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", "replace")
    except Exception as e:
        note(f"读 /metrics 失败：{type(e).__name__}: {e}")
        return None
    total = 0.0
    seen = False
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        head = line.split("{")[0].split(" ")[0]
        if head == name:
            try:
                total += float(line.rsplit(" ", 1)[1])
                seen = True
            except (IndexError, ValueError):
                continue
    return total if seen else None


def wait_terminal(c: Client, tid: str, token: str, timeout=40.0):
    """轮询到终态，返回 (status, 轮询次数, 耗时)。"""
    terminal = {"succeeded", "failed", "cancelled", "partially_succeeded", "aborted_budget"}
    t0 = time.perf_counter()
    last = None
    n = 0
    while time.perf_counter() - t0 < timeout:
        st, b, _ = c.req("GET", f"/api/v1/tasks/{tid}", token=token, timeout=10)
        n += 1
        last = (b or {}).get("status") if isinstance(b, dict) else None
        if last in terminal:
            return last, n, time.perf_counter() - t0
        time.sleep(0.2)
    return last, n, time.perf_counter() - t0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default=str(DEFAULT_EXE))
    ap.add_argument("--base", default="")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--slow-ms", type=int, default=800, help="slow-step 每步睡多久（夹在 0..5000）")
    ap.add_argument("--keep", action="store_true", help="兼容旧参数；本脚本本就不自动清理")
    args = ap.parse_args()

    proc = None
    data_dir = None
    logf = None
    if args.base:
        base = args.base
    else:
        exe = pathlib.Path(args.exe)
        if not exe.exists():
            print(f"[FAIL] 找不到可执行文件: {exe}（用 --exe 指定）")
            return 1
        port = args.port or free_port()
        data_dir = pathlib.Path(tempfile.mkdtemp(prefix="sa-async-probe-"))
        log_path = pathlib.Path(str(data_dir) + "-server.log")
        logf = open(log_path, "w", encoding="utf-8", errors="replace")
        env = dict(os.environ)
        env.update({
            "SA_DATA_DIR": str(data_dir),
            "SA_BIND": f"127.0.0.1:{port}",
            "SA_ENV": "dev",
            "SA_JWT_SECRET": "async-probe-secret-0123456789abcdef0123456789",
            "SA_METRICS_TOKEN": METRICS_TOKEN,
            "SA_ALLOW_REGISTRATION": "true",
            "SA_STORAGE": "json",
            "SA_RUNNER": "sim",
            "SA_AUDIT": "file",
            "SA_TASK_SUBMIT_QUOTA_PER_HOUR": "900",
            "SA_SIM_SLOW_MS": str(max(0, min(5000, args.slow_ms))),
            "SA_ASYNC_MAX_RUNNING": "3",   # 压小一点，A5 才不用起九个任务
        })
        for k in [k for k in env if k.startswith("SA_LLM_")]:
            env.pop(k)  # 探针绝不能因为开发机上有真模型配置就变成分钟级
        proc = subprocess.Popen([str(exe)], env=env, stdout=logf, stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        base = f"http://127.0.0.1:{port}"
        note(f"数据目录 {data_dir}")
        note(f"服务日志 {log_path}")
    note(f"服务地址 {base}")
    note(f"slow-step 每步 {args.slow_ms}ms")

    c = Client(base)
    try:
        ready = False
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 60:
            st, b, _ = c.req("GET", "/readyz", timeout=5)
            if st == 200 and isinstance(b, dict) and b.get("ready"):
                ready = True
                break
            if proc is not None and proc.poll() is not None:
                break
            time.sleep(0.3)
        rec("A0 服务就绪（/readyz ready=true）", ready, "" if ready else "60 秒未就绪")
        if not ready:
            if logf:
                logf.close()
                tail = pathlib.Path(str(data_dir) + "-server.log")
                if tail.exists():
                    print(tail.read_text(encoding="utf-8", errors="replace")[-1500:])
            return 1

        stamp = uuid.uuid4().hex[:8]
        uname = f"ap_{stamp}"
        st, reg, _ = c.req("POST", "/api/v1/auth/register", {
            "username": uname, "email": f"{uname}@example.test", "password": "Async-Probe-Passw0rd!",
        })
        tokens = (reg or {}).get("tokens") if isinstance(reg, dict) else None
        token = (tokens or {}).get("access_token") if isinstance(tokens, dict) else None
        rec("A0b 注册并拿到令牌", st == 201 and bool(token), f"status={st}")
        if not token:
            return 1

        goal_slow = "slow-step 用于测异步受理与状态流的任务"

        # ---- A1 缺省 mode 必须是 async（契约声明，也是唯一不会让客户端超时的选择） ----
        st, b, el = c.req("POST", "/api/v1/tasks", {
            "goal": goal_slow, "task_type": "coding", "model_profile": "fast",
            "budget_tokens": 20000,
        }, token=token, timeout=180)
        b = b if isinstance(b, dict) else {}
        default_status = b.get("status")
        hdr_mode = c.last_headers.get("x-sa-exec-mode", "")
        note(f"A1 缺省提交：status={st} 耗时={el:.2f}s 返回状态={default_status} 分支头={hdr_mode or '(缺)'}")
        rec("A1 缺省提交即异步受理（202 + queued，不等执行完）",
            st == 202 and default_status == "queued",
            f"status={st} 返回状态={default_status} 耗时={el:.2f}s"
            + ("；201 + 终态说明缺省仍是 sync，慢模型下客户端会先超时" if st == 201 else ""))
        rec("A1b 缺省提交的耗时远小于执行耗时（真的没在等）", el < 1.0,
            f"受理耗时={el:.2f}s（一步 {args.slow_ms}ms）")
        rec("A1c 响应头 X-SA-Exec-Mode 自证走了 async 分支", hdr_mode == "async",
            f"X-SA-Exec-Mode={hdr_mode or '(缺失)'}")
        loc = c.last_headers.get("location", "")
        rec("A1d 202 带 Location 指向该任务资源（RFC 9110 对 202 的要求）",
            bool(b.get("id")) and loc.endswith(f"/tasks/{b.get('id')}"),
            f"Location={loc or '(缺失)'} task_id={b.get('id')}")
        if st == 202 and b.get("id"):
            wait_terminal(c, b["id"], token, timeout=60)

        # ---- A1e 显式 mode=sync 仍必须是 201 + 终态（同步能力没被削掉） ----
        st, b, el = c.req("POST", "/api/v1/tasks", {
            "goal": "显式同步模式的短任务", "task_type": "coding", "model_profile": "fast",
            "budget_tokens": 20000, "mode": "sync",
        }, token=token, timeout=180)
        b = b if isinstance(b, dict) else {}
        sync_hdr = c.last_headers.get("x-sa-exec-mode", "")
        rec("A1e 显式 mode=sync 仍返回 201 + 终态", st == 201 and b.get("status") == "succeeded",
            f"status={st} 返回状态={b.get('status')}")
        rec("A1f 显式 sync 的分支头为 sync", sync_hdr == "sync",
            f"X-SA-Exec-Mode={sync_hdr or '(缺失)'}")

        # ---- A2 显式 mode=async 也必须"受理即返回" ----
        st, b, el = c.req("POST", "/api/v1/tasks", {
            "goal": goal_slow, "task_type": "coding", "model_profile": "fast",
            "budget_tokens": 20000, "mode": "async",
        }, token=token, timeout=180)
        b = b if isinstance(b, dict) else {}
        aid = b.get("id")
        # 一步 800ms、四步就是 3 秒出头；受理若也要 3 秒，说明它其实在等
        rec("A2 mode=async 返回 202 且状态 queued", st == 202 and b.get("status") == "queued",
            f"status={st} 返回状态={b.get('status')} 耗时={el:.2f}s")
        rec("A2b 受理耗时远小于执行耗时（真的没在等）", el < 1.0,
            f"受理耗时={el:.2f}s（一步 {args.slow_ms}ms，四步约 {args.slow_ms*4/1000:.1f}s）")
        if aid:
            # ---- A3 异步任务真会跑到终态 ----
            final, polls, took = wait_terminal(c, aid, token, timeout=60)
            rec("A3 异步任务最终收敛到 succeeded", final == "succeeded",
                f"终态={final} 轮询={polls} 次 耗时={took:.2f}s")
        else:
            rec("A3 异步任务最终收敛到 succeeded", False, "拿不到任务 id，无法追踪")

        # ---- A4 SSE 是否给了中间态 ----
        st, b, el = c.req("POST", "/api/v1/tasks", {
            "goal": goal_slow, "task_type": "coding", "model_profile": "fast",
            "budget_tokens": 20000, "mode": "async",
        }, token=token, timeout=60)
        sid = (b or {}).get("id") if isinstance(b, dict) else None
        if sid:
            frames = read_sse(base, f"/api/v1/tasks/{sid}/events", token, max_events=12, idle_timeout=30)
            seqs = [f[1].get("seq") for f in frames if isinstance(f[1], dict) and "seq" in f[1]]
            statuses = [f[1].get("status") for f in frames if isinstance(f[1], dict)]
            running_seen = "running" in statuses
            monotonic = seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
            rec("A4 事件流收到帧", len(frames) >= 2, f"帧数={len(frames)} 事件名={sorted({f[0] for f in frames})}")
            rec("A4b 事件流出现 running 中间态（不是只有首尾两帧）", running_seen,
                f"状态序列={statuses[:8]}")
            rec("A4c seq 严格递增且不重复", monotonic, f"seq 序列={seqs[:8]}")
        else:
            rec("A4 事件流收到帧", False, "拿不到任务 id，无法订阅")

        # ---- A5 并发上限：429 且不留幽灵 ----
        lim = 3  # 与启动时的 SA_ASYNC_MAX_RUNNING 一致
        submitted, rejected = [], []
        for i in range(lim + 3):
            st, b, el = c.req("POST", "/api/v1/tasks", {
                "goal": f"slow-step 并发上限测试 {i}", "task_type": "coding",
                "model_profile": "fast", "budget_tokens": 20000, "mode": "async",
            }, token=token, timeout=60)
            tid = (b or {}).get("id") if isinstance(b, dict) else None
            if st == 202:
                submitted.append(tid)
            elif st == 429:
                rejected.append((st, b.get("message") if isinstance(b, dict) else ""))
            else:
                rejected.append((st, f"意外状态码 tid={tid}"))
        rec("A5 顶到并发上限时返回 429", len(rejected) >= 1 and all(s == 429 for s, _ in rejected),
            f"受理 {len(submitted)} 条 / 被拒 {len(rejected)} 条（上限 {lim}）：{[s for s, _ in rejected]}")
        if rejected:
            note(f"429 文案：{rejected[0][1]}")
        # 被拒的那几条绝不能留下 queued 幽灵
        ghost = []
        st, lb, _ = c.req("GET", "/api/v1/tasks?limit=100", token=token, timeout=10)
        items = (lb or {}).get("items", []) if isinstance(lb, dict) else []
        for it in items:
            if str(it.get("goal", "")).startswith("slow-step 并发上限测试") and it.get("status") == "queued":
                ghost.append(it.get("id"))
        rec("A5b 被拒的提交没有留下永远停在 queued 的幽灵任务", not ghost,
            f"疑似幽灵 {len(ghost)} 条")

        # 等这批跑完，别把后面的取消项挤掉
        for tid in submitted:
            if tid:
                wait_terminal(c, tid, token, timeout=40)

        # ---- A6 异步任务能被取消 ----
        st, b, el = c.req("POST", "/api/v1/tasks", {
            "goal": goal_slow, "task_type": "coding", "model_profile": "fast",
            "budget_tokens": 20000, "mode": "async",
        }, token=token, timeout=60)
        cid = (b or {}).get("id") if isinstance(b, dict) else None
        if cid:
            st2, b2, _ = c.req("POST", f"/api/v1/tasks/{cid}/cancel", token=token, timeout=30)
            rec("A6 取消异步任务返回 202（立旗，不假装已完成）", st2 == 202, f"status={st2}")
            final, _, took = wait_terminal(c, cid, token, timeout=40)
            rec("A6b 取消后收敛为 cancelled", final == "cancelled", f"终态={final} 耗时={took:.2f}s")
        else:
            rec("A6 取消异步任务返回 202（立旗，不假装已完成）", False, "拿不到任务 id")

        # ---- A7 断点续传：Last-Event-ID 只补后续事件 ----
        # 前 6 项证"流是活的"，证不了"断了还能接回来"。前端时间轴一旦断线，
        # 没有续传就只能重拉 detail 补齐——那是另一套逻辑，且中间的 running 帧永久丢失。
        #
        # 判据踩过两次坑，都记在这里，免得后人再走一遍：
        # ① 最初断言"续传必须收到帧"，偶发变红。读源码才明白那不是 bug——断线期间若无事可补，
        #    服务端会按设计主动 EOF（不白挂一条永不更新的连接）。于是改用指标判"走没走续传分支"（A7）。
        # ② 但 A7 只证"进了续传分支"，证不了"真的补到了断线之后的事件"。要守这件事，
        #    锚点就必须落在**非终态帧**上；而 fast-sim 一条任务只有 2~3 帧，首连第 2 帧
        #    有约两成概率正好是 succeeded —— 此时"断线之后已无事件"，服务端 0 帧 EOF 完全正确，
        #    断言"必须有帧"就会误报（实测 5 轮对照实验里出现 1 次）。
        #    治法不是放宽断言，而是**把前置条件构造出来**：锚点落在终态帧就换个新任务重来。
        #    与此同时，把"锚点=终态帧 → 秒回 EOF"单独立为 A7g 正向断言，
        #    让原来那条误报路径也变成被守护的语义，而不是被绕过去。
        def _submit_slow():
            st_, b_, _ = c.req("POST", "/api/v1/tasks", {
                "goal": goal_slow, "task_type": "coding", "model_profile": "fast",
                "budget_tokens": 20000, "mode": "async",
            }, token=token, timeout=60)
            return (b_ or {}).get("id") if isinstance(b_, dict) else None

        rid, anchor, last_id = None, None, None
        TERMINAL = ("succeeded", "failed", "cancelled")
        # ③ 第三次踩：原来只取 2 帧、且拿**最后一帧**当锚点。全量回归时机器正忙，
        #    首连建立慢于任务收尾（slow-step 四步 ×800ms≈3.2s），两帧正好是"快照 + 终态"，
        #    三次尝试全部落空，整道门禁误报成红（实测 24 道里 1 道红，单跑却 25/25 全绿）。
        #    治法仍是**构造前置条件**而不是放宽断言：
        #    a) 多取几帧（8 帧），要的是"流里出现过非终态帧"，不是"最后一帧是非终态"；
        #    b) 锚点取流里**第一个非终态帧**的 seq，其后必然还有帧可补；
        #    c) 尝试次数 3 → 6。
        #    注意 seq=0 是快照帧，拿它当 Last-Event-ID 等于"没带"，必须跳过。
        for attempt in range(1, 7):
            tid = _submit_slow()
            if not tid:
                break
            first = read_sse(base, f"/api/v1/tasks/{tid}/events", token,
                             max_events=8, idle_timeout=12, with_ids=True)
            fst = [d.get("status") for _, _, d in first if isinstance(d, dict) and "status" in d]
            cand = None
            for fid, _, d in first:
                if not (fid and fid.isdigit()) or int(fid) <= 0:
                    continue
                if not isinstance(d, dict) or "status" not in d:
                    continue
                if d.get("status") in TERMINAL:
                    continue
                cand = int(fid)
                break
            note(f"A7 首连收到 {len(first)} 帧 状态={fst} 锚点={cand}"
                 f"{'' if attempt == 1 else f'（第 {attempt} 次尝试）'}")
            if cand:
                rid, anchor, last_id = tid, cand, cand
                break
            note("   流里没有可用的非终态帧（首连接上时任务已收尾）：换一个新任务重来（最多 6 次）")

        if rid is None:
            rec("A7 重连带 Last-Event-ID 真的走了续传分支（sa_sse_replay_total 增长，未退化成快照）",
                False, "三次都没能构造出'锚点在非终态帧'的前置条件，无法验证补帧")
            rec("A7b 续传补到断线之后的事件，且首帧 seq 严格大于 Last-Event-ID（不重发已收到的）",
                False, "前置条件未构造出来")
            rec("A7c 续传不重推 seq=0 快照（那是首次连接才有的待遇）", False, "前置条件未构造出来")
            rec("A7g 锚点落在终态帧时秒回 EOF、不挂住连接（本次误报的真实成因）", False,
                "前置条件未构造出来")
        else:
            m0 = metric_total(base, METRICS_TOKEN, "sa_sse_replay_total")
            g0 = metric_total(base, METRICS_TOKEN, "sa_sse_replay_gap_total")
            # 断线窗口必须落在任务**还在跑**的时候（锚点已保证非终态）。
            # 0.2s 足以让后台再推至少一帧，又保证任务未收尾——这条路才真的被覆盖。
            time.sleep(0.2)
            resumed = read_sse(base, f"/api/v1/tasks/{rid}/events", token,
                               max_events=3, idle_timeout=12, last_id=last_id, with_ids=True)
            m1 = metric_total(base, METRICS_TOKEN, "sa_sse_replay_total")
            g1 = metric_total(base, METRICS_TOKEN, "sa_sse_replay_gap_total")
            rseqs = [d.get("seq") for _, _, d in resumed if isinstance(d, dict) and "seq" in d]
            # 计数器为 0 时 Prometheus 文本里根本不会出现该名字（首次 inc 才露面），
            # 所以"读不到"要当 0 处理，不能当"指标缺失"——否则第一次续传必然误判成没走续传分支。
            m0v, m1v = (0.0 if m0 is None else m0), (0.0 if m1 is None else m1)
            g0v, g1v = (0.0 if g0 is None else g0), (0.0 if g1 is None else g1)
            used_replay = m1 is not None and m1v > m0v
            gap_clean = g1 is None or g1v == g0v
            rec("A7 重连带 Last-Event-ID 真的走了续传分支（sa_sse_replay_total 增长，未退化成快照）",
                used_replay and gap_clean,
                f"replay {m0v}→{m1v} gap {g0v}→{g1v} 续传 {len(resumed)} 帧 seq={rseqs[:6]}")
            rec("A7b 续传补到断线之后的事件，且首帧 seq 严格大于 Last-Event-ID（不重发已收到的）",
                bool(rseqs) and rseqs[0] is not None and last_id is not None and rseqs[0] > last_id,
                f"首帧 seq={rseqs[0] if rseqs else None} Last-Event-ID={last_id} 共 {len(rseqs)} 帧")
            rec("A7c 续传不重推 seq=0 快照（那是首次连接才有的待遇）",
                bool(rseqs) and not any(x == 0 for x in rseqs), f"seq 序列={rseqs[:6]}")
            # 非法锚点：一律当"没有"，回退到 seq=0 快照，且**不算跟丢**（gap 不该涨）
            bad = read_sse(base, f"/api/v1/tasks/{rid}/events", token,
                           max_events=1, idle_timeout=10, last_id="not-a-number", with_ids=True)
            bseq = [d.get("seq") for _, _, d in bad if isinstance(d, dict) and "seq" in d]
            g2 = metric_total(base, METRICS_TOKEN, "sa_sse_replay_gap_total")
            rec("A7d 非法 Last-Event-ID 退回首次连接（给 seq=0 快照，且不计入 sa_sse_replay_gap_total）",
                bseq[:1] == [0] and (g1 is None or g2 is None or g2 == g1),
                f"首帧 seq={bseq[:3]} gap {g1}→{g2}")
            # 任务收尾后再连一次：应当立刻给一份 status 快照，让前端不用回查 detail 就知道结局；
            # 且拿完就收流，不白挂一个订阅位到进程退出。
            # （锚点不能用快照帧的 id——快照 seq 恒为 0，拿 0 当 Last-Event-ID 等于"没带"，会绕回这里。）
            fin, _, _ = wait_terminal(c, rid, token, timeout=30)
            t_late = time.perf_counter()
            late = read_sse(base, f"/api/v1/tasks/{rid}/events", token,
                            max_events=1, idle_timeout=6, with_ids=True)
            el_late = time.perf_counter() - t_late
            late_seq = [d.get("seq") for _, _, d in late if isinstance(d, dict) and "seq" in d]
            late_st = [d.get("status") for _, _, d in late if isinstance(d, dict) and "status" in d]
            rec("A7e 任务已终态后重连：立刻拿到终态快照（不用回查 detail），且不空等挂连接",
                late_seq[:1] == [0] and late_st[:1] == [fin] and el_late < 3.0,
                f"首帧 seq={late_seq[:1]} status={late_st[:1]} 耗时={el_late:.2f}s")
            # 锚点远超已有帧：补不齐也不许挂住、不许返回缺格的假进度
            t1 = time.perf_counter()
            ahead = read_sse(base, f"/api/v1/tasks/{rid}/events", token,
                             max_events=1, idle_timeout=6, last_id=999999, with_ids=True)
            el2 = time.perf_counter() - t1
            rec("A7f 锚点超前且无帧可补时不挂连接（快速 EOF），不白占一个订阅位",
                not ahead and el2 < 3.0, f"帧数={len(ahead)} EOF 耗时={el2:.2f}s")
            # A7g：锚点=该任务**最后一帧**且任务已终态时，服务端必须秒回 EOF。
            # 这正是当初把门禁误报成红的那条路径——它不是缺陷，是设计；既然容易被误会，
            # 就把它立成断言钉死：既证明"不挂住"，也顺带说明 A7b 为何要先构造非终态锚点。
            end_seq = max([x for x in rseqs if x is not None] + [last_id or 0])
            t2 = time.perf_counter()
            tail = read_sse(base, f"/api/v1/tasks/{rid}/events", token,
                            max_events=2, idle_timeout=6, last_id=end_seq, with_ids=True)
            el3 = time.perf_counter() - t2
            tail_seq = [d.get("seq") for _, _, d in tail if isinstance(d, dict) and "seq" in d]
            rec("A7g 锚点=最后一帧且任务已终态：秒回 EOF，不挂住连接（原误报路径的正向断言）",
                not tail and el3 < 3.0,
                f"锚点={end_seq} 帧数={len(tail)} seq={tail_seq[:3]} EOF 耗时={el3:.2f}s")

    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if logf:
            logf.close()
        # 刻意不删数据目录：清理动作一律交给内置 delete 工具或人工，脚本自身绝不做递归删除
        if data_dir is not None and not args.base:
            note(f"数据目录已保留（未自动清理）：{data_dir}")
            note(f"服务日志：{pathlib.Path(str(data_dir) + '-server.log')}")

    passed = sum(1 for _, ok, _ in results if ok)
    failed = len(results) - passed
    print("=" * 78)
    print(f"合计 {len(results)} 项 | 通过 {passed} | 失败 {failed}")
    if failed:
        print("失败项：")
        for name, ok, detail in results:
            if not ok:
                print(f"  - {name}  -> {detail}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
