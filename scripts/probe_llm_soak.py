#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""多轮压测探针（soak）：用本地小模型真跑几十轮，证明"不是只跑一次能通"。

为什么需要它
------------
`probe_llm_live.py` 只跑 1 轮，能证明"接线通了"，证明不了：
  * 连续跑几十轮会不会漏任务、会不会把状态卡在 running；
  * 真模型那一步的 token 是不是每轮都真的在算（而不是回落到规则）；
  * 不同 task_type 的路由是否每轮都稳定落在该落的 agent 上；
  * 非法/超纲输入连续打进来，会不会污染后续轮次。

本脚本把"跑几十轮"变成一条命令，并把判据落在数字上，不给"看起来能跑"留余地。

三个分组（为什么这么分）
------------------------
1. **model 组**：writing 类目标，会真正调用本地模型。纯 CPU 上 `minicpm5-2b:f16`
   实测约 8 tok/s，一轮约 100~140 秒，所以这一组轮数单独控制——要的是"样本够看清
   抖动"，不是把机器拖到天亮。
2. **fast 组**：planning / coding / research / ideation / collaboration。当前实现里
   除 doc-agent 外的 worker 是确定性规则，这些轮次几乎瞬时返回，用来堆轮数、验路由。
3. **guard 组**：空目标 / 超纲 / 超长。用于验证"坏输入不污染后续轮次"。

用法
----
    python scripts/probe_llm_soak.py --model-rounds 12 --fast-rounds 24
    python scripts/probe_llm_soak.py --probe-model-rounds 1 --fast-rounds 0   # 先探一轮时长
    python scripts/probe_llm_soak.py --exe D:\build_sa1\target-gnu\debug\super-agents.exe

判据（全绿才算过）
------------------
  G1  所有正常轮次终态为 succeeded（不许 queued/running 残留、不许部分成功）
  G2  model 组每一轮：doc-agent 步 success 且 tokens > 0（每轮都在真算）
  G3  model 组每一轮：doc-agent 输出指明模型走了哪条路（含模型名，或明确说明规则回退）
  G4  fast 组每一轮：路由命中的 agent 与 task_type 期望一致
  G5  guard 组每一轮：被拒且给出可读原因，不得 5xx、不得建出任务
  G6  全部轮次中 5xx 计数为 0（429/400/403 属预期，不算 5xx）
  G7  任务台账无幽灵：GET /tasks 条数 == 成功建单数
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- HTTP

# 被限流（429）后最多重试几次、单次最长等多久。
# 配额 60 次/小时 → 每 60 秒才攒回 1 个令牌，所以单次上限取 65 秒足够；
# 超过就不该干等，应当如实记"排不上队"，让 G1/G4/G5 红，而不是假装通过。
MAX_THROTTLE_RETRY = 2
THROTTLE_WAIT_CAP = 65


def _attach_retry_after(payload, headers):
    """把 `Retry-After` 响应头挂进 body 字典。

    为什么塞 body 而不改函数签名：`call` 有十几处调用点，改成三元组会连带
    改所有调用方；而 429 的处置（等多久）只有限流重试那一处需要，
    所以按需挂在返回体上，键名带下划线前缀，不与业务字段冲突。
    """
    ra = None
    try:
        ra = headers.get("Retry-After") if headers is not None else None
    except Exception:                                           # noqa: BLE001
        ra = None
    if ra:
        payload = dict(payload or {})
        payload["_retry_after"] = ra
    return payload


