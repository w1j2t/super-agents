#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
端到端冒烟（不依赖任何第三方库，只用标准库）。

用法：
    python scripts/smoke.py                     # 自动寻找 exe，起临时实例跑全部断言
    python scripts/smoke.py --exe <path>        # 指定 exe
    python scripts/smoke.py --no-quota          # 跳过 60/h 限流那一段（省时间）
    python scripts/smoke.py --base http://127.0.0.1:8088   # 对已在跑的实例做只读冒烟（会自动跳过写操作之外的危险项）
    python scripts/smoke.py --keep              # 跑完不删数据目录，便于看日志

退出码：0 = 全部通过；1 = 有失败项（逐项列出）。

设计原则（给审计方看）：
1. 断言只写"源码里能推导出的期望"，不写"跑出来是什么就写什么"。
2. 每项断言都会打印实际值，失败项直接给 HTTP 状态码 + 响应体前 200 字。
3. 涉及"返回码有多种合理可能"的地方（例如越权可能是 403 也可能是 404 以防信息泄漏），
   断言写成集合，但**把实际命中的那个值打印出来**，不允许含糊。
"""

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import pathguard  # noqa: E402  （SA-031：删除前统一过守卫）

# 数据目录的归属标记：只有本次冒烟自己创建的目录里才会出现这个文件。
SMOKE_OWNER_MARK = ".sa-smoke-owner"

OK, FAIL = "PASS", "FAIL"
results = []


def rec(name, ok, detail=""):
    results.append((name, OK if ok else FAIL, detail))
    mark = "OK  " if ok else "FAIL"
    line = f"[{mark}] {name}"
    if detail:
        line += f"  ->  {detail}"
    print(line, flush=True)


def expect(name, actual, allowed, detail=""):
    """actual 落在 allowed 里即通过，同时打印实际值。"""
    allowed = allowed if isinstance(allowed, (list, tuple, set)) else [allowed]
    ok = actual in allowed
    shown = f"实际={actual}" + (f" 期望={list(allowed)}" if len(allowed) > 1 else "")
    if detail:
        shown += f" | {detail}"
    rec(name, ok, shown)


class Client:
    def __init__(self, base):
        self.base = base.rstrip("/")

    def __call__(self, method, path, body=None, token=None, raw=False, headers=None):
        url = self.base + path
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        r = urllib.request.Request(url, data=data, method=method)
        if body is not None:
            r.add_header("Content-Type", "application/json")
        if token:
            r.add_header("Authorization", f"Bearer {token}")
        for k, v in (headers or {}).items():
            r.add_header(k, v)
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                payload, status = resp.read(), resp.status
                hdrs = {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as e:
            payload, status = e.read(), e.code
            hdrs = {k.lower(): v for k, v in e.headers.items()}
        except Exception as e:  # 连接层失败
            return 0, {}, {"transport_error": repr(e)}
        text = payload.decode("utf-8", "replace")
        if raw:
            return status, hdrs, text
        try:
            return status, hdrs, (json.loads(text) if text else None)
        except Exception:
            return status, hdrs, text


def wait_ready(c, seconds=60):
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            s, _, b = c("GET", "/readyz")
            if s == 200:
                return True, b
        except Exception:
            pass
        time.sleep(0.25)
    return False, None


def sse_read(c, path, token, want=3, timeout=25):
    """真读一条 SSE 长连接。

    只用标准库：urlopen 拿到响应后**逐行 readline**，读到 want 帧或 EOF 就主动关闭，
    绝不把整条流读到底（终态前它是不会结束的）。

    返回 (status, content_type, frames, eof)：
    - frames 是每帧 data 解析出的 JSON 对象；
    - eof=True 表示服务端主动结束了流（HTTP EOF），不是被我们掐断的。
    """
    url = c.base + path
    req = urllib.request.Request(url, method="GET")
    req.add_header("Accept", "text/event-stream")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:  # 401/403/404 这类"连都没连上"的情况
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, (e.headers.get("Content-Type") or ""), [{"_error_body": body[:200]}], False
    except Exception as e:
        return 0, "", [{"_transport_error": repr(e)}], False

    ctype = resp.headers.get("Content-Type") or ""
    frames, eof, cur = [], False, {}
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            raw = resp.readline()
            if not raw:  # 服务端主动结束
                eof = True
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line:  # 空行 = 一帧结束
                if cur.get("data"):
                    try:
                        frames.append(json.loads(cur["data"]))
                    except Exception:
                        frames.append({"_raw": cur["data"]})
                cur = {}
                if len(frames) >= want:
                    break
                continue
            if line.startswith(":"):  # SSE 注释行（心跳）
                continue
            key, _, val = line.partition(":")
            cur[key.strip()] = val.strip()
    finally:
        resp.close()
    return 200, ctype, frames, eof


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default="")
    ap.add_argument("--port", type=int, default=18099)
    ap.add_argument("--data-dir", default="")
    ap.add_argument("--base", default="")
    ap.add_argument("--no-quota", action="store_true")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="额外注入的环境变量，可重复，例如 --env SA_STORAGE=memory",
    )
    args = ap.parse_args()

    extra_env = {}
    for item in args.env:
        if "=" not in item:
            print(f"--env 参数格式错误（需 KEY=VALUE）: {item}")
            return 2
        k, v = item.split("=", 1)
        extra_env[k.strip()] = v

    proc = None
    data_dir = None
    log_path = None
    if not args.base:
        # 默认二进制必须和 scripts/build-exe.py 的产出目录一致（target-gnu）。
        # 这里曾写死 target/debug，而 build-exe 编到 target-gnu，于是单跑冒烟
        # 测的是另一份更旧的 exe：门禁红、单跑绿，两边结论对不上（真踩过）。
        _root = pathlib.Path(os.environ.get("LOCALAPPDATA", ".")) / "sa-build"
        _cands = [
            _root / "target-gnu" / "debug" / "super-agents.exe",
            _root / "target" / "debug" / "super-agents.exe",
        ]
        exe = args.exe or next((str(p) for p in _cands if p.exists()), str(_cands[0]))
        if not pathlib.Path(exe).exists():
            print(f"找不到可执行文件: {exe}\n先按 docs/50-build-and-verify.md §4 编译，或用 --exe 指定路径。")
            return 2
        # 陈旧检测：exe 比 src 下任一 .rs 旧，说明测的是上一版代码，绿灯没有意义。
        _exe_m = pathlib.Path(exe).stat().st_mtime
        _src_root = pathlib.Path(__file__).resolve().parent.parent / "src"
        _newest = max((p.stat().st_mtime for p in _src_root.rglob("*.rs")), default=0)
        age_note = ""
        if _newest > _exe_m:
            age_note = "（[WARN] exe 比源码旧，先跑 scripts/build-exe.py 再判结论）"
        print(f"[exe] {exe}{age_note}")
        data_dir = pathlib.Path(args.data_dir or (pathlib.Path(os.environ.get("TEMP", ".")) / f"sa-smoke-{int(time.time())}"))
        data_dir.mkdir(parents=True, exist_ok=True)
        # SA-031：只删"本次自己建的"目录。--data-dir 指到已有目录（甚至项目根、家目录）
        # 时，末尾那句 rmtree 会连别人的数据一起删。标记文件是唯一的归属证据。
        try:
            (data_dir / SMOKE_OWNER_MARK).write_text(str(os.getpid()), encoding="utf-8")
        except OSError as exc:
            print(f"[WARN] 无法写入归属标记（{exc}），本次将不自动清理数据目录：{data_dir}")
        log_path = data_dir.with_suffix(".log")
        env = dict(os.environ)
        env["SA_DATA_DIR"] = str(data_dir)
        env["SA_BIND"] = f"127.0.0.1:{args.port}"
        # 显式 sim：默认后端已改为 process（H2 默认诚实），本门禁测治理链路而非真实执行，
        # 真实执行由 prove-real-exec / probe_llm_* 专门覆盖。
        env["SA_RUNNER"] = "sim"
        # 注意：src/config.rs 强制 SA_METRICS_TOKEN 长度 >= 24，否则进程启动即退出
        env["SA_METRICS_TOKEN"] = "smoke-metrics-token-0123456789abcdef"
        # 注册默认已关闭（FIX-002：原来默认开放 = 谁先连上谁就是 owner）。
        # 冒烟要造 owner + 第二个 member 两个账号，属于测试夹具而不是"开放注册"，
        # 所以这里显式打开——门禁若依赖"默认开着"就是在给漏洞当保险丝。
        env["SA_ALLOW_REGISTRATION"] = "1"
        env.update(extra_env)
        logf = open(log_path, "w", encoding="utf-8", errors="replace")
        proc = subprocess.Popen([exe], env=env, stdout=logf, stderr=subprocess.STDOUT)
        base = f"http://127.0.0.1:{args.port}"
    else:
        base = args.base

    c = Client(base)
    stamp = uuid.uuid4().hex[:8]
    metrics_token = "smoke-metrics-token-0123456789abcdef"

    try:
        ok, ready_body = wait_ready(c)
        rec("S01 服务在 60 秒内就绪", ok, f"/readyz {json.dumps(ready_body, ensure_ascii=False)[:120] if ready_body else '无响应'}")
        if not ok:
            return 1
        storage = (ready_body or {}).get("storage", "unknown")
        durable = (ready_body or {}).get("storage_durable", "unknown")
        rec(
            "S01b 存储后端可识别",
            storage in ("json", "memory"),
            f"storage={storage} durable={durable}",
        )

        # ---------- A. 存活 / 就绪 / 指标 ----------
        s, _, b = c("GET", "/healthz")
        expect("S02 healthz 200 且 status=ok", (s, (b or {}).get("status")), [(200, "ok")], f"{s}")
        s, _, b = c("GET", "/readyz")
        expect("S03 readyz 200 且 ready=true", (s, (b or {}).get("ready")), [(200, True)])
        rec("S04 readyz 返回 schema_version=1", (b or {}).get("schema_version") == 1, f"实际={ (b or {}).get('schema_version') }")

        s, _, b = c("GET", "/metrics")
        expect("S05 /metrics 无凭据必须 401（不是裸奔）", s, [401], json.dumps(b, ensure_ascii=False)[:80])
        s, h, t = c("GET", "/metrics", token=metrics_token, raw=True)
        ct = (h or {}).get("content-type", "")
        rec("S06 /metrics 带 SA_METRICS_TOKEN 返回 200", s == 200, f"实际={s}")
        rec("S07 /metrics Content-Type 是 Prometheus 文本", "text/plain" in ct, f"实际={ct}")
        rec("S08 /metrics 正文含 sa_uptime_seconds", "sa_uptime_seconds" in t, f"正文前 60 字={t[:60]!r}")
        s, _, _ = c("GET", "/metrics", token="wrong-token")
        expect("S09 /metrics 错误静态令牌必须 401", s, [401])

        # ---------- B. 认证与令牌 ----------
        owner_name = f"smoke_owner_{stamp}"
        owner_pw = "Sm0ke!Passw0rd"
        s, _, b = c("POST", "/api/v1/auth/register", {
            "username": owner_name, "email": f"{owner_name}@example.com",
            "password": owner_pw, "display_name": "Smoke Owner"})
        expect("S10 首个注册用户 201", s, [201], json.dumps(b, ensure_ascii=False)[:100] if s != 201 else "")
        owner = (b or {}).get("user", {})
        tok = (b or {}).get("tokens", {}).get("access_token")
        rtok = (b or {}).get("tokens", {}).get("refresh_token")
        rec("S11 首个用户角色是 owner", owner.get("role") == "owner", f"实际={owner.get('role')}")
        rec("S12 注册响应带 onboarding_status=not_started", (b or {}).get("onboarding_status") == "not_started",
            f"实际={(b or {}).get('onboarding_status')}")
        rec("S13 令牌有效期 expires_in>0", ((b or {}).get("tokens", {}) or {}).get("expires_in", 0) > 0,
            f"实际={((b or {}).get('tokens', {}) or {}).get('expires_in')}")

        s, _, b = c("POST", "/api/v1/auth/register", {
            "username": owner_name, "email": f"other_{owner_name}@example.com", "password": owner_pw})
        expect("S14 重复用户名被拒", s, [409], json.dumps(b, ensure_ascii=False)[:80])
        s, _, b = c("POST", "/api/v1/auth/register", {
            "username": f"weak_{stamp}", "email": f"weak_{stamp}@example.com", "password": "123"})
        expect("S15 弱密码被拒（422）", s, [422], json.dumps(b, ensure_ascii=False)[:100])

        s, _, b = c("POST", "/api/v1/auth/login", {"account": owner_name, "password": owner_pw})
        expect("S16 正确密码登录 200", s, [200])
        rec("S17 登录后 last_login_at 被写入", bool(((b or {}).get("user") or {}).get("last_login_at")),
            f"实际={((b or {}).get('user') or {}).get('last_login_at')}")
        s, _, b = c("POST", "/api/v1/auth/login", {"account": owner_name, "password": "definitely-wrong"})
        expect("S18 错误密码 401", s, [401], json.dumps(b, ensure_ascii=False)[:80])

        s, _, b = c("GET", "/api/v1/auth/me", token=tok)
        expect("S19 带令牌取 /auth/me 200", s, [200])
        rec("S20 /auth/me 返回本人用户名", (b or {}).get("username") == owner_name, f"实际={(b or {}).get('username')}")
        s, _, _ = c("GET", "/api/v1/auth/me")
        expect("S21 无令牌访问 /auth/me 必须 401", s, [401])
        s, _, _ = c("GET", "/api/v1/auth/me", token=rtok)
        expect("S22 用 refresh_token 冒充 access_token 必须 401（类型校验）", s, [401])

        s, _, b = c("POST", "/api/v1/auth/refresh", {"refresh_token": rtok})
        expect("S23 刷新令牌 200", s, [200])
        new_rtok = ((b or {}).get("tokens") or {}).get("refresh_token")
        new_tok = ((b or {}).get("tokens") or {}).get("access_token")
        rec("S24 刷新后 refresh_token 轮换（新旧不同）", bool(new_rtok) and new_rtok != rtok)
        s, _, _ = c("POST", "/api/v1/auth/refresh", {"refresh_token": rtok})
        expect("S25 旧 refresh_token 复用必须 401（轮换即作废）", s, [401], "这是防重放的关键")
        if new_tok:
            tok = new_tok

        # ---------- C. 新手教程 ----------
        s, _, b = c("GET", "/api/v1/onboarding", token=tok)
        expect("S26 onboarding 总览 200", s, [200])
        rec("S27 初始状态 not_started", (b or {}).get("status") == "not_started", f"实际={(b or {}).get('status')}")
        rec("S28 必做步骤数=5", (b or {}).get("total_required") == 5, f"实际={(b or {}).get('total_required')}")
        rec("S29 步骤清单 6 条（含非必做）", len((b or {}).get("steps") or []) == 6,
            f"实际={len((b or {}).get('steps') or [])}")

        s, _, b = c("POST", "/api/v1/onboarding/steps/welcome/complete", token=tok)
        expect("S30 完成 welcome 200", s, [200])
        rec("S31 首次完成 changed=true", (b or {}).get("changed") is True, f"实际={(b or {}).get('changed')}")
        s, _, b = c("POST", "/api/v1/onboarding/steps/welcome/complete", token=tok)
        rec("S32 重复完成 changed=false（幂等）", (b or {}).get("changed") is False, f"实际={(b or {}).get('changed')}")
        s, _, b = c("POST", "/api/v1/onboarding/steps/not-a-step/complete", token=tok)
        expect("S33 未知步骤 422", s, [422], json.dumps(b, ensure_ascii=False)[:80])
        s, _, b = c("POST", "/api/v1/onboarding/skip", token=tok)
        expect("S34 跳过教程 200", s, [200])
        rec("S35 跳过后 skipped=true", ((b or {}).get("onboarding") or b or {}).get("skipped") is True,
            f"实际={((b or {}).get('onboarding') or b or {}).get('skipped')}")
        s, _, b = c("POST", "/api/v1/onboarding/reset", token=tok)
        expect("S36 重置教程 200", s, [200])
        rec("S37 重置后回到 not_started", ((b or {}).get("onboarding") or b or {}).get("status") == "not_started",
            f"实际={((b or {}).get('onboarding') or b or {}).get('status')}")

        # ---------- D. 记忆 ----------
        key = f"smoke.key.{stamp}"
        s, _, b = c("POST", "/api/v1/memories", {"scope": "private", "key": key, "content": "v1",
                                                 "tags": ["smoke"], "confidence": 0.9}, token=tok)
        expect("S38 新建记忆 201", s, [201], json.dumps(b, ensure_ascii=False)[:100] if s != 201 else "")
        rec("S39 新建时 created=true", (b or {}).get("created") is True, f"实际={(b or {}).get('created')}")
        mid = ((b or {}).get("entry") or {}).get("id")
        rec("S40 返回 entry.version=1", ((b or {}).get("entry") or {}).get("version") == 1,
            f"实际={((b or {}).get('entry') or {}).get('version')}")

        s, _, b = c("POST", "/api/v1/memories", {"scope": "private", "key": key, "content": "v2"}, token=tok)
        expect("S41 同 key 再提交 200（覆盖而非新建）", s, [200], f"实际={s}")
        rec("S42 覆盖时 created=false 且 version=2",
            (b or {}).get("created") is False and ((b or {}).get("entry") or {}).get("version") == 2,
            f"created={(b or {}).get('created')} version={((b or {}).get('entry') or {}).get('version')}")

        s, _, b = c("GET", f"/api/v1/memories?limit=50", token=tok)
        expect("S43 记忆列表 200", s, [200])
        items = (b or {}).get("items") or []
        rec("S44 列表包含刚写入的 key", any(i.get("key") == key for i in items), f"共 {len(items)} 条")

        # ---------- D2. 记忆列表过滤/分页（服务端筛选真实生效） ----------
        s, _, b = c("GET", f"/api/v1/memories?q={key}&limit=50", token=tok)
        hit = (b or {}).get("items") or []
        rec("S44b 记忆 q 关键字过滤命中", s == 200 and any(i.get("key") == key for i in hit),
            f"共 {len(hit)} 条")
        s, _, b = c("GET", "/api/v1/memories?q=zzz-no-such-key-zzz&limit=50", token=tok)
        rec("S44c 记忆 q 过滤能排除", s == 200 and len((b or {}).get("items") or []) == 0,
            f"共 {len((b or {}).get('items') or [])} 条")
        s, _, b = c("GET", "/api/v1/memories?scope=private&limit=50", token=tok)
        sc = (b or {}).get("items") or []
        rec("S44d scope=private 过滤真生效", s == 200 and all(i.get("scope") == "private" for i in sc),
            f"共 {len(sc)} 条")
        c("POST", "/api/v1/memories", {"scope": "private", "key": f"{key}.page2", "content": "p2"}, token=tok)
        s, _, b = c("GET", "/api/v1/memories?limit=1&offset=0", token=tok)
        p1 = [i.get("id") for i in ((b or {}).get("items") or [])]
        s, _, b = c("GET", "/api/v1/memories?limit=1&offset=1", token=tok)
        p2 = [i.get("id") for i in ((b or {}).get("items") or [])]
        c("POST", "/api/v1/memories", {"scope": "private", "key": f"{key}.tagged",
                                        "content": "这条靠标签才搜得到", "tags": ["财报搜专用"]}, token=tok)
        s, _, b = c("GET", "/api/v1/memories?q=" + urllib.parse.quote("财报搜专用"), token=tok)
        hit = [i for i in ((b or {}).get("items") or []) if i.get("key") == f"{key}.tagged"]
        rec("S44f 关键词能命中 tags（不只是 key/content）", s == 200 and len(hit) == 1,
            f"命中 {len(hit)} 条")
        rec("S44e 记忆分页真翻页且两页不重叠", bool(p1) and bool(p2) and set(p1).isdisjoint(set(p2)),
            f"{p1} / {p2}")

        s, _, b = c("GET", f"/api/v1/memories/{mid}", token=tok)
        expect("S45 记忆详情 200", s, [200])
        rec("S46 详情内容是 v2", (b or {}).get("content") == "v2", f"实际={(b or {}).get('content')}")

        s, _, b = c("PUT", f"/api/v1/memories/{mid}", {"content": "v3", "tags": ["x"]}, token=tok)
        expect("S47 更新记忆 200", s, [200])
        rec("S48 更新后 version=3", (b or {}).get("version") == 3, f"实际={(b or {}).get('version')}")

        s, _, b = c("POST", "/api/v1/memories", {"scope": "not-a-scope", "key": "x", "content": "y"}, token=tok)
        expect("S49 非法 scope 422", s, [422], json.dumps(b, ensure_ascii=False)[:100])

        s, _, _ = c("DELETE", f"/api/v1/memories/{mid}", token=tok)
        expect("S50 删除记忆 204", s, [204], f"实际={s}")
        s, _, _ = c("GET", f"/api/v1/memories/{mid}", token=tok)
        expect("S51 删除后详情 404", s, [404], f"实际={s}")

        # ---------- E. 能力目录 ----------
        s, _, b = c("GET", "/api/v1/agents", token=tok)
        expect("S52 子 agent 目录 200", s, [200])
        agents = b if isinstance(b, list) else []
        rec("S53 目录非空且含 super-agent", any(a.get("id") == "super-agent" for a in agents), f"共 {len(agents)} 个")
        s, _, b = c("GET", "/api/v1/agents/super-agent", token=tok)
        expect("S54 agent 详情 200", s, [200])
        s, _, b = c("GET", "/api/v1/agents/nope", token=tok)
        expect("S55 未知 agent 404", s, [404], json.dumps(b, ensure_ascii=False)[:80])
        s, _, b = c("GET", "/api/v1/capabilities", token=tok)
        caps = b if isinstance(b, list) else []
        expect("S56 能力目录 200", s, [200])
        rec("S57 能力目录非空", len(caps) > 0, f"共 {len(caps)} 项")
        s, _, _ = c("GET", "/api/v1/capabilities/nope", token=tok)
        expect("S58 未知 capability 404", s, [404])

        # ---------- F. 任务 ----------
        s, _, b = c("POST", "/api/v1/tasks", {"mode": "sync", "goal": "把冒烟测试跑通并核对返回码", "task_type": "coding",
                                              "model_profile": "fast", "budget_tokens": 20000}, token=tok)
        expect("S59 提交任务 201", s, [201], json.dumps(b, ensure_ascii=False)[:120] if s != 201 else "")
        task = b or {}
        tid = task.get("id")
        rec("S60 任务同步执行完（status=succeeded）", task.get("status") == "succeeded", f"实际={task.get('status')}")
        rec("S61 任务带计划 plan 且非空", len(task.get("plan") or []) > 0, f"步骤数={len(task.get('plan') or [])}")
        rec("S62 计划里每步都有 agent_id", all(p.get("agent_id") for p in (task.get("plan") or [])),
            f"示例={((task.get('plan') or [{}])[0]).get('agent_id')}")

        s, _, b = c("GET", f"/api/v1/tasks/{tid}", token=tok)
        expect("S63 任务详情 200", s, [200])
        s, _, b = c("GET", f"/api/v1/tasks/{tid}/trace", token=tok)
        expect("S64 任务 trace 200", s, [200])
        trace = b if isinstance(b, list) else []
        rec("S65 trace 非空且含 execute 阶段", any(e.get("phase") == "execute" for e in trace), f"条目数={len(trace)}")
        rec("S66 trace 累计 tokens>0", sum(e.get("tokens", 0) for e in trace) > 0,
            f"累计={sum(e.get('tokens', 0) for e in trace)}")

        s, _, b = c("GET", "/api/v1/tasks?limit=10", token=tok)
        expect("S67 任务列表 200", s, [200])
        rec("S68 列表包含刚提交的任务", any(t.get("id") == tid for t in ((b or {}).get("items") or [])),
            f"共 {(b or {}).get('total')} 个")

        # ---------- F2. 任务列表过滤与分页（服务端筛选真实生效） ----------
        s, _, b = c("GET", "/api/v1/tasks?status=succeeded&limit=50", token=tok)
        ti = (b or {}).get("items") or []
        rec("S68b status=succeeded 过滤真生效", s == 200 and len(ti) > 0
            and all(t.get("status") == "succeeded" for t in ti), f"共 {len(ti)} 条")
        rec("S68c 过滤结果仍含刚提交任务", any(t.get("id") == tid for t in ti), "")
        s, _, b = c("GET", "/api/v1/tasks?status=failed&limit=50", token=tok)
        tf = (b or {}).get("items") or []
        rec("S68d status=failed 不会混入成功任务",
            s == 200 and all(t.get("status") == "failed" for t in tf)
            and not any(t.get("id") == tid for t in tf), f"共 {len(tf)} 条")
        s, _, b = c("GET", "/api/v1/tasks?status=bogus", token=tok)
        expect("S68e 非法 status 被 422 拒绝（不是静默忽略）", s, [422],
               json.dumps(b, ensure_ascii=False)[:100])
        s, _, b = c("GET", "/api/v1/tasks?q=%E5%86%92%E7%83%9F%E6%B5%8B%E8%AF%95%E8%B7%91%E9%80%9A&limit=50",
                    token=tok)
        tq = (b or {}).get("items") or []
        rec("S68f 任务 q 关键字过滤命中", s == 200 and any(t.get("id") == tid for t in tq), f"共 {len(tq)} 条")
        s, _, b = c("GET", "/api/v1/tasks?q=zzz-no-such-goal-zzz&limit=50", token=tok)
        rec("S68g 任务 q 过滤能排除", s == 200 and len((b or {}).get("items") or []) == 0,
            f"共 {len((b or {}).get('items') or [])} 条")
        s, _, b = c("GET", "/api/v1/tasks?task_type=coding&limit=50", token=tok)
        tt = (b or {}).get("items") or []
        rec("S68h task_type=coding 过滤真生效",
            s == 200 and all(t.get("task_type") == "coding" for t in tt), f"共 {len(tt)} 条")
        c("POST", "/api/v1/tasks", {"mode": "sync", "goal": "第二个任务用于验证分页翻页", "task_type": "coding",
                                    "budget_tokens": 20000}, token=tok)
        s, _, b = c("GET", "/api/v1/tasks?status=succeeded&limit=1&offset=0", token=tok)
        q1 = [t.get("id") for t in ((b or {}).get("items") or [])]
        s, _, b = c("GET", "/api/v1/tasks?status=succeeded&limit=1&offset=1", token=tok)
        q2 = [t.get("id") for t in ((b or {}).get("items") or [])]
        rec("S68i 任务分页真翻页且两页不重叠", bool(q1) and bool(q2) and set(q1).isdisjoint(set(q2)),
            f"{q1} / {q2}")

        s, _, b = c("POST", "/api/v1/tasks", {"mode": "sync", "goal": ""}, token=tok)
        expect("S69 空目标被拒 422", s, [422], json.dumps(b, ensure_ascii=False)[:100])
        s, _, b = c("GET", "/api/v1/tasks/no-such-task", token=tok)
        expect("S70 未知任务 404", s, [404], json.dumps(b, ensure_ascii=False)[:80])

        s, _, b = c("POST", "/api/v1/tasks", {"mode": "sync", "goal": "预算压到最低的可执行任务", "task_type": "coding",
                                              "budget_tokens": 100}, token=tok)
        rec("S71a 预算低于下限（100<200）被 422 拒绝", s == 422,
            json.dumps(b, ensure_ascii=False)[:100])
        s, _, b = c("POST", "/api/v1/tasks", {"mode": "sync", "goal": "预算压到最低的可执行任务", "task_type": "coding",
                                              "budget_tokens": 200}, token=tok)
        st = (b or {}).get("status")
        expect("S71b 预算 200 触发 aborted_budget（不是静默降级放行）", st, ["aborted_budget"],
               f"实际状态={st}")
        rec("S71c 中止时 result_summary 写明原因",
            "预算" in ((b or {}).get("result_summary") or ""), "summary 含『预算』字样")
        s, _, b = c("GET", "/api/v1/tasks?status=aborted_budget&limit=50", token=tok)
        ab = (b or {}).get("items") or []
        rec("S71d 中止任务能被 status=aborted_budget 过滤出来（非空且状态一致）",
            s == 200 and len(ab) > 0 and all(x.get("status") == "aborted_budget" for x in ab),
            f"共 {len(ab)} 条")

        # ---------- G. 多用户隔离与 RBAC ----------
        m_name = f"smoke_member_{stamp}"
        s, _, b = c("POST", "/api/v1/auth/register", {
            "username": m_name, "email": f"{m_name}@example.com", "password": owner_pw})
        expect("S72 第二个用户注册 201", s, [201])
        mtok = ((b or {}).get("tokens") or {}).get("access_token")
        rec("S73 第二个用户角色是 member", ((b or {}).get("user") or {}).get("role") == "member",
            f"实际={((b or {}).get('user') or {}).get('role')}")

        s, _, b = c("GET", f"/api/v1/tasks/{tid}", token=mtok)
        expect("S74 member 读他人任务被拒（404 或 403）", s, [403, 404], json.dumps(b, ensure_ascii=False)[:100])
        s, _, b = c("POST", "/api/v1/tasks", {"mode": "sync", "goal": "整理本周工作项并形成周报", "task_type": "writing"}, token=mtok)
        expect("S75 member 可提交自己的任务 201", s, [201])
        s, _, b = c("GET", "/api/v1/observability/overview", token=mtok)
        expect("S76 member 访问管理员概览被拒（403 或 422）", s, [403, 422],
               json.dumps(b, ensure_ascii=False)[:100] + " ← 若返回 422 说明越权与校验仍共用错误码")
        s, _, b = c("POST", "/api/v1/memories", {"scope": "shared", "key": f"x.{stamp}", "content": "y"}, token=mtok)
        expect("S77b member 可写 shared 记忆（rbac 矩阵允许）", s, [201], json.dumps(b, ensure_ascii=False)[:100])
        s, _, b = c("POST", "/api/v1/memories", {"scope": "system", "key": f"sys.{stamp}", "content": "y"}, token=mtok)
        expect("S77c member 写 system 记忆必须 403", s, [403], json.dumps(b, ensure_ascii=False)[:120])
        s, _, b = c("GET", f"/api/v1/memories?scope=private&limit=50", token=mtok)
        mine = (b or {}).get("items") or []
        rec("S77d member 看不到 owner 的私有记忆", all(m.get("scope") != "private" or m.get("own") for m in mine),
            f"返回 {len(mine)} 条")

        # ---------- H. 可观测 ----------
        s, _, b = c("GET", "/api/v1/observability/overview", token=tok)
        expect("S78 管理员概览 200", s, [200])
        ov = b or {}
        # 断言"概览与目录自洽"，而不是写死一个数字：写死的坏处是改回旧值就绿了，
        # 等于测了个常量；漂移恰恰是这类数字最容易犯的病。
        rec("S79 概览里 agents_total 与 agent 目录一致",
            ov.get("agents_total") == len(agents),
            f"概览={ov.get('agents_total')} 目录={len(agents)}")
        _agent_ids = {a.get("id") for a in agents}
        # 断言分三截：新增的必须在、裁撤的必须不在。"只在"不写死总数——总数会随
        # 每次立册/裁撤变动，写死等于每次都要改断言；写"必须在/必须不在"才守得住。
        rec("S79b 七个垂直域已上线、collab 与运维 agent 已撤并",
            {"game-agent", "finance-agent", "legal-agent", "data-agent",
             "narrative-agent", "market-agent", "qa-agent"} <= _agent_ids
            and "collab-agent" not in _agent_ids
            and "ops-agent" not in _agent_ids,
            f"目录={sorted(i for i in _agent_ids if i)}")
        rec("S80 概览里 capabilities_total 与目录一致",
            ov.get("capabilities_total") == len(caps), f"概览={ov.get('capabilities_total')} 目录={len(caps)}")
        rec("S80b 概览报出当前审计部件（可插拔可视化）",
            ov.get("audit_backend") == "memory", f"实际={ov.get('audit_backend')}")
        rec("S80c 概览报出当前执行部件（可插拔可视化）",
            ov.get("runner_backend") == "sim", f"实际={ov.get('runner_backend')}")
        s, _, b = c("GET", "/api/v1/observability/audit?limit=20", token=tok)
        expect("S81 审计日志 200", s, [200])
        rec("S82 审计日志非空（鉴权/任务都留痕）", len((b or {}).get("items") or []) > 0,
            f"共 {(b or {}).get('total')} 条，其中拒绝 {(b or {}).get('denied')} 条")

        # ---------- H2. 审计过滤（安全排查要能只看被拒记录） ----------
        s, _, b = c("GET", "/api/v1/observability/audit?decision=deny&limit=50", token=tok)
        dn = (b or {}).get("items") or []
        rec("S82b decision=deny 过滤真生效（只看被拒）",
            s == 200 and all(e.get("decision") == "deny" for e in dn), f"共 {len(dn)} 条")
        s, _, b = c("GET", "/api/v1/observability/audit?decision=allow&limit=50", token=tok)
        al = (b or {}).get("items") or []
        rec("S82c decision=allow 过滤真生效",
            s == 200 and all(e.get("decision") == "allow" for e in al), f"共 {len(al)} 条")
        s, _, b = c("GET", "/api/v1/observability/audit?decision=bogus", token=tok)
        expect("S82d 非法 decision 被 422 拒绝（与任务/记忆列表口径一致）", s, [422],
               json.dumps(b, ensure_ascii=False)[:100])
        s, _, b = c("GET", "/api/v1/observability/audit?limit=1&offset=0", token=tok)
        a1 = (b or {})
        rec("S82e 审计分页 has_more 字段可信",
            "has_more" in a1 and (a1.get("has_more") is True) == (a1.get("total", 0) > 1),
            f"total={a1.get('total')} has_more={a1.get('has_more')}")

        s, h, t = c("GET", "/metrics", token=metrics_token, raw=True)
        rec("S83 metrics 输出注册计数器（sa_registrations_total）", "sa_registrations_total" in t,
            "由 services/auth.rs 打点")
        rec("S83b metrics 输出执行后端标签（证明 StepRunner 已接线）",
            'sa_executor_backend_total{backend="sim"}' in t, "orchestration/executor.rs")
        rec("S83c metrics 输出任务启动计数器", "sa_tasks_started_total" in t, "")
        import re as _re
        http_lines = [ln for ln in t.splitlines() if ln.startswith("sa_http_requests_total{")]
        rec("S83d sa_http_requests_total 已接线（有真实生产调用点，不再是死指标）",
            len(http_lines) > 0 and "sa_http_requests_total" in t, f"共 {len(http_lines)} 条序列")
        rec("S83e 计数标签按状态码分桶（能看出 200/401/404 分别多少）",
            any('status="200"' in ln for ln in http_lines)
            and any('status="401"' in ln or 'status="404"' in ln for ln in http_lines),
            f"样本={http_lines[:2]}")
        rec("S83f 路由标签已折叠（不含 uuid 原值，避免监控序列爆炸）",
            len(http_lines) > 0
            and not any(_re.search(r'route="[^"]*[0-9a-f]{8}-[0-9a-f]{4}-', ln) for ln in http_lines)
            and any(":id" in ln or "tasks" in ln for ln in http_lines),
            "含 :id 折叠" if any(":id" in ln for ln in http_lines) else "无 id 形态，仅校验未泄漏 uuid")
        rec("S83g 404 兜底也被计数（前端写错路径能查到）",
            any('status="404"' in ln for ln in http_lines), "S12 已制造过 404")

        # ---------- I. 会话撤销 ----------
        s, _, b = c("POST", "/api/v1/auth/logout", {"refresh_token": new_rtok}, token=tok)
        expect("S84 登出 200", s, [200])
        rec("S85 登出撤销了会话（revoked_sessions>=1）", (b or {}).get("revoked_sessions", 0) >= 1,
            f"实际={(b or {}).get('revoked_sessions')}")
        s, _, _ = c("POST", "/api/v1/auth/refresh", {"refresh_token": new_rtok})
        expect("S86 登出后该 refresh_token 失效（401）", s, [401])

        # S86b~e：登出撤掉的不只是 refresh，access 也必须当场失效。
        # 用一条独立会话验证——直接撤主令牌会把后面几十条用例全打成 401，
        # 那样红的是门禁，不是被测代码。
        s, _, b = c("POST", "/api/v1/auth/login", {"account": owner_name, "password": owner_pw})
        side_tok = ((b or {}).get("tokens") or {}).get("access_token")
        side_rtok = ((b or {}).get("tokens") or {}).get("refresh_token")
        s, _, _ = c("GET", "/api/v1/auth/me", token=side_tok)
        expect("S86b 旁路会话登录后 access 可用（对照组）", s, [200])
        s, _, _ = c("POST", "/api/v1/auth/logout", {"refresh_token": side_rtok}, token=side_tok)
        expect("S86c 登出旁路会话 200", s, [200])
        s, _, b = c("GET", "/api/v1/auth/me", token=side_tok)
        rec("S86d 登出后旧 access 立即失效（401，而非硬撑到过期）", s == 401,
            f"实际={s} {json.dumps(b, ensure_ascii=False)[:90]}")
        # S86e：S84 登出的正是主会话本身，按"登出即撤整条会话"的语义，
        # 主 access 必须当场 401。旧断言"主令牌仍可用"是"登出只撤 refresh"的旧行为，
        # 那等于让 access 变成撤不掉的令牌，不能留。后续用例重新登录后再跑。
        s, _, _ = c("GET", "/api/v1/auth/me", token=tok)
        rec("S86e 被登出的那条会话 access 也失效（401；撤销粒度是整条会话）", s == 401, f"实际={s}")
        s, _, b = c("POST", "/api/v1/auth/login", {"account": owner_name, "password": owner_pw})
        fresh = (b or {}).get("tokens") or {}
        if fresh.get("access_token"):
            tok = fresh["access_token"]
        if fresh.get("refresh_token"):
            new_rtok = fresh["refresh_token"]
        s, _, _ = c("GET", "/api/v1/auth/me", token=tok)
        rec("S86f 重新登录立即生效（撤会话不影响后继登录）", s == 200, f"实际={s}")

        # ---------- I2. 参数与错误口径（越界必拒、中文可展示、不泄漏内部细节） ----------
        s, _, b = c("GET", "/api/v1/memories?limit=0", token=tok)
        blob = json.dumps(b, ensure_ascii=False)
        rec("S93 分页 limit=0 明确 422 而非静默改成 1", s == 422, f"实际={s}")
        rec("S94 分页越界错误回报允许范围（1..=200）", "1..=200" in blob, blob[:120])
        s, _, b = c("GET", "/api/v1/memories?limit=-1", token=tok)
        blob = json.dumps(b, ensure_ascii=False)
        rec(
            "S95 负数 limit 报中文字段级错误且不泄漏 serde 英文",
            s == 422 and "invalid digit" not in blob and "必须是非负整数" in blob,
            f"实际={s} {blob[:110]}",
        )
        s, _, b = c("GET", "/api/v1/memories?limit=100000", token=tok)
        blob = json.dumps(b, ensure_ascii=False)
        rec("S96 超上限 limit 明确 422 并回报收到值", s == 422 and "100000" in blob, f"实际={s}")

        # ---------- K. 异步执行与取消（协作式取消的真实链路） ----------
        # 说明：默认模拟执行是零耗时的，异步任务 20ms 内就跑完，取消永远打不中。
        # 因此这里用 goal 里的 "slow-step" 注入慢步（仅 sim 后端生效），
        # 否则这一段只是在测"取消一个已完成任务是 409"，属于自欺欺人。
        s, _, b = c("POST", "/api/v1/tasks", {"goal": "slow-step 异步任务用于验证取消真能生效",
                                              "task_type": "coding", "model_profile": "fast",
                                              "budget_tokens": 20000, "mode": "async"}, token=tok)
        cid = (b or {}).get("id")
        rec("S97 async 提交返回 202/201 且状态为 queued", s in (200, 201, 202) and (b or {}).get("status") == "queued",
            f"实际={s} status={(b or {}).get('status')}")
        s, _, b = c("POST", f"/api/v1/tasks/{cid}/cancel", token=tok)
        rec("S98 取消执行中的任务返回 202（立旗，不是假装已完成）", s == 202,
            f"实际={s} {json.dumps(b, ensure_ascii=False)[:110]}")
        canc_trace = [e for e in ((b or {}).get("trace") or []) if e.get("phase") == "cancel"]
        rec("S98b 取消请求写进了 trace（phase=cancel/requested）",
            any(e.get("outcome") == "requested" for e in canc_trace), f"cancel 条目={len(canc_trace)}")

        final = None
        for _ in range(60):
            s, _, b = c("GET", f"/api/v1/tasks/{cid}", token=tok)
            final = (b or {}).get("status")
            if final in ("cancelled", "succeeded", "failed", "partially_succeeded", "aborted_budget"):
                break
            time.sleep(0.1)
        rec("S99 取消后任务最终收敛为 cancelled（不是 succeeded/failed）", final == "cancelled",
            f"实际终态={final}")
        rec("S99b 取消任务的 result_summary 讲清了原因",
            "取消" in ((b or {}).get("result_summary") or ""), ((b or {}).get("result_summary") or "")[:80])

        s, _, b = c("GET", "/api/v1/tasks?status=cancelled&limit=50", token=tok)
        cl = (b or {}).get("items") or []
        rec("S99c 已取消任务能被 status=cancelled 过滤出来",
            s == 200 and any(x.get("id") == cid for x in cl), f"共 {len(cl)} 条")

        s, _, b = c("POST", f"/api/v1/tasks/{cid}/cancel", token=tok)
        rec("S99d 对已终结任务再取消返回 409（不返回 200 骗人）", s == 409,
            f"实际={s} {json.dumps(b, ensure_ascii=False)[:90]}")
        s, _, b = c("POST", "/api/v1/tasks/no-such-task/cancel", token=tok)
        rec("S99e 取消不存在的任务返回 404", s == 404, f"实际={s}")
        s, _, b = c("POST", f"/api/v1/tasks/{cid}/cancel", token=mtok)
        rec("S99f 他人无法取消我的任务（403/404）", s in (403, 404), f"实际={s}")

        # 写穿：任务/记忆一落库就刷盘，指标必须能证明它真刷了（不是只写在注释里）
        s, _, mt = c("GET", "/metrics", token=metrics_token, raw=True)
        wt = 0
        for line in (mt or "").splitlines():
            if line.startswith("sa_store_write_through_total"):
                try:
                    wt = float(line.rsplit(" ", 1)[1])
                except Exception:
                    pass
        rec("S99h 写穿落盘计数器已接线且真的刷过盘（sa_store_write_through_total > 0）",
            s == 200 and wt > 0, f"实际={wt}")

        # 同步接口不该声称可取消：sync 提交的任务直接跑完，取消必然 409
        s, _, b = c("POST", "/api/v1/tasks", {"mode": "sync", "goal": "同步任务用于验证不可取消语义",
                                              "task_type": "coding", "budget_tokens": 20000}, token=tok)
        sid = (b or {}).get("id")
        s, _, b = c("POST", f"/api/v1/tasks/{sid}/cancel", token=tok)
        rec("S99g 同步任务跑完后取消返回 409（语义诚实）", s == 409, f"实际={s}")

        # ---------- L. 任务进度实时流（SSE，真连接实测） ----------
        # 用一个独立用户：既避开上一段可能触发的 60/h 配额，也顺便验证"长连接同样按人鉴权"。
        sse_name = f"sse_owner_{stamp}"
        s, _, b = c("POST", "/api/v1/auth/register", {
            "username": sse_name, "email": f"{sse_name}@example.com",
            "password": "Sm0ke!Passw0rd", "display_name": "SSE Owner"})
        stok = (b or {}).get("tokens", {}).get("access_token")
        rec("S100 SSE 测试用户注册成功（拿到独立令牌）", s == 201 and bool(stok), f"实际={s}")

        # slow-step 会让每一步睡 300ms，任务因此有约 1s 的可观测执行期，
        # 否则模拟后端零耗时，永远看不到 running 那一帧。
        s, _, b = c("POST", "/api/v1/tasks", {
            "goal": "slow-step 事件流观测：验证 queued→running→终态三帧",
            "task_type": "coding", "model_profile": "fast",
            "budget_tokens": 20000, "mode": "async"}, token=stok)
        etid = (b or {}).get("id")
        rec("S100b 事件流用的异步任务已受理", s in (200, 201, 202) and bool(etid), f"实际={s}")

        st, ctype, frames, eof1 = sse_read(c, f"/api/v1/tasks/{etid}/events", stok, want=6, timeout=25)
        rec("S101 SSE 建连成功且媒体类型是 text/event-stream",
            st == 200 and ctype.startswith("text/event-stream"),
            f"实际状态={st} Content-Type={ctype or '空'}")
        rec("S101b 首帧是连接时快照（seq=0，不是广播事件）",
            bool(frames) and frames[0].get("seq") == 0 and frames[0].get("task_id") == etid,
            f"首帧={json.dumps(frames[0], ensure_ascii=False)[:120] if frames else '无帧'}")
        seqs = [f.get("seq") for f in frames if isinstance(f.get("seq"), int)]
        statuses = [f.get("status") for f in frames if f.get("status")]
        rec("S101c 事件序号从 1 起严格递增（前端可据此判断漏帧）",
            [q for q in seqs if q != 0] == sorted(set(q for q in seqs if q != 0))
            and all(x >= 1 for x in seqs if x != 0),
            f"seq={seqs}")
        rec("S101d 事件流覆盖 running（真的看到执行中，不是只有终态）",
            "running" in statuses, f"状态序列={statuses}")
        rec("S101e 事件流最终推送终态且与服务端状态一致",
            bool(statuses) and statuses[-1] in
            ("succeeded", "partially_succeeded", "failed", "cancelled", "aborted_budget"),
            f"末帧状态={statuses[-1] if statuses else '无'}")
        rec("S101f 终态之后服务端主动 EOF（不挂住连接）", eof1,
            "读到 HTTP EOF" if eof1 else "未在超时前结束（客户端被挂住）")

        # 已终结的任务：连上应该先给一帧终态快照，随即 EOF
        st2, ctype2, frames2, eof2 = sse_read(c, f"/api/v1/tasks/{etid}/events", stok, want=5, timeout=15)
        rec("S101g 已终结任务订阅：先给快照再立刻 EOF",
            st2 == 200 and len(frames2) == 1 and eof2,
            f"实际状态={st2} 帧数={len(frames2)} EOF={eof2}")

        # 鉴权与越权：长连接不能成为绕过校验的缝
        st3, _, _, _ = sse_read(c, f"/api/v1/tasks/{etid}/events", None, want=1, timeout=8)
        rec("S102 未带令牌订阅事件流被 401 拒绝（长连接同样要鉴权）", st3 == 401, f"实际={st3}")
        st4, _, _, _ = sse_read(c, f"/api/v1/tasks/{etid}/events", mtok, want=1, timeout=8)
        rec("S102b 他人无法订阅我的任务事件流（403/404）", st4 in (403, 404), f"实际={st4}")
        st5, _, _, _ = sse_read(c, "/api/v1/tasks/no-such-task/events", stok, want=1, timeout=8)
        rec("S102c 订阅不存在任务的事件流返回 404", st5 == 404, f"实际={st5}")

        # 取消与事件流的联动：前端点了"取消"之后，靠这条流确认任务真的收敛了，
        # 而不是自己再起一个轮询。这里实测"取消 → 事件流推 cancelled → EOF"整条链。
        s, _, b = c("POST", "/api/v1/tasks", {
            "goal": "slow-step 取消联动：验证事件流能推送 cancelled 并结束",
            "task_type": "coding", "model_profile": "fast",
            "budget_tokens": 20000, "mode": "async"}, token=stok)
        ctid = (b or {}).get("id")
        time.sleep(0.35)  # 让执行器真正进入 running，否则取消打中的是 queued
        s, _, b = c("POST", f"/api/v1/tasks/{ctid}/cancel", token=stok)
        rec("S103 执行中的任务取消返回 202（立旗）", s == 202, f"实际={s}")
        st6, _, frames3, eof3 = sse_read(c, f"/api/v1/tasks/{ctid}/events", stok, want=6, timeout=20)
        st3seq = [f.get("status") for f in frames3 if f.get("status")]
        rec("S103b 取消后事件流推送 cancelled（不是只给个 202 就完事）",
            "cancelled" in st3seq, f"状态序列={st3seq}")
        rec("S103c 取消收敛后事件流主动 EOF", eof3,
            f"帧数={len(frames3)} EOF={eof3}")
        s, _, b = c("GET", f"/api/v1/tasks/{ctid}", token=stok)
        rec("S103d 事件流说的 cancelled 与详情接口一致（无两套真相）",
            (b or {}).get("status") == "cancelled", f"详情状态={(b or {}).get('status')}")

        # 丢帧必须看得见：SSE 缓冲满时丢事件是"绝不阻塞发布者"的必然代价，
        # 但看不见就等于没有——前端偶发跳帧时，得能分清是网络断还是服务端已丢。
        s, _, mt_text = c("GET", "/metrics", token=metrics_token, raw=True)
        rec("S104 /metrics 暴露 SSE 丢事件计数器（慢消费者可被观测）",
            "sa_sse_events_dropped_total" in mt_text, f"实际状态={s}")
        tmpl = re.search(r"^sa_sse_subscribers\s+([0-9.]+)$", mt_text, re.M)
        rec("S104b /metrics 暴露当前 SSE 订阅数（连接已全部结束时应为 0）",
            tmpl is not None and float(tmpl.group(1)) == 0.0,
            f"实际={'未出现' if tmpl is None else tmpl.group(1)}")

        # ---------- J. 任务提交限流 60/h（放最后，会大量消耗配额） ----------
        if args.no_quota:
            print("\n[SKIP] 限流段被 --no-quota 跳过")
        else:
            hit429 = False
            retry_after = None
            success = 0
            for i in range(70):
                s, h, b = c("POST", "/api/v1/tasks", {"mode": "sync", "goal": f"限流探测任务 {i}", "task_type": "writing"}, token=tok)
                if s == 201:
                    success += 1
                    continue
                if s == 429:
                    hit429 = True
                    retry_after = {k.lower(): v for k, v in (h or {}).items()}.get("retry-after")
                    break
                rec(f"限流探测在未预期状态码处中断", False, f"第 {i} 次返回 {s}: {json.dumps(b, ensure_ascii=False)[:120]}")
                break
            rec("S87 连续提交最终命中 429（限流生效）", hit429, f"成功 {success} 次后命中")
            rec("S88 429 带 Retry-After 头（前端可据此退避）", bool(retry_after), f"Retry-After={retry_after}")
            rec("S89 命中前的成功次数不超过 60", success <= 60, f"实际成功={success}（配额 60/h，前面已计入若干次）")

            s, _, b = c("POST", "/api/v1/tasks", {"mode": "sync", "goal": "另一用户不受前一人限流影响", "task_type": "writing"}, token=mtok)
            expect("S90 限流按用户隔离（他人不受影响）", s, [201], f"实际={s}")
            s, h, t = c("GET", "/metrics", token=metrics_token, raw=True)
            rec("S91 限流计数器已接线（sa_task_submit_rate_limited_total 出现）",
                "sa_task_submit_rate_limited_total" in t, "")
            s, _, b = c("POST", "/api/v1/tasks", {"mode": "sync", "goal": "", "task_type": "writing"}, token=tok)
            rec("S92 已限流用户即使请求非法也先返回 429（限流先于校验）", s == 429,
                f"实际={s}（源码 task.rs 把限流放在校验之前，属于已知行为）")

    except BaseException:
        # 中途抛异常时若只打印"合计 X 项 | 通过 X"，读的人会以为全绿——
        # 实际是脚本自己断了。这里先留下原因，再照常收尾与退出非零。
        import traceback

        print("\n[FATAL] 冒烟脚本自身异常中断，以下项目未执行：")
        traceback.print_exc()
        raise
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        # ---------- 汇总 ----------
        total = len(results)
        passed = sum(1 for r in results if r[1] == OK)
        failed = [r for r in results if r[1] == FAIL]
        print("\n" + "=" * 64)
        print(f"合计 {total} 项 | 通过 {passed} | 失败 {len(failed)}")
        if failed:
            print("\n失败项：")
            for n, _, d in failed:
                print(f"  - {n}   {d}")
        if log_path and log_path.exists():
            tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-12:]
            print("\n服务日志尾部：")
            print("\n".join(tail))
        if data_dir and not args.keep:
            # SA-031：两道关——① 目录归属（本次自己建的才删）；② 路径守卫（不在仓库 /
            # 系统目录 / 家目录）。任一不过就不删，并把位置打印出来让人自己看。
            if not (data_dir / SMOKE_OWNER_MARK).exists():
                print(f"\n[SKIP] 数据目录不是本次创建的，保留不删：{data_dir}")
            else:
                try:
                    pathguard.safe_rmtree(data_dir, base=data_dir.parent, what="冒烟数据目录")
                except pathguard.GuardError as exc:
                    print(f"\n[SKIP] {exc}（数据目录已保留：{data_dir}）")
        print("=" * 64)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
