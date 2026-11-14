#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check-response-shape.py —— 契约的"落地一致"门禁：真实响应 JSON 形状 vs OpenAPI schema。

为什么必须要有这道门禁
----------------------
已有的 check-contract.py / check-openapi.py / gen-ts-types.py 全是**静态**比对：
源码 ⇄ 生成器 ⇄ 文档 ⇄ TS 类型。它们共同仰赖一个前提——"生成器读源码的姿势是对的"。
生成器一旦读漏（典型：把 `#[serde(flatten)]` 当成一个普通嵌套字段），
生成器与校验器就会**互相背书**：十几道门禁全绿，真实响应却与契约差了一层嵌套，
前端照着契约读 `data.task.goal`，真实字段是 `data.goal`，拿到 undefined 且不抛错。
这类偏差只有"真起进程、真发 HTTP、拿真实 JSON 对 schema"才能抓到。

判定规则（每一条都对应一种前端会受伤的形态）
  - schema.required 里的键，响应里没有        → FAIL（前端读 undefined）
  - 响应出现 schema.properties 里没有的键     → FAIL（契约漏字段）
  - type / enum 与响应不符                    → FAIL（前端类型判断失效）
  - 状态码不在该操作的 responses 里            → FAIL（前端 case 分支缺失）

刻意不做：不校验业务语义（那是 smoke.py 的活），不发真模型请求（毫秒级完成），
不覆盖 /metrics（Prometheus 文本，不走 JSON schema）。

用法：
    python scripts/check-response-shape.py --exe <path-to-super-agents.exe>
    python scripts/check-response-shape.py --base http://127.0.0.1:18080   # 对已起实例跑
退出码：0 = 全部一致；1 = 发现不一致。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
OPENAPI = ROOT / "docs" / "openapi.json"

OK, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []

# 明确"没有 JSON 响应体"的端点白名单：只有写进这张表的 (方法, 路径模板, 状态码)
# 才允许"契约无 schema 但不算失败"。其余一律视为契约漏登。
#
# 为什么要有这张表（2026-11-14 复盘）：原实现是"契约没有 schema 就记 PASS，
# 跳过形状比对"。于是 `POST /api/v1/tasks` 的 201/202 在契约里 content 是空的、
# 实际响应却是一整份 TaskDetail，门禁 14 每次都印 "跳过形状比对" 然后算通过，
# 39/39 全绿。也就是说：**契约越缺，门禁越绿**。判定力必须反过来——
# 契约缺 schema 而真实返回了 JSON，就是契约漏字段，必须红。
NO_JSON_BODY_OK: set[tuple[str, str, str]] = {
    # 204 天生无响应体
    ("delete", "/api/v1/memories/{id}", "204"),
    # 纯文本 / Prometheus 文本，不走 JSON
    ("get", "/healthz", "200"),
    ("get", "/readyz", "200"),
    ("get", "/metrics", "200"),
}


def rec(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, OK if ok else FAIL, detail))
    mark = "OK  " if ok else "FAIL"
    line = f"[{mark}] {name}"
    if detail:
        line += f"  ->  {detail}"
    print(line, flush=True)


def skip(name: str, why: str) -> None:
    results.append((name, SKIP, why))
    print(f"[SKIP] {name}  ->  {why}", flush=True)


# --------------------------------------------------------------------------- #
# 迷你 JSON Schema 校验器（只覆盖本项目 openapi.json 实际用到的关键字）
# --------------------------------------------------------------------------- #
def resolve(doc: dict, schema):
    """跟随 $ref 到实体 schema；遇到自引用/坏引用返回空 dict（当作不约束）。"""
    hops = 0
    while isinstance(schema, dict) and "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            return {}
        node = doc
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                return {}
            node = node[part]
        schema = node
        hops += 1
        if hops > 32:
            return {}
    return schema