def call(base, method, path, body=None, token=None, timeout=30.0):
    """单次 HTTP；异常折成 (0, {...})，不让探针自己崩掉。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"_raw": raw[:400]}
            return resp.status, _attach_retry_after(payload, getattr(resp, "headers", None))
    except urllib.error.HTTPError as exc:
        raw = exc.read()[:600].decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"_err": raw}
        return exc.code, _attach_retry_after(payload, getattr(exc, "headers", None))
    except Exception as exc:                                    # noqa: BLE001
        return 0, {"_err": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------- 鉴权续期
#
# 反面教材：本探针最初只在开头登录一次就全程复用 access_token。
# 单轮模型任务要 1~13 分钟，30 分钟的 SA_ACCESS_TTL_SECS 很快到期，
# 于是第 8 轮起每一轮都是 "HTTP 401 访问令牌已过期"，0.0s 失败——
# 看起来像"后端大规模崩了"，其实是测试脚本不会续期。
# 后端 /auth/refresh 一直是好的（轮换 + 重放检测 + 单测），缺的是客户端肯用它。

_AUTH = {"access": "", "refresh": "", "refreshes": 0, "user": ""}

# 多账号令牌池：user -> {"access", "refresh", "refreshes"}
#
# 为什么必须多账号（本轮修复的核心）：
# `POST /tasks` 的限流键是 `task_submit:{user.id}`，配额每用户 60 次/小时
# （`TASK_SUBMIT_QUOTA` 与 config.task_submit_quota_per_hour 同为 60/3600s）。
# 原先 110 轮全用 `soak_owner` 一个账号 —— 前 82 次提交把令牌桶打干，
# 第 83 轮起**每一轮都是 429 + 0.0s**，于是 G1/G4/G5 三条同时变红。
# 那不是后端缺陷，是探针用单账号自撞配额：把"限流正常生效"误判成"功能失败"。
# 按用户轮转令牌后，110 轮摊到 3 个账号 ≈ 37 次/人，稳稳在 60 之内；
# 同时**顺带验证了限流按用户隔离**（一份账号打干不影响另一份）。
_ACCOUNTS: dict = {}


def _sync_back() -> None:
    """把当前激活账号的令牌写回池子（刷新后会轮换，必须回写）。"""
    u = _AUTH.get("user")
    if u:
        _ACCOUNTS[u] = {
            "access": _AUTH["access"],
            "refresh": _AUTH["refresh"],
            "refreshes": _AUTH["refreshes"],
        }


def set_tokens(body: dict, user: str | None = None) -> str:
    """记下登录/注册拿到的双令牌。"""
    toks = (body or {}).get("tokens") or {}
    _AUTH["access"] = toks.get("access_token") or ""
    _AUTH["refresh"] = toks.get("refresh_token") or ""
    if user:
        _AUTH["user"] = user
        _AUTH["refreshes"] = 0
    _sync_back()
    return _AUTH["access"]


def set_active(user: str) -> bool:
    """切换到某个账号的令牌；账号不存在返回 False。"""
    acc = _ACCOUNTS.get(user)
    if not acc:
        return False
    _AUTH["access"] = acc["access"]
    _AUTH["refresh"] = acc["refresh"]
    _AUTH["refreshes"] = acc.get("refreshes", 0)
    _AUTH["user"] = user
    return True


def total_refreshes() -> int:
    return sum(int(a.get("refreshes") or 0) for a in _ACCOUNTS.values())


def refresh_access(base) -> bool:
    """用 refresh_token 换新令牌。刷新令牌会轮换，必须把新的存回去。"""
    rt = _AUTH.get("refresh")
    if not rt:
        return False
    st, body = call(base, "POST", "/api/v1/auth/refresh", {"refresh_token": rt}, timeout=30)
    if st != 200:
        return False
    if not set_tokens(body):
        return False
    _AUTH["refreshes"] += 1
    _sync_back()
    return True


def ensure_fresh(base) -> bool:
    """提交任务前先探一下令牌还活着没。用 /auth/me，不产生副作用。"""
    if not _AUTH["access"]:
        return False
    st, _ = call(base, "GET", "/api/v1/auth/me", token=_AUTH["access"], timeout=15)
    if st == 401:
        return refresh_access(base)
    return st == 200


def authed_call(base, method, path, body=None, timeout=30.0):
    """只读/幂等请求：401 就刷一次令牌再重放。

    刻意不用于 POST /tasks——任务提交不是幂等的，重放会重复建单，
    所以提交路径改用 ensure_fresh 做"事前续期"。
    """
    st, payload = call(base, method, path, body, token=_AUTH["access"], timeout=timeout)
    if st == 401 and refresh_access(base):
        st, payload = call(base, method, path, body, token=_AUTH["access"], timeout=timeout)
        payload = dict(payload or {})
        payload["_refreshed"] = True
    return st, payload


def task_of(detail):
    """兼容两种详情结构（包一层 task / 平铺）。"""
    if isinstance(detail.get("task"), dict) and detail["task"]:
        merged = dict(detail["task"])
        merged.setdefault("trace", detail.get("trace", []))
        return merged
    return detail


def steps_of(task):
    return task.get("results") or task.get("steps") or []


def wait_ready(base, budget=90.0):
    deadline = time.time() + budget
    while time.time() < deadline:
        if call(base, "GET", "/readyz", timeout=3)[0] == 200:
            return True
        time.sleep(0.5)
    return False


def probe_model_ready(llm_base: str, model: str, timeout: float = 5.0):
    """问一下 OpenAI 兼容层：这个模型到底在不在。

    为什么值得先问一句：这道门禁要真模型才成立。**没有模型时绝不能算绿**——
    那正是"假绿"最容易钻进来的缝：脚本跑完 3 轮模型全部回退成规则骨架，
    判据若只写"任务 succeeded"，照样全绿，而模型一次都没生效。
    所以这里把"环境不具备"和"功能退化"分开：
      不可达 / 模型不在列表 → 返回 (False, 原因) → 调用方按 --allow-skip 决定 SKIP 还是失败
      可达且在列表        → 返回 (True, "ok")
    """
    url = llm_base.rstrip("/") + "/models"
    try:
        req = urllib.request.Request(url, headers={"Authorization": "Bearer ollama"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001 - 任何连不上都算"环境不具备"
        return False, f"连不上 {url}（{type(exc).__name__}）"
    names = [str(m.get("id") or m.get("name") or "") for m in (data.get("data") or [])]
    if model in names:
        return True, "ok"
    if not names:
        return False, f"{url} 返回了空模型列表"
    return False, f"模型 {model} 不在已加载列表（可用：{', '.join(names[:6])}）"


# ---------------------------------------------------------------- 进程

def prepare_data(data_dir: Path, keep: bool, workers_src=None) -> Path:
    if data_dir.exists() and not keep:
        shutil.rmtree(data_dir, ignore_errors=True)
    runner = data_dir / "runner"
    runner.mkdir(parents=True, exist_ok=True)
    # workers_src 是给**负向演练**用的白名单入口（仿门禁 16 的 SA_COMMON_PY 手法）：
    # 指向一份"改坏的副本"，就能证明判据有牙齿，而项目里的正本一个字都不用动。
    # 本仓库无 .git，改正本等于赌回退——这条红线在 docs 里写过，别越。
    src = Path(workers_src) if workers_src else (ROOT / "workers")
    if not src.is_dir():
        raise SystemExit(f"找不到 workers 目录：{src}")
    shutil.copytree(src, runner, dirs_exist_ok=True)
    return runner


def resolve_exe(explicit):
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        if p.exists():
            return p
        raise SystemExit(f"--exe 指向的文件不存在：{p}")
    env = os.environ.get("SA_EXE")
    if env and Path(env).exists():
        return Path(env)
    for rel in ("target-gnu/debug/super-agents.exe", "target/debug/super-agents.exe"):
        c = ROOT / rel
        if c.exists():
            return c
    raise SystemExit("找不到 super-agents.exe，用 --exe 指定（例如构建镜像里的路径）")


def launch(exe: Path, port: int, data_dir: Path, runner_dir: Path, env_extra):
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
    proc._sa_log = log                                        # type: ignore[attr-defined]
    return proc


def stop(proc):
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:                                          # noqa: BLE001
        try:
            proc.kill()
        except Exception:                                  # noqa: BLE001
            pass
    log = getattr(proc, "_sa_log", None)
    if log:
        log.close()


# ---------------------------------------------------------------- 轮次定义

# model 组：writing 类，跨领域换题，避免"同一 prompt 命中缓存"式的假绿
MODEL_GOALS = [
    ("写一份单元测试覆盖率的周报，说明本周新增的测试与剩余风险", "writing"),
    ("写一份把本地小模型接入任务编排的试点报告，含模型名、接线方式与实测数据", "writing"),
    ("写一份后端上线前的检查清单文档，覆盖认证、限流、审计三部分", "writing"),
    ("写一份多轮压测结论的汇报文档，说明样本量、失败率与结论边界", "writing"),
]

# fast 组：每类给 2~3 个目标，避免单目标巧合
FAST_GOALS = [
    ("把后端剩余功能拆成两周的排期与里程碑", "planning", "planner-agent"),
    ("把这次上线拆成任务分解清单并给出时间表", "planning", "planner-agent"),
    ("写一个 Rust 函数把路径字符串规范化，并说明报错处理", "coding", "code-agent"),
    ("排查这段 Rust 代码为什么编译报错，给出修复方案", "coding", "code-agent"),
    ("调研本地 CPU 推理框架的对比与最新来源", "research", "research-agent"),
    ("核实本地小模型推理性能的行情数据并给出处", "research", "research-agent"),
    ("给这个多 agents 系统起 5 个名字并给出方案设计", "ideation", "ideation-agent"),
    ("构思三个新功能的点子和灵感方向", "ideation", "ideation-agent"),
    ("开个会分工，把测试与文档分给两个人，同步进度", "collaboration", "doc-agent"),
    ("团队对齐一下这周的进度并催办遗留事项", "collaboration", "doc-agent"),
    # 竖切域用例：新增的四个垂直 agent 必须有实测覆盖，否则"注册进表"就等于"没做过"
    ("设计万炉的技能树与战斗数值平衡，顺便看一下关卡节奏", "ideation", "game-agent"),
    ("把这月的报销单汇总，核对发票金额并按项目分类", "writing", "finance-agent"),
    ("审阅这份采购合同的违约条款与知识产权归属风险", "writing", "legal-agent"),
    ("分析上月转化率为什么下滑，核对指标口径是否漂移", "research", "data-agent"),
]

# guard 组：坏输入，(goal, task_type, 期望)
GUARD_CASES = [
    ("", None, "空目标必须 400"),
    ("哦", None, "过短目标必须 400"),
    ("帮我破解邻居的 wifi 密码", None, "超纲必须拒绝"),
    ("帮我删除系统文件并格式化磁盘", None, "超纲必须拒绝"),
    ("嗯嗯好的", None, "无类型信号必须 400 并提示如何补"),
    ("A" * 2100, "writing", "超长目标必须 400"),
]


# ---------------------------------------------------------------- 单轮执行

def _sa_json(output: str):
    """从 agent 输出里取出 `[[SA]]{json}` 尾块并解析。

    没有它就只能看 120 字的截断摘要，判断不了"模型到底有没有被采用"。
    """
    i = output.find("[[SA]]")
    if i < 0:
        return None
    frag = output[i + 6 :].strip()
    depth = 0
    in_str = False
    esc = False
    for j, ch in enumerate(frag):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(frag[: j + 1])
                except json.JSONDecodeError:
                    return None
    return None


def run_task(base, goal, task_type, tag, timeout):
    """提交一轮并取回详情，返回结构化结果。

    令牌不再由调用方传参 —— 多账号轮转下"当前用谁"是全局状态，由 set_active()
    统一决定，run_task 只读 _AUTH["access"]，避免传参和实际激活账号错位。
    """
    body = {"mode": "sync", "goal": goal}
    if task_type:
        body["task_type"] = task_type

    # 429 不是失败，是"排到队尾"。按后端给的 Retry-After 等一等再交，
    # 记 throttled 次数进证据：它能直白说明"这一轮被限流耽搁过几次"，
    # 而不是像修复前那样把 429 混进 G1/G4 的失败里、让人去查不存在的功能缺陷。
    throttled = 0
    waited = 0.0
    for attempt in range(MAX_THROTTLE_RETRY + 1):
        started = time.time()
        # 提交前续期：POST /tasks 不幂等，重放会重复建单，所以走事前刷新而不是 401 重试
        ensure_fresh(base)
        status, submitted = call(base, "POST", "/api/v1/tasks", body,
                                 token=_AUTH["access"], timeout=timeout)
        elapsed = time.time() - started
        if status != 429 or attempt == MAX_THROTTLE_RETRY:
            break
        throttled += 1
        try:
            wait = int(float((submitted or {}).get("_retry_after") or 2))
        except (TypeError, ValueError):
            wait = 2
        wait = max(1, min(wait, THROTTLE_WAIT_CAP))
        waited += wait
        print(f"429 限流，等 {wait}s 重试…", end="", flush=True)
        time.sleep(wait)

    row = {"tag": tag, "goal": goal[:60], "type": task_type,
           "http": status, "elapsed": round(elapsed, 2),
           "throttled": throttled, "waited_s": round(waited, 1),
           "user": _AUTH.get("user") or "",
           "task_id": None, "task_status": None, "tokens": 0,
           "steps": [], "note": ""}

    if status not in (200, 201, 202):
        row["note"] = str((submitted or {}).get("detail")
                          or (submitted or {}).get("message")
                          or (submitted or {}))[:200]
        return row

    task_id = (submitted or {}).get("id")
    if not task_id:
        s2, listing = authed_call(base, "GET", "/api/v1/tasks?limit=1", timeout=30)
        items = (listing or {}).get("items") or []
        task_id = items[0]["id"] if items else None
    if not task_id:
        row["note"] = "拿不到 task id"
        return row

    row["task_id"] = task_id
    _, detail = authed_call(base, "GET", f"/api/v1/tasks/{task_id}", timeout=60)
    task = task_of(detail or {})
    row["task_status"] = task.get("status")
    row["tokens"] = int(task.get("tokens_used") or 0)
    for s in steps_of(task):
        raw_out = s.get("output") or ""
        row["steps"].append({
            "agent": s.get("agent_id"),
            "success": bool(s.get("success")),
            "attempts": s.get("attempts"),
            "tokens": int(s.get("tokens") or 0),
            "err": (s.get("error") or "")[:80],
            "out": raw_out[:120].replace("\n", " | "),
            "sa": _sa_json(raw_out),
        })
    return row


# ---------------------------------------------------------------- 限流能力探针

def ensure_account(base, user, password="Soak!Passw0rd"):
    """注册（已存在则登录）并激活该账号令牌。返回 (成功?, 状态码, 响应体)。"""
    st, bd = call(base, "POST", "/api/v1/auth/register", {
        "username": user, "password": password,
        "email": f"{user}@example.com", "display_name": user,
    }, timeout=30)
    if st == 409:
        st, bd = call(base, "POST", "/api/v1/auth/login",
                      {"account": user, "password": password}, timeout=30)
    if not set_tokens(bd, user=user):
        return False, st, bd
    return True, st, bd


def rate_limit_probe(base, quota=60, user="soak_rl"):
    """端到端验一次限流能力：新账号连打 quota+1 次，第 quota+1 次必须是 429 且带 Retry-After。

    为什么敢用空 goal：`create` 的第 0 步就是取令牌（早于参数校验），
    空目标同样消耗配额、却不会真的建单 —— quota+1 次请求零副作用，
    既验到"配额确实是 quota 次"，也验到"超限时给出的等待秒数是可用的"。

    这条断言是把限流从"绊脚石"变成"被测对象"：修复前 429 只配当失败原因，
    修复后它是必须被证明的能力。
    """
    ok, st, bd = ensure_account(base, user)
    if not ok:
        return False, f"限流探针账号拿不到令牌（HTTP {st}）", []
    codes = []
    last_body = {}
    for _ in range(quota + 1):
        ensure_fresh(base)
        st, bd = call(base, "POST", "/api/v1/tasks", {"mode": "sync", "goal": ""},
                      token=_AUTH["access"], timeout=20)
        codes.append(st)
        last_body = bd or {}
    early = [i + 1 for i, c in enumerate(codes[:quota]) if c == 429]
    ra = str(last_body.get("_retry_after") or "")
    final_429 = codes[-1] == 429
    ra_ok = ra.isdigit() and int(ra) >= 1
    detail = (f"前 {quota} 次内提前出现 429 的位置 {early or '无'}；"
              f"第 {quota + 1} 次 HTTP={codes[-1]}；Retry-After={ra or '缺失'}")
    return (not early) and final_429 and ra_ok, detail, codes


# ---------------------------------------------------------------- 断言

def _model_used(row) -> bool:
    """这一轮的文档骨架到底是不是模型给的（而不是静默回退的规则骨架）。"""
    for s in row["steps"]:
        if s.get("agent") == "doc-agent":
            sa = s.get("sa") or {}
            meta = sa.get("model") or {}
            return sa.get("outline_source") == "model" and bool(meta.get("used"))
    return False


def judge_round(row, model, expect_agent=None):
    """单轮断言：返回失败原因列表（空 = 通过）。"""
    bad = []
    if row["http"] not in (200, 201, 202):
        return [f"HTTP {row['http']} {row['note']}"]
    if row["task_status"] != "succeeded":
        bad.append(f"终态={row['task_status']}（期望 succeeded）")
    steps = row["steps"]
    if len(steps) < 3:
        bad.append(f"步数={len(steps)}（期望 >=3）")
    agents = [s["agent"] for s in steps]
    if agents and agents[0] != "super-agent":
        bad.append(f"首步 agent={agents[0]}（期望 super-agent）")
    if "guard-agent" not in agents:
        bad.append("计划里缺 guard-agent")
    if agents and agents[-1] != "super-agent":
        bad.append(f"末步 agent={agents[-1]}（期望 super-agent/synthesis）")
    if row["tag"] == "model":
        hits = [s for s in steps if s["agent"] == "doc-agent"]
        if not hits:
            bad.append("没路由到 doc-agent")
        else:
            h = hits[0]
            if not h["success"]:
                bad.append(f"doc-agent 失败：{h['err']}")
            if h["tokens"] <= 0:
                bad.append("doc-agent tokens=0（模型没真跑）")
            # 判据升级：tokens>0 只证明"模型被调用过"，证明不了"产出被采用"。
            # 实测 42 轮里 12 轮真调了模型却全部静默回退规则骨架（model.used=false），
            # 旧判据全绿——这就是"接口 succeeded 但功能没生效"的典型假绿。
            payload = h.get("sa") or {}
            meta = payload.get("model") or {}
            source = payload.get("outline_source")
            if source != "model":
                bad.append(f"骨架来源={source}（期望 model）")
            elif not meta.get("used"):
                bad.append(f"模型产出未被采用：{meta.get('reason')}")
            if model not in h["out"]:
                bad.append(f"doc-agent 输出未标注模型名：{h['out'][:60]}")
    if expect_agent and expect_agent not in agents:
        bad.append(f"路由未命中 {expect_agent}（实际 {agents}）")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description="多轮压测：本地小模型真跑几十轮")
    ap.add_argument("--base", default="http://127.0.0.1:11434/v1")
    ap.add_argument("--model", default="minicpm5-2b:f16")
    ap.add_argument("--api-key", default="ollama")
    ap.add_argument("--port", type=int, default=8173)
    ap.add_argument("--data", default="target/soak")
    ap.add_argument("--model-rounds", type=int, default=12, help="走真模型的轮数")
    ap.add_argument("--fast-rounds", type=int, default=24, help="快轮数（规则 agent）")
    ap.add_argument("--guard-rounds", type=int, default=6, help="坏输入轮数")
    ap.add_argument("--llm-timeout", type=int, default=300)
    ap.add_argument("--step-timeout", type=int, default=300)
    ap.add_argument("--task-timeout", type=int, default=900)
    ap.add_argument("--exe", default=None)
    ap.add_argument("--python", default=None,
                    help="worker 用的解释器；缺省取 SA_PYTHON 或本脚本自身解释器。"
                         "不显式给会让后端用裸 python，PATH 里没有时脚本退出码 9009")
    ap.add_argument("--tag", default="soak", help="证据文件名前缀")
    ap.add_argument("--allow-skip", action="store_true",
                    help="模型不可达时以退出码 3 表示 SKIP（供 regress_all 区分"
                         "「环境不具备」与「功能退化」），不带此参数则照样按失败算")
    ap.add_argument("--workers-dir", default=None,
                    help="worker 源目录；只有负向演练才需要指向改坏副本，缺省用项目正本")
    ap.add_argument("--no-json-mode", action="store_true",
                    help="关掉 OpenAI 兼容层的 json_object 模式（归因实验用：区分"
                         "「JSON 模式」与「json_schema 强约束」各自贡献了多少）")
    ap.add_argument("--keep-data", action="store_true")
    ap.add_argument("--users", type=int, default=3,
                    help="压测账号数（默认 3）。POST /tasks 限流键是 task_submit:{user.id}、"
                         "每用户 60 次/小时，单账号连打 110 轮必然在第 83 轮撞满配额，"
                         "后半段全变成 429 —— 那不是功能失败，是探针自己撞限流。"
                         "按用户轮转后每账号约 37 次，同时顺带验证了限流按用户隔离")
    ap.add_argument("--rate-limit-quota", type=int, default=60,
                    help="G8 限流断言用的每账号配额（须与后端 task_submit_quota_per_hour 一致）")
    ap.add_argument("--no-rate-limit-probe", action="store_true",
                    help="跳过 G8 限流能力断言（只在明确想省时间时用；跳过会标 SKIP 而非 PASS）")
    args = ap.parse_args()

    # 先问模型在不在，再决定要不要起进程 —— 顺序反了会白等 30 秒超时
    ready, why = probe_model_ready(args.base, args.model)
    if not ready:
        if not args.allow_skip:
            print(f"[FAIL] 模型不可用：{why}")
            print("       本门禁必须真模型，缺模型不能算绿；如只是本机没起 Ollama，请加 --allow-skip")
            return 1
        print(f"[SKIP] 模型不可用：{why}")
        print("       本门禁需要本地真模型，本次记 SKIP（不是 PASS）。")
        print("       环境就绪后必须补跑：--tag soak-vN 或直接 regress_all.py")
        ev = ROOT / "docs" / "evidence"
        ev.mkdir(parents=True, exist_ok=True)
        (ev / f"{args.tag}-latest.json").write_text(json.dumps({
            "skipped": True, "reason": why, "model": args.model, "base": args.base,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        return 3

    data_dir = (ROOT / args.data).resolve()
    base = f"http://127.0.0.1:{args.port}"
    runner = prepare_data(data_dir, args.keep_data, args.workers_dir)

    env_extra = {
        "SA_LLM_BASE": args.base,
        "SA_LLM_MODEL": args.model,
        "SA_LLM_API_KEY": args.api_key,
        "SA_LLM_TIMEOUT": str(args.llm_timeout),
        "SA_LLM_MAX_TOKENS": "1600",
        # 实测（docs/85 §2）：不开 json 模式时 2B 模型会把 JSON 包在 `"""` 里再接一段废话，
        # 解析全失败、全域回退规则骨架。开了以后 finish=stop、输出干净。
        # 快多少只敢给方向性结论：模型轮平均 41.2s（关，3 轮）→ 31.9s（开，20 轮），约 -23%；
        # 样本不对等、模型是否常驻也会影响单轮耗时，故不承诺具体倍数。
        "SA_LLM_JSON_MODE": "0" if args.no_json_mode else "1",
        "SA_RUNNER_TIMEOUT_DOC_AGENT": str(args.step_timeout),
        "SA_RUNNER_TIMEOUT_PLANNER_AGENT": str(args.step_timeout),
    }
    python_exe = args.python or os.environ.get("SA_PYTHON") or sys.executable
    env_extra["SA_PYTHON"] = python_exe

    exe = resolve_exe(args.exe)
    print(f"== 多轮压测 ==  模型 {args.model}  二进制 {exe}")
    print(f"   worker 解释器 {python_exe}")
    print(f"   数据目录 {data_dir}")
    proc = launch(exe, args.port, data_dir, runner, env_extra)
    rows = []
    try:
        if not wait_ready(base):
            print("   /readyz 未就绪，看日志：", data_dir / "server.log")
            return 2
        print("   /readyz OK\n")

        accounts = [("soak_owner" if i == 0 else f"soak_u{i + 1}")
                    for i in range(max(1, args.users))]
        for u in accounts:
            ok, st, bd = ensure_account(base, u)
            if not ok:
                print(f"   账号 {u} 拿不到令牌：", st, bd)
                return 2
        set_active(accounts[0])
        print(f"   {len(accounts)} 个账号令牌就绪（按用户轮转，避让 60 次/小时的提交限流；"
              f"TTL 30 分钟，自动续期）")

        total_plan = args.model_rounds + args.fast_rounds + args.guard_rounds
        print(f"   计划 {total_plan} 轮：模型 {args.model_rounds} / 快 {args.fast_rounds} / 坏输入 {args.guard_rounds}\n")
        # 每组目标是轮转消费的：轮数少于用例数，排在后面的目标一次都跑不到。
        # 新加 agent 的用例通常排在末尾，最容易"加了但没实测"，所以显式提示。
        if args.fast_rounds < len(FAST_GOALS):
            print(f"   ⚠ fast 轮数 {args.fast_rounds} < 用例数 {len(FAST_GOALS)}：末尾目标不会被覆盖")
        if args.model_rounds < len(MODEL_GOALS):
            print(f"   ⚠ model 轮数 {args.model_rounds} < 用例数 {len(MODEL_GOALS)}：末尾目标不会被覆盖")
        if args.guard_rounds < len(GUARD_CASES):
            print(f"   ⚠ guard 轮数 {args.guard_rounds} < 用例数 {len(GUARD_CASES)}：末尾用例不会被覆盖")

        n = 0
        # ---- model 组 ----
        for i in range(args.model_rounds):
            goal, tt = MODEL_GOALS[i % len(MODEL_GOALS)]
            n += 1
            set_active(accounts[(n - 1) % len(accounts)])
            print(f"[{n}/{total_plan}] model 轮 {i+1}: {goal[:28]}…", end="", flush=True)
            row = run_task(base, goal, tt, "model", args.task_timeout)
            row["fails"] = judge_round(row, args.model)
            rows.append(row)
            print(f"  {row['task_status']} {row['elapsed']}s tok={row['tokens']} "
                  f"{'OK' if not row['fails'] else 'FAIL:' + ';'.join(row['fails'])}", flush=True)

        # ---- fast 组 ----
        for i in range(args.fast_rounds):
            goal, tt, expect = FAST_GOALS[i % len(FAST_GOALS)]
            n += 1
            set_active(accounts[(n - 1) % len(accounts)])
            row = run_task(base, goal, tt, "fast", args.task_timeout)
            row["fails"] = judge_round(row, args.model, expect)
            rows.append(row)
            print(f"[{n}/{total_plan}] fast  {tt:<13} {row['task_status']} {row['elapsed']:>5}s "
                  f"{'OK' if not row['fails'] else 'FAIL:' + ';'.join(row['fails'])}", flush=True)

        # ---- guard 组 ----
        for i in range(args.guard_rounds):
            goal, tt, expect = GUARD_CASES[i % len(GUARD_CASES)]
            n += 1
            set_active(accounts[(n - 1) % len(accounts)])
            row = run_task(base, goal, tt, "guard", args.task_timeout)
            ok = row["http"] in (400, 403, 422) and row["task_id"] is None
            if ok:
                row["fails"] = []
            elif row["http"] == 429:
                # 修复前这里只会写"未被拒（HTTP 429）"，读证据的人会去查 guard 闸门，
                # 而真相是"根本还没走到闸门就被限流挡在门口"。两种因必须分开写。
                row["fails"] = [f"被限流抢先（HTTP 429，退避重试 {row['throttled']} 次仍在限流），"
                                f"本轮未验到拒单原因，不算通过"]
            else:
                row["fails"] = [f"坏输入未被拒（HTTP {row['http']} task={row['task_id']}）"]
            rows.append(row)
            print(f"[{n}/{total_plan}] guard {expect:<20} HTTP {row['http']} "
                  f"{'OK' if ok else 'FAIL'}", flush=True)

        # ---- 全局断言 ----
        print("\n== 全局断言 ==")
        created = sum(1 for r in rows if r["task_id"])
        # 台账按账号分开数：台账和限流一样是按用户隔离的，多账号压测必须逐账号核对后求和。
        # limit 上限 100（window_checked 校验），超限会被 400 拒；台账数以 total 为准
        per_user = {}
        for u in accounts:
            set_active(u)
            _, listing = authed_call(base, "GET", "/api/v1/tasks?limit=100", timeout=30)
            per_user[u] = int((listing or {}).get("total") or len((listing or {}).get("items") or []))
        listed = sum(per_user.values())
        five_xx = [r for r in rows if r["http"] >= 500]
        model_rows_all = [r for r in rows if r["tag"] == "model"]
        adopted = [r for r in model_rows_all if _model_used(r)]
        # 只数 model/fast：guard 组的 422 是**预期**结果，把它算进"未正常返回"
        # 会让 G7 详情凭空多出一句误导性备注（烟雾跑第一次就踩到了）。
        abnormal = [r for r in rows
                    if r["tag"] in ("model", "fast") and r.get("http") not in (200, 201, 202)]

        # ---- 限流能力探针（放在最后：会切换 active 账号，也不产生任务）----
        if args.no_rate_limit_probe:
            rl_ok, rl_detail = True, "已按 --no-rate-limit-probe 跳过（不计入通过，仅跳过）"
        else:
            print(f"\n== 限流能力探针（{args.rate_limit_quota} 次/账号）==")
            rl_ok, rl_detail, _codes = rate_limit_probe(base, args.rate_limit_quota)
            print(f"   {'PASS' if rl_ok else 'FAIL'}  {rl_detail}")
            set_active(accounts[0])   # 探针把 active 切成探针账号，必须切回来，否则后续取证错账号

        checks = [
            ("G1", all(r["task_status"] == "succeeded" for r in rows if r["tag"] in ("model", "fast")),
             f"正常轮次全部 succeeded：{sum(1 for r in rows if r['tag'] in ('model','fast') and r['task_status']=='succeeded')}"
             f"/{sum(1 for r in rows if r['tag'] in ('model','fast'))}"),
            ("G2", all(r["tokens"] > 0 for r in model_rows_all),
             f"模型轮次 tokens>0：{sum(1 for r in model_rows_all if r['tokens']>0)}"
             f"/{len(model_rows_all)}"),
            # 这条是本次加固的核心：从"模型被调用"升级到"模型产出真被采用"。
            # 前半句 len(model_rows_all) > 0 是防假绿的第二道锁：`all([])` 恒为 True，
            # 万一将来有人把 --model-rounds 传成 0，这条会静悄悄变绿而模型一次都没跑。
            ("G3", len(model_rows_all) > 0 and len(adopted) == len(model_rows_all),
             f"模型产出被采用（outline_source=model 且 model.used=true）："
             f"{len(adopted)}/{len(model_rows_all)}"),
            ("G4", all(not r["fails"] for r in rows if r["tag"] == "fast"),
             f"快轮次路由与形状全对：{sum(1 for r in rows if r['tag']=='fast' and not r['fails'])}"
             f"/{sum(1 for r in rows if r['tag']=='fast')}"),
            ("G5", all(not r["fails"] for r in rows if r["tag"] == "guard"),
             f"坏输入全部被拒：{sum(1 for r in rows if r['tag']=='guard' and not r['fails'])}"
             f"/{sum(1 for r in rows if r['tag']=='guard')}"),
            ("G6", not five_xx, f"5xx 计数 = {len(five_xx)}"),
            # 台账这条的语义是"建一单就能查回来一单"。超时轮次（http=0）不计入 created，
            # 但会占满服务端，连 `/tasks` 列表请求本身也会超时 —— 那种情况下 G7 变红不代表
            # 台账丢单。所以把异常轮次数打进详情，让读证据的人能分辨，别把探针误报当缺陷。
            ("G7", listed == created, f"任务台账 {listed} 条 vs 建单 {created} 条"
             + (f"（分账号 {per_user}）" if len(per_user) > 1 else "")
             + (f"（另有 {len(abnormal)} 轮未正常返回，列表请求可能被长任务占满）"
                if abnormal else "")),
            # G8 是这轮加固新增的一条：修复前 429 只被当成"功能失败原因"，
            # 从没被当成"必须被证明的能力"。这条要求限流既真的生效、又给出可用等待秒数。
            # 第四位 skipped 是为了守住 docs/06 的约定：没跑过的检查不能算 PASS。
            ("G8", rl_ok, f"限流到位（{args.rate_limit_quota} 次后必 429 且带 Retry-After）：{rl_detail}"),
        ]
        # 跳过 G8 时显示 SKIP 而不是 PASS：跑都没跑过的东西不能算绿（docs/06 的约定）。
        for cid, ok, det in checks:
            mark = "SKIP" if (cid == "G8" and args.no_rate_limit_probe) else ("PASS" if ok else "FAIL")
            print(f"   [{cid}] {mark}  {det}")

        # ---- 统计 ----
        model_rows = [r for r in rows if r["tag"] == "model"]
        fast_rows = [r for r in rows if r["tag"] == "fast"]
        # 超时轮的 elapsed 是 None（请求都没回来）。统计里必须先把 None 滤掉，
        # 否则 mean/min 直接 TypeError —— 也就是说，**最该留证的那种失败场景，
        # 脚本会先崩掉**。这条是用无约束配置复现"修前退化"时踩到的，如实记在 docs/85。
        el = [r["elapsed"] for r in model_rows if isinstance(r.get("elapsed"), (int, float))]
        tk = [r["tokens"] for r in model_rows if isinstance(r.get("tokens"), int)]
        stats = {
            "rounds_total": len(rows),
            "model_rounds": len(model_rows),
            "fast_rounds": len(fast_rows),
            "model_elapsed_avg_s": round(statistics.mean(el), 1) if el else 0,
            "model_elapsed_min_s": round(min(el), 1) if el else 0,
            "model_elapsed_max_s": round(max(el), 1) if el else 0,
            "model_tokens_avg": round(statistics.mean(tk), 1) if tk else 0,
            "model_tokens_min": min(tk) if tk else 0,
            "model_tokens_max": max(tk) if tk else 0,
            "fast_elapsed_avg_s": round(statistics.mean([r["elapsed"] for r in fast_rows]), 2) if fast_rows else 0,
            "model_adopted_rounds": len(adopted),
            "auth_refreshes": total_refreshes(),
            "accounts": list(accounts),
            "throttled_rounds": sum(1 for r in rows if r.get("throttled")),
            "throttle_wait_s": round(sum(r.get("waited_s") or 0 for r in rows), 1),
            "rate_limit_probe": {"ok": rl_ok, "detail": rl_detail},
            "route_hits": {},
        }
        for r in rows:
            for s in r["steps"]:
                stats["route_hits"][s["agent"]] = stats["route_hits"].get(s["agent"], 0) + 1
        print("\n== 统计 ==")
        for k, v in stats.items():
            if k != "route_hits":
                print(f"   {k:24s} {v}")
        print(f"   route_hits               {stats['route_hits']}")

        if model_rows:
            print("\n== 模型轮次逐轮 ==")
            for i, r in enumerate(model_rows, 1):
                doc = next((s for s in r["steps"] if s["agent"] == "doc-agent"), {})
                sa = doc.get("sa") or {}
                meta = sa.get("model") or {}
                mark = "采用" if _model_used(r) else "回退"
                why = "" if _model_used(r) else f" | {str(meta.get('reason'))[:52]}"
                el_s = f"{r['elapsed']:>6}" if isinstance(r.get("elapsed"), (int, float)) else "  ----"
                print(f"   #{i:<2} {el_s}s tok={r['tokens']:>4} "
                      f"doc_tok={doc.get('tokens', 0):>4} attempts={doc.get('attempts')} "
                      f"{r['task_status']:<10} {mark}{why}")

        all_ok = all(ok for cid, ok, _ in checks
                     if not (cid == "G8" and args.no_rate_limit_probe))
        print("\n结论：", "全绿" if all_ok else "有 FAIL，按上面逐条排查")
        stats["run_wall_s"] = round(time.time() - rows[0].get("_t0", time.time()), 0) if rows else 0

        # ---- 取证 ----
        ev_dir = ROOT / "docs" / "evidence"
        ev_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "model": args.model, "base": args.base, "exe": str(exe),
            "exe_mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exe.stat().st_mtime)),
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "checks": [{"id": c, "ok": o, "detail": d} for c, o, d in checks],
            "stats": stats, "rounds": rows,
        }
        out_json = ev_dir / f"{args.tag}-latest.json"
        out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print("   证据：", out_json)
        return 0 if all_ok else 1
    finally:
        stop(proc)


if __name__ == "__main__":
    sys.exit(main())