def jtype(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return "?"


def validate(inst, schema, doc: dict, path: str, errs: list, depth: int = 0) -> None:
    if depth > 24:
        return
    schema = resolve(doc, schema)
    if not schema:
        return

    branch_key = "anyOf" if "anyOf" in schema else ("oneOf" if "oneOf" in schema else None)
    if branch_key:
        best = None
        for b in schema[branch_key]:
            sub: list = []
            validate(inst, b, doc, path, sub, depth + 1)
            if not sub:
                return  # 任一分支通过即通过
            if best is None or len(sub) < len(best):
                best = sub
        errs.extend(best or [f"{path}: 不满足 {branch_key} 的任何分支"])
        return

    if "enum" in schema and inst not in schema["enum"]:
        errs.append(f"{path}: 值 {inst!r} 不在枚举 {schema['enum']}")
        return

    declared = schema.get("type")
    if declared:
        wants = declared if isinstance(declared, list) else [declared]
        actual = jtype(inst)
        # JSON 里整数同时也满足 number；反之不成立
        if actual not in wants and not (actual == "integer" and "number" in wants):
            errs.append(f"{path}: 类型={actual}，schema 要求 {'/'.join(wants)}")
            return

    if isinstance(inst, dict):
        props = schema.get("properties") or {}
        for need in schema.get("required") or []:
            if need not in inst:
                errs.append(f"{path}.{need}: 缺必填字段（schema.required 有，响应里没有）")
        ap = schema.get("additionalProperties")
        for k, v in inst.items():
            if k in props:
                validate(v, props[k], doc, f"{path}.{k}", errs, depth + 1)
            elif isinstance(ap, dict):
                validate(v, ap, doc, f"{path}.{k}", errs, depth + 1)
            elif ap is False:
                errs.append(f"{path}.{k}: schema 明确不允许额外字段")
            elif props:
                errs.append(f"{path}.{k}: 响应多出此键，schema.properties 里没有（契约漏字段）")
    elif isinstance(inst, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for i, v in enumerate(inst):
                validate(v, items, doc, f"{path}[{i}]", errs, depth + 1)


# --------------------------------------------------------------------------- #
# HTTP 客户端
# --------------------------------------------------------------------------- #
class Client:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.token: str | None = None

    def raw(self, method: str, path: str, body=None, token=None, timeout: int = 120, ctype=None):
        url = self.base + path
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            if isinstance(body, bytes):
                data = body
            else:
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = ctype or "application/json"
        tk = token if token is not None else self.token
        if tk:
            headers["Authorization"] = "Bearer " + tk
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:  # 连接层失败也要给出可读原因，不能静默
            return -1, json.dumps({"transport_error": repr(e)}).encode("utf-8")

    def js(self, method: str, path: str, body=None, token=None, timeout: int = 120):
        st, raw = self.raw(method, path, body, token, timeout=timeout)
        if not raw:
            return st, None
        try:
            return st, json.loads(raw.decode("utf-8"))
        except Exception:
            return st, {"_unparsed": raw.decode("utf-8", "replace")[:400]}


# --------------------------------------------------------------------------- #
# 场景执行
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default="", help="super-agents.exe 路径")
    ap.add_argument("--base", default="", help="对已在跑的实例校验（跳过启动）")
    ap.add_argument("--port", type=int, default=18241)
    ap.add_argument("--data-dir", default="")
    ap.add_argument("--keep", action="store_true", help="保留数据目录与日志")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if not OPENAPI.exists():
        print(f"[FAIL] 找不到 {OPENAPI}，先运行 python scripts/gen-openapi.py")
        return 1
    doc = json.loads(OPENAPI.read_text(encoding="utf-8"))

    def get_schema(method: str, tmpl: str, status: int):
        node = doc["paths"].get(tmpl, {}).get(method.lower(), {}).get("responses", {}).get(str(status))
        if not node:
            return None
        content = node.get("content") or {}
        js = content.get("application/json")
        return js.get("schema") if js else None

    def get_sse_schema(tmpl: str, status: int = 200):
        node = doc["paths"].get(tmpl, {}).get("get", {}).get("responses", {}).get(str(status), {})
        js = (node.get("content") or {}).get("text/event-stream")
        return js.get("schema") if js else None

    proc = None
    logf = None
    data_dir = None
    if not args.base:
        exe = args.exe
        if not exe:
            # 以前这里写死 %LOCALAPPDATA%\sa-build\target\debug —— 那是**已弃用**的旧镜像
            # （现行镜像是 target-gnu）。真实踩到：手动跑本脚本时它起的是几个月前的旧 exe，
            # 报出 5 项"形状不符"，而门禁里用真 exe 全绿。假绿会让人放松，假红同样误导人。
            # 改为与门禁 0（build-exe.py）同一套解析规则，杜绝两个镜像各测一套。
            exe = ""
            try:
                sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
                from sync_build import resolve_build  # noqa: PLC0415
                mirror = resolve_build()
                for rel in (("target-gnu", "debug"), ("target", "debug")):
                    cand = mirror.joinpath(*rel) / "super-agents.exe"
                    if cand.exists():
                        exe = str(cand)
                        break
                else:
                    exe = str(mirror / "target-gnu" / "debug" / "super-agents.exe")
            except Exception:  # noqa: BLE001
                exe = str(pathlib.Path(os.environ.get("LOCALAPPDATA", ".")) / "sa-build" / "target" / "debug" / "super-agents.exe")
        if not pathlib.Path(exe).exists():
            print(f"[FAIL] 找不到可执行文件: {exe}（用 --exe 指定）")
            return 2
        data_dir = pathlib.Path(args.data_dir or tempfile.mkdtemp(prefix="sa-shape-"))
        data_dir.mkdir(parents=True, exist_ok=True)
        log_path = pathlib.Path(str(data_dir) + "-server.log")
        env = dict(os.environ)
        env.update(
            {
                "SA_DATA_DIR": str(data_dir),
                "SA_BIND": f"127.0.0.1:{args.port}",
                "SA_ENV": "dev",
                "SA_JWT_SECRET": "shape-check-secret-0123456789abcdef0123456789",
                "SA_METRICS_TOKEN": "shape-check-metrics-token-0123456789",
                "SA_ALLOW_REGISTRATION": "true",
                "SA_STORAGE": "json",
                "SA_RUNNER": "sim",
                "SA_AUDIT": "file",
                "SA_TASK_SUBMIT_QUOTA_PER_HOUR": "500",
            }
        )
        # 本门禁只验形状，**绝不能**因为开发机上有 SA_LLM_* 就变成分钟级甚至超时。
        for k in [k for k in env if k.startswith("SA_LLM_")]:
            env.pop(k)
        logf = open(log_path, "w", encoding="utf-8", errors="replace")
        proc = subprocess.Popen([exe], env=env, stdout=logf, stderr=subprocess.STDOUT)
        base = f"http://127.0.0.1:{args.port}"
    else:
        base = args.base

    C = Client(base)

    def check(label: str, method: str, tmpl: str, actual: str, want, body=None, token=None, note: str = ""):
        st, b = C.js(method, actual, body, token)
        wants = want if isinstance(want, (list, tuple)) else [want]
        detail = f"{method} {actual} status={st}"
        if note:
            detail += f" [{note}]"
        if st not in wants:
            rec(label, False, detail + f" 期望={list(wants)} body={json.dumps(b, ensure_ascii=False)[:220]}")
            return st, b
        schema = get_schema(method, tmpl, st)
        if schema is None:
            key = (method.lower(), tmpl, str(st))
            if key in NO_JSON_BODY_OK:
                rec(label, True, detail + "（契约声明该状态码无 JSON 响应体，白名单内）")
                return st, b
            # 契约缺 schema，但真实响应带着 JSON —— 这就是"契约漏登响应体"，
            # 不改判成 PASS 才是本门禁存在的意义。
            if isinstance(b, (dict, list)) and b:
                rec(label, False, detail + " 契约该状态码没有响应体 schema，但真实响应是 JSON"
                                          "（前端照契约拿不到类型，等于没写）")
            else:
                rec(label, True, detail + "（响应体非 JSON）")
            return st, b
        errs: list = []
        validate(b, schema, doc, "$", errs)
        if errs:
            rec(label, False, detail + f" 形状不符 {len(errs)} 处: " + " | ".join(errs[:5]))
        else:
            rec(label, True, detail + " 形状一致")
        return st, b

    try:
        # 0) 就绪
        ready = False
        for _ in range(120):
            st, b = C.js("GET", "/readyz", timeout=5)
            if st == 200 and (b or {}).get("ready"):
                ready = True
                break
            if proc is not None and proc.poll() is not None:
                break
            time.sleep(0.5)
        rec("R00 服务就绪（/readyz ready=true）", ready, "60 秒内未就绪" if not ready else "")
        if not ready:
            return 1

        stamp = uuid.uuid4().hex[:8]
        username = f"shape_{stamp}"
        email = f"{username}@example.test"
        password = "Shape-Check-Passw0rd!"

        # 1) 认证链路
        st, reg = check(
            "R01 POST /auth/register 形状 vs AuthResult",
            "POST", "/api/v1/auth/register", "/api/v1/auth/register", 201,
            {"username": username, "email": email, "password": password},
        )
        check(
            "R02 POST /auth/login 形状 vs AuthResult（account 传 username）",
            "POST", "/api/v1/auth/login", "/api/v1/auth/login", 200,
            {"account": username, "password": password},
        )
        # 契约里 `account` 的说明是"用户名或邮箱均可"，这是一条**没被任何门禁证明过**的承诺：
        # 只说"能登录"不够，返回形状也得跟 AuthResult 一致（否则前端 login 有两种分支要处理）。
        st, login = check(
            "R02b POST /auth/login 形状 vs AuthResult（account 传 email）",
            "POST", "/api/v1/auth/login", "/api/v1/auth/login", 200,
            {"account": email, "password": password},
        )
        tokens = (login or {}).get("tokens") or {}
        C.token = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token") or ""
        rec("R02c 拿到 access_token / refresh_token", bool(C.token) and bool(refresh_token), "")

        check("R03 GET /auth/me 形状 vs UserView", "GET", "/api/v1/auth/me", "/api/v1/auth/me", 200)
        if refresh_token:
            st, ref = check(
                "R04 POST /auth/refresh 形状 vs AuthResult",
                "POST", "/api/v1/auth/refresh", "/api/v1/auth/refresh", 200,
                {"refresh_token": refresh_token},
            )
            # 刷新会轮换令牌、旧 refresh_token 立即作废，后续 logout 必须用新的
            refreshed = ((ref or {}).get("tokens") or {}).get("refresh_token")
            if refreshed:
                refresh_token = refreshed

        # 2) 任务：同步分支（201）
        # 显式给 task_type：不传时后端按 goal 关键词打分，打不到任何信号会 422 拒绝
        # （那是刻意的"不猜"策略，见 docs/03）。这里要验的是形状，不是路由，所以给确定的类型。
        # 显式给 mode=sync：2026-11 把缺省从 sync 改成 async 之后，缺省不再产出 201。
        # 想验 201 就必须点名要走同步分支——门禁脚本从此不再吃缺省值。
        goal = f"响应形状门禁自检 {stamp}：核对接口契约与实际返回的 JSON 是否一致"
        st, t_sync = check(
            "R05 POST /tasks（显式 mode=sync）201 + TaskDetail 形状",
            "POST", "/api/v1/tasks", "/api/v1/tasks", 201,
            {"mode": "sync", "goal": goal, "task_type": "research"},
        )
        # R05b 缺省模式的形状：缺省=async 是契约声明，这里守"202 也必须是完整 TaskDetail"。
        # 201 与 202 共用同一 schema，若哪天有人给 202 单独塞一个精简体，前端会拿到 undefined。
        st, t_async = check(
            "R05b POST /tasks（缺省，即 async 分支）202 + TaskDetail 形状",
            "POST", "/api/v1/tasks", "/api/v1/tasks", 202,
            {"goal": goal + "（缺省分支）", "task_type": "research"},
        )
        async_state = (t_async or {}).get("status")
        rec(
            "R05c 缺省受理返回的状态是 queued（不是假装已经跑完）",
            async_state == "queued",
            f"status={async_state}",
        )
        sync_id = (t_sync or {}).get("id") or ""
        if sync_id:
            st, detail = check(
                "R06 GET /tasks/{id} 200 + TaskDetail 形状",
                "GET", "/api/v1/tasks/{id}", f"/api/v1/tasks/{sync_id}", 200,
            )
            # 这条是本次改造的"靶心"：flatten 必须平铺，不能套一层 task
            body = detail if isinstance(detail, dict) else {}
            rec(
                "R06a 真实响应是平铺的（顶层不含嵌套 task 键）",
                "task" not in body,
                f"顶层键={sorted(body.keys())[:6]}..." if "task" in body else "平铺确认",
            )
            rec(
                "R06b 平铺证据：顶层直接含 goal / trace / memory_refs",
                all(k in body for k in ("goal", "trace", "memory_refs")),
                f"缺={[k for k in ('goal', 'trace', 'memory_refs') if k not in body]}",
            )
            check(
                "R07 GET /tasks/{id}/trace 200 + TraceEntry[] 形状",
                "GET", "/api/v1/tasks/{id}/trace", f"/api/v1/tasks/{sync_id}/trace", 200,
            )
            check(
                "R08 GET /tasks?limit=5 200 + TaskList 形状",
                "GET", "/api/v1/tasks", "/api/v1/tasks?limit=5", 200,
            )
        else:
            skip("R06/R07/R08", "同步建任务未拿到 id")

        # 3) 任务：异步分支（202）
        st, t_async = check(
            "R09 POST /tasks（mode=async）202 + TaskDetail 形状",
            "POST", "/api/v1/tasks", "/api/v1/tasks", 202,
            {"goal": f"异步分支形状自检 {stamp}", "task_type": "writing", "mode": "async"},
        )
        async_id = (t_async or {}).get("id") or ""

        # R10 单独提交一条"慢任务"专供取消：sim 后端跑得太快，前面那条 202 的
        # 任务往往在这一行之前就已经 succeeded，取消只能拿到 409 conflict。
        # 409 是**正确**行为（任务已结束不能取消），但它验不到"取消成功"那条路。
        # goal 里带 "slow-step" 是项目内置的慢任务注入开关（仅 sim 后端生效），
        # 每一步固定睡 SA_SIM_SLOW_MS（默认 300ms），让任务有可观测的持续期。
        st, t_slow = check(
            "R08b POST /tasks（mode=async + slow-step）202 + TaskDetail 形状",
            "POST", "/api/v1/tasks", "/api/v1/tasks", 202,
            {"goal": f"slow-step 取消链路演练 {stamp}", "task_type": "research", "mode": "async"},
        )
        slow_id = (t_slow or {}).get("id") or ""
        if slow_id:
            check(
                "R10 POST /tasks/{id}/cancel 202|200 + TaskDetail 形状",
                "POST", "/api/v1/tasks/{id}/cancel", f"/api/v1/tasks/{slow_id}/cancel", (202, 200),
            )
        else:
            skip("R10", "慢任务未拿到 id")

        # 竞态兜底：即便慢任务也可能已经跑完，此时取消**必须**返回 409 + Problem 形状，
        # 不能 500、不能裸 200。这条与 R10 互补：一条验"取消得到受理"，一条验"取消不了要说清楚"。
        if async_id:
            st2, conflict_body = C.js("POST", f"/api/v1/tasks/{async_id}/cancel")
            if st2 in (200, 202):
                rec("R10b 已结束任务的取消语义", True, "该任务仍在执行，取消被受理（走 R10 同一分支）")
            elif st2 == 409:
                errs2: list = []
                problem = (doc.get("components", {}).get("schemas", {}) or {}).get("Problem")
                if problem:
                    validate(conflict_body, problem, doc, "$", errs2)
                rec(
                    "R10b 已结束任务的取消语义",
                    not errs2 and bool((conflict_body or {}).get("message")),
                    "409 + Problem 形状一致" if not errs2 else " | ".join(errs2[:3]),
                )
            else:
                rec("R10b 已结束任务的取消语义", False, f"status={st2} 期望 409 或 20x")

        if async_id:
            # SSE 首帧：契约说是 text/event-stream + TaskEvent
            try:
                frame_text = sse_first_frame(base, f"/api/v1/tasks/{async_id}/events", C.token or "")
                data_lines = [ln[5:].strip() for ln in frame_text.splitlines() if ln.startswith("data:")]
                payload = json.loads(data_lines[0]) if data_lines else None
                schema = get_sse_schema("/api/v1/tasks/{id}/events")
                errs: list = []
                if payload is None:
                    rec("R11 GET /tasks/{id}/events 首帧可解析为 JSON", False, f"首帧原文={frame_text[:160]!r}")
                elif schema is None:
                    # 同一条原则：SSE 端点契约里必须写明帧 schema（x-stream.frameSchema
                    # 与 content['text/event-stream'].schema 都算），没写就是漏登，不是"跳过"。
                    rec("R11 GET /tasks/{id}/events 首帧形状 vs TaskEvent", False,
                        "契约缺 SSE 帧 schema：前端无法得知 data 里是什么，等于契约未交付")
                else:
                    validate(payload, schema, doc, "$", errs)
                    rec(
                        "R11 GET /tasks/{id}/events 首帧形状 vs TaskEvent",
                        not errs,
                        "形状一致" if not errs else " | ".join(errs[:4]),
                    )
            except Exception as e:
                rec("R11 GET /tasks/{id}/events 首帧形状 vs TaskEvent", False, f"读取失败 {e!r}")
        else:
            skip("R10/R11", "异步建任务未拿到 id")

        # 4) 只读资源
        st, agents = check("R12 GET /agents 200 + AgentView[] 形状", "GET", "/api/v1/agents", "/api/v1/agents", 200)
        if isinstance(agents, list) and agents:
            aid = agents[0].get("id")
            check("R13 GET /agents/{id} 200 + AgentView 形状", "GET", "/api/v1/agents/{id}", f"/api/v1/agents/{aid}", 200)
        st, caps = check("R14 GET /capabilities 200 + CapabilityView[] 形状", "GET", "/api/v1/capabilities", "/api/v1/capabilities", 200)
        if isinstance(caps, list) and caps:
            cid = caps[0].get("id")
            check("R15 GET /capabilities/{id} 200 + CapabilityView 形状", "GET", "/api/v1/capabilities/{id}", f"/api/v1/capabilities/{cid}", 200)

        # 5) 记忆
        st, up = check(
            "R16 POST /memories（首次写）201 + UpsertResult 形状",
            "POST", "/api/v1/memories", "/api/v1/memories", (201, 200),
            {"scope": "private", "key": f"shape-{stamp}", "content": "形状门禁写入的记忆条目", "tags": ["shape"]},
        )
        mem_id = ((up or {}).get("entry") or {}).get("id") or ""
        check("R17 GET /memories 200 + MemoryList 形状", "GET", "/api/v1/memories", "/api/v1/memories", 200)
        if mem_id:
            check(
                "R18 PUT /memories/{id} 200 + MemoryEntry 形状",
                "PUT", "/api/v1/memories/{id}", f"/api/v1/memories/{mem_id}", 200,
                {"content": "形状门禁更新后的内容"},
            )
            st, _ = C.js("DELETE", f"/api/v1/memories/{mem_id}")
            rec("R19 DELETE /memories/{id} 204（无响应体）", st == 204, f"status={st}")

        # 6) 引导 / 可观测
        st, ob = check("R20 GET /onboarding 200 + OnboardingView 形状", "GET", "/api/v1/onboarding", "/api/v1/onboarding", 200)
        check("R21 GET /observability/overview 200 + Overview 形状", "GET", "/api/v1/observability/overview", "/api/v1/observability/overview", 200)
        check("R22 GET /observability/audit 200 + AuditList 形状", "GET", "/api/v1/observability/audit", "/api/v1/observability/audit?limit=5", 200)
        check("R23 GET /observability/components 200 + ComponentList 形状", "GET", "/api/v1/observability/components", "/api/v1/observability/components", 200)
        # R31：告警能力是第 19 道门禁的行为主体，但行为门禁只验"数字对不对"，
        # 验不了"发出去的 JSON 字段齐不齐"。这里补形状：AlertList 的 7 个顶层字段
        # 少一个，前端图表就会画成空白，而行为断言全绿也发现不了。
        check(
            "R31 GET /observability/alerts 200 + AlertList 形状",
            "GET", "/api/v1/observability/alerts", "/api/v1/observability/alerts", 200,
        )
        steps = (ob or {}).get("steps") or []
        step_key = next((s.get("key") for s in steps if isinstance(s, dict) and not s.get("completed")), None)
        if step_key:
            check(
                "R24 POST /onboarding/steps/{key}/complete 200 + StepChangeView 形状",
                "POST", "/api/v1/onboarding/steps/{step_id}/complete",
                f"/api/v1/onboarding/steps/{step_key}/complete", 200,
            )
        else:
            skip("R24", "没有未完成的引导步骤")
        check("R25 POST /onboarding/skip 200 + OnboardingView 形状", "POST", "/api/v1/onboarding/skip", "/api/v1/onboarding/skip", 200)

        # 7) 错误形状：前端要靠 Problem 统一渲染错误
        check("R26 无令牌 GET /tasks → 401 + Problem", "GET", "/api/v1/tasks", "/api/v1/tasks", 401, token="")
        ghost = str(uuid.uuid4())
        check("R27 GET /tasks/{不存在} → 404 + Problem", "GET", "/api/v1/tasks/{id}", f"/api/v1/tasks/{ghost}", 404)
        check("R28 POST /tasks 缺 goal → 422 + Problem", "POST", "/api/v1/tasks", "/api/v1/tasks", 422, {})
        st, b = C.raw("POST", "/api/v1/tasks", body=b"{bad json", ctype="application/json")
        try:
            body_json = json.loads(b.decode("utf-8")) if b else None
        except Exception:
            body_json = {"_unparsed": b.decode("utf-8", "replace")[:200]}
        schema = get_schema("POST", "/api/v1/tasks", st)
        errs = []
        if schema is None:
            # 与 check() 同一个原则：契约没写 = 漏登，不是"跳过"。
            errs = [f"契约缺 {st} 的响应体 schema（400 必须能渲染 Problem）"]
        else:
            validate(body_json, schema, doc, "$", errs)
        rec(
            "R29 POST /tasks 非法 JSON → 400 + Problem",
            st == 400 and not errs,
            f"status={st} " + ("形状一致" if not errs else " | ".join(errs[:3])),
        )

        # 8) 登出（契约里 requestBody.required=true，必须带 refresh_token）
        if refresh_token:
            check(
                "R30 POST /auth/logout 200 + RevokedView 形状",
                "POST", "/api/v1/auth/logout", "/api/v1/auth/logout", 200,
                {"refresh_token": refresh_token},
            )
        else:
            skip("R30", "没有可用的 refresh_token")

    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        if logf is not None:
            logf.close()
        if data_dir is not None and not args.keep:
            shutil.rmtree(data_dir, ignore_errors=True)
            log_path.unlink(missing_ok=True)

    passed = sum(1 for _, s, _ in results if s == OK)
    failed = [r for r in results if r[1] == FAIL]
    skipped = [r for r in results if r[1] == SKIP]
    print("-" * 70)
    print(f"响应形状检查：{passed} 通过 / {len(failed)} 失败 / {len(skipped)} 跳过，共 {len(results)} 项")
    if failed:
        print("失败清单：")
        for name, _, detail in failed:
            print(f"  [FAIL] {name}  ->  {detail}")
        return 1
    print("结论：所有 JSON 端点的真实响应形状与 docs/openapi.json 一致。")
    return 0


def sse_first_frame(base: str, path: str, token: str, timeout: int = 20) -> str:
    """读到第一个空行（一帧结束）即断开，避免把整个事件流读完。"""
    req = urllib.request.Request(base.rstrip("/") + path, method="GET")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "text/event-stream")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        buf = b""
        while b"\n\n" not in buf and len(buf) < 65536:
            ch = r.read(1)
            if not ch:
                break
            buf += ch
        return buf.decode("utf-8", "replace")


if __name__ == "__main__":
    sys.exit(main())
