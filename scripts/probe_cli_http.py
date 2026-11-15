#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_cli_http.py —— CLI 客户端子命令（login/logout/submit/ls/show）行为探针（门禁 23）。

为什么需要这一道，而门禁 22 不够：
  门禁 22 守的是"外壳"——子命令在不在、无参是否仍是 serve、doctor 的退出码。
  它全程不登录、不提交任务，所以对"客户端"这一半完全无感。而客户端这一半的
  失效方式特别阴：命令照样 rc=0、照样打印中文，只是**内容不对**（id 被截断没法
  复制去 show、时间不标时区、刷新后把已作废的令牌写回文件、多终端并发时莫名
  要求重新登录）。这些没有任何一道门禁会红，只会在深夜排障时以"CLI 老是让我
  重新登录"的形式出现。

  这一道的价值：把"人能看懂的输出"和"脚本能解析的输出"都钉成断言，
  并专门盯住 401 → 刷新 → 重试这条最容易悄悄坏掉的链路。

五组断言（失效方式不同，所以分组）：
  A 参数面    五个客户端子命令的参数是否真存在。快、不起进程。
  B 认证周期  login 落盘 / logout 删档 / 无令牌 rc=3 / 无账号不挂死。
  C 401 自愈  access 坏→自动刷新→回写（access 与 refresh 都要换）；
              双坏→rc=3；--token 与 SA_TOKEN 能绕过坏文件；多进程并发刷新不互踩。
  D 输出契约  表头与数据行按**显示宽度**逐列对齐（中文算 2 列，这是最容易错的地方）、
              完整 UUID、UTC 口径写在标签上、--json 纯净可解析、show 详情与 404。
  E 判据有效  连不上服务必须是 rc=1 而不是 0/2；doctor 不许顺手动令牌文件；
              CLI 读操作不许改变服务端数据。

退出码：0 = 全部通过；1 = 有 failed；2 = 脚本自身配置错（--exe 不存在等）。

用法：
  python scripts/probe_cli_http.py
  python scripts/probe_cli_http.py --fast     # 跳过并发项（省约 10s）
  python scripts/probe_cli_http.py --drill    # 负向演练：换个不是本服务的程序，确认本门禁会成片变红
  python scripts/probe_cli_http.py --json out.json

与 probe_cli.py 的公共部分（run_cli / start_service / Report …）故意不抽公共库：
门禁脚本要能单个文件拷走、单跑、单改，抽了公共库就变成"改一处炸十道"。
"""

import argparse
import concurrent.futures as cf
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]

try:
    from sync_build import resolve_build

    DEFAULT_EXE = resolve_build() / "target-gnu" / "debug" / "super-agents.exe"
except Exception:  # noqa: BLE001
    DEFAULT_EXE = (pathlib.Path(os.environ.get("LOCALAPPDATA", ".")) / "sa-build"
                   / "target-gnu" / "debug" / "super-agents.exe")

# src/cli/task.rs 的退出码口径。这里复写成常量不是为了"同步"，而是为了让断言
# 能写出 `rc == RC_AUTH`——数字 3 单独出现时，人读代码看不出它意味着"该重新登录"。
RC_OK = 0
RC_USAGE = 1     # 用法错 / 连不上 / 参数不合法
RC_SERVER = 2    # 服务端返回了但不是我想要的结果（404、5xx）
RC_AUTH = 3      # 令牌失效且刷新失败，需要重新登录

# 表格列宽，必须与 src/cli/task.rs 的 W_* 常量逐一对应。
# 一旦那边改了宽、这边没跟上，D 组会红——这正是它存在的意义。
COL_WIDTHS = [36, 19, 13, 13, 19]
HEADER_CELLS = ["id", "状态", "类型", "tokens", "创建时间(UTC)"]

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TOKENS_RE = re.compile(r"^\d+/\d+$")
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

DEFAULT_ACCOUNT = "cli_http_probe"
DEFAULT_PASSWORD = "Cli!Http2026"


# ---------------------------------------------------------------- 基础工具
def pick_workdir():
    """在 target/cli-http-probe 下递增新建 run-N。

    只新建、不删除：本机对脚本内的递归删除有硬拦截（DEL-101），
    写成"每次清空目录"会让整道门禁起不来；递增目录还能留下每轮的现场供事后翻。
    """
    base = ROOT / "target" / "cli-http-probe"
    base.mkdir(parents=True, exist_ok=True)
    for i in range(1, 2000):
        d = base / ("run-%d" % i)
        if not d.exists():
            d.mkdir(parents=True)
            return d
    raise RuntimeError("cli-http-probe 下已积累过多运行目录，请人工归档")


def find_free_port(start=18420, tries=80):
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
            except OSError:
                continue
            return p
    raise RuntimeError("在 %d~%d 之间找不到空闲端口" % (start, start + tries))


def clean_env(extra=None):
    """剔除全部 SA_* 再叠加本用例要的。

    不剔除的话，本机 shell 里若残留 SA_TOKEN / SA_DATA_DIR，断言会在
    "我造的场景"和"上一个人留下的场景"之间反复横跳，红得莫名其妙。
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("SA_")}
    env.setdefault("SA_JWT_SECRET", "x" * 40)
    if extra:
        env.update(extra)
    return env


def run_cli(exe, args, env_extra=None, timeout=90, stdin_closed=False):
    """跑一次 CLI。返回 (rc, stdout+stderr 文本)。

    用 bytes 读再自己解码：Windows 控制台默认 GBK，subprocess 的 text 模式会
    按 locale 解 UTF-8 输出，中文立刻变乱码，然后"中文没乱码"这条断言反而永远假红。
    """
    kw = {}
    if stdin_closed:
        kw["stdin"] = subprocess.DEVNULL
    try:
        r = subprocess.run([str(exe)] + list(args), capture_output=True,
                           env=clean_env(env_extra), timeout=timeout, **kw)
        return r.returncode, (r.stdout + r.stderr).decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return None, "超时（%ss）" % timeout


def http_get(base, path, timeout=3.0, token=None):
    # base 为 None 意味着服务没起来。返回"查不到"而不是抛异常：
    # 门禁脚本自己崩掉时不会打印汇总行，看起来像工具坏了，而不是"这道门禁判红"。
    if not base:
        return None, ""
    req = urllib.request.Request(base + path)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None, ""


def http_json(base, method, path, body=None, token=None, timeout=30):
    if not base:  # 同上：没服务时给空结果，让断言自己变红
        return None, {}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", errors="replace") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", errors="replace") or "{}")
        except Exception:  # noqa: BLE001
            return e.code, {}
    except Exception:  # noqa: BLE001
        return None, {}


def start_service(exe, port, data_dir, log_path, wait=30.0):
    log = open(log_path, "wb")
    env = clean_env({"SA_BIND": "127.0.0.1:%d" % port, "SA_DATA_DIR": str(data_dir)})
    proc = subprocess.Popen([str(exe)], stdout=log, stderr=subprocess.STDOUT, env=env, cwd=str(ROOT))
    base = "http://127.0.0.1:%d" % port
    deadline = time.time() + wait
    while time.time() < deadline:
        if proc.poll() is not None:
            log.close()
            return None, None, "进程提前退出 rc=%s" % proc.returncode
        code, _ = http_get(base, "/healthz", timeout=1.5)
        if code == 200:
            return proc, log, "就绪"
        time.sleep(0.25)
    return proc, log, "%ss 内 /healthz 无响应" % int(wait)


def stop(proc, log):
    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
    if log is not None:
        try:
            log.close()
        except Exception:  # noqa: BLE001
            pass


def dwidth(s):
    """显示宽度：全角/宽字符算 2 列。与 src/cli/task.rs 的 dwidth 同口径。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def slice_disp(s, start, end):
    """按显示宽度切 [start, end) 区间。用于把表格行切成"列窗口"。"""
    out, col = [], 0
    for ch in s:
        if col >= end:
            break
        if col >= start:
            out.append(ch)
        col += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return "".join(out)


def cell(line, idx):
    """取第 idx 列的文本。

    表格格式串是 `"{} {} {} {} {} {}"`——每列之间还有**一个分隔空格**。
    忘了这个 1 列偏移，取到的就会是"上一列尾巴 + 下一列开头"，
    看起来只是差几个字符，实际会让所有列断言一起错位（时间列会变成 `07:3`）。
    """
    lo = sum(COL_WIDTHS[:idx]) + idx
    return slice_disp(line, lo, lo + COL_WIDTHS[idx]).strip()


class Report:
    def __init__(self):
        self.rows = []

    def add(self, group, name, ok, detail=""):
        ok = bool(ok)
        self.rows.append({"group": group, "name": name, "ok": ok, "detail": detail})
        print("%s %-2s %-38s %s" % ("[PASS]" if ok else "[FAIL]", group, name, detail))
        return ok

    @property
    def failed(self):
        return [r for r in self.rows if not r["ok"]]

    def summary(self):
        return "合计 %d 项 | 通过 %d | 失败 %d" % (
            len(self.rows), len(self.rows) - len(self.failed), len(self.failed))


class Ctx:
    def __init__(self, exe, workdir):
        self.exe = exe
        self.workdir = workdir
        self.port = None
        self.hostport = None
        self.base = None
        self.token_file = None
        self.alive = False
        self.proc = None
        self.log = None
        self.reg_token = ""

    def env(self, extra=None):
        # CLI 取服务地址读的是 SA_BIND（与 Config 同源），形如 127.0.0.1:8088——
        # 不带 http:// 前缀。写成 SA_ADDR 或带 scheme 都会被静默忽略，
        # 然后报"连不上 127.0.0.1:8088"，让人以为服务没起。
        # 服务没起来（演练模式）时给一个必然拒绝的地址：subprocess 的环境变量
        # 只能是字符串，塞 None 会直接抛 TypeError，把"演练"变成"脚本崩了"。
        hp = self.hostport or "127.0.0.1:9"
        e = {"SA_BIND": hp, "SA_TOKEN_FILE": str(self.token_file),
             "SA_ACCOUNT": DEFAULT_ACCOUNT, "SA_PASSWORD": DEFAULT_PASSWORD}
        if extra:
            e.update(extra)
        return e


def g(rep, ctx, group, name, ok, detail=""):
    """服务没就绪时，把该组断言统一记红。

    不能"服务没起来就跳过"——跳过等于空跑，门禁变绿但什么也没验。
    """
    if not ctx.alive:
        return rep.add(group, name, False, "服务未就绪（%s）" % (detail or "起不来"))
    return rep.add(group, name, ok, detail)


# ---------------------------------------------------------------- A 组：参数面
def group_a(rep, ctx):
    exe = ctx.exe
    rc, out = run_cli(exe, ["--help"], timeout=30)
    for cmd in ("serve", "doctor", "login", "logout", "submit", "ls", "show"):
        rep.add("A", "A1 --help 列出 %s" % cmd, rc == 0 and cmd in out, "rc=%s 命中=%s" % (rc, cmd in out))

    # 这句子是运维唯一能看到"哪些子命令需要服务在跑"的地方。少了它，
    # 服务没起时用户会在 login/ls 之间反复试，而不知道 doctor 本来就能跑。
    rep.add("A", "A2 help 说明哪些需要服务在跑",
            "除 `doctor` 与 `logout` 外" in out, "命中=%s" % ("除 `doctor` 与 `logout` 外" in out))

    for cmd, opts in (
        ("login", ["--account", "--password", "--show-token"]),
        ("submit", ["--task-type", "--mode", "--model-profile", "--budget-tokens"]),
        ("ls", ["--limit", "--offset", "--status", "--task-type", "--q", "--json"]),
        ("show", ["--addr", "--token"]),
    ):
        rc, out = run_cli(exe, [cmd, "--help"], timeout=30)
        miss = [o for o in opts if o not in out]
        rep.add("A", "A3 %s 参数齐（缺 %s）" % (cmd, miss or "无"),
                rc == 0 and not miss, "rc=%s 缺=%s" % (rc, miss))


# ---------------------------------------------------------------- B 组：认证周期
def group_b(rep, ctx):
    tok = ctx.token_file

    rc, out = run_cli(ctx.exe, ["login"], ctx.env())
    g(rep, ctx, "B", "B1 login rc=0", rc == RC_OK, "rc=%s" % rc)
    # 中文不乱码是"输出能给人看"的底线：Windows 上 GBK/UTF-8 一错，整屏变问号。
    # 断的是三处中文标签而非整句，措辞微调不会误红，但编码一错必红。
    g(rep, ctx, "B", "B2 login 中文未乱码",
      "令牌存" in out and "有效期" in out and "\ufffd" not in out,
      (out.strip().splitlines() or [""])[0][:40])

    text = tok.read_text(encoding="utf-8") if tok.exists() else ""
    lines = [l for l in text.splitlines() if l.strip()]
    acc = next((l[len("access="):] for l in lines if l.startswith("access=")), "")
    ref = next((l[len("refresh="):] for l in lines if l.startswith("refresh=")), "")
    g(rep, ctx, "B", "B3 令牌文件恰两行 access/refresh",
      len(lines) == 2 and bool(acc) and bool(ref), "行数=%s acc=%s ref=%s" % (len(lines), len(acc), len(ref)))
    # 曾经出现过的病：只写了一半（前缀）也 rc=0。长度是廉价但有效的护栏。
    g(rep, ctx, "B", "B4 access 长度像真 JWT", len(acc) > 200, "len=%s" % len(acc))

    # 重复登录必须覆盖而不是追加，否则文件越滚越长，下次读出哪一行全看运气。
    run_cli(ctx.exe, ["login"], ctx.env())
    text2 = tok.read_text(encoding="utf-8") if tok.exists() else ""
    lines2 = [l for l in text2.splitlines() if l.strip()]
    g(rep, ctx, "B", "B5 重复 login 覆盖不追加", len(lines2) == 2, "行数=%s" % len(lines2))

    # 无账号且 stdin 关闭：必须给一句人话退出，不能卡在 read_line 上等一个不会来的输入。
    rc, out = run_cli(ctx.exe, ["login"], clean_env({"SA_TOKEN_FILE": str(tok)}),
                      timeout=20, stdin_closed=True)
    g(rep, ctx, "B", "B6 login 无账号不挂死",
      rc in (RC_USAGE, RC_SERVER) and ("--account" in out or "账号" in out),
      "rc=%s 提示=%s" % (rc, out.strip().splitlines()[0][:50] if out.strip() else ""))


# ---------------------------------------------------------------- C 组：401 自愈
def read_tokens(tok):
    if not tok.exists():
        return "", ""
    acc = ref = ""
    for ln in tok.read_text(encoding="utf-8").splitlines():
        if ln.startswith("access="):
            acc = ln[7:].strip()
        elif ln.startswith("refresh="):
            ref = ln[8:].strip()
    return acc, ref


def group_c(rep, ctx):
    tok = ctx.token_file
    env = ctx.env()

    acc0, ref0 = read_tokens(tok)
    g(rep, ctx, "C", "C0 起始令牌齐", bool(acc0) and bool(ref0), "acc=%s ref=%s" % (len(acc0), len(ref0)))

    # access 打成垃圾、refresh 留着：正常 CLI 必须自己刷一次并成功。
    tok.write_text("access=broken-by-probe\nrefresh=%s\n" % ref0, encoding="utf-8")
    rc, out = run_cli(ctx.exe, ["ls", "--limit", "1"], env)
    g(rep, ctx, "C", "C1 access 坏 → 自动刷新后成功", rc == RC_OK, "rc=%s" % rc)

    acc1, ref1 = read_tokens(tok)
    g(rep, ctx, "C", "C2 刷新后 access 已更换", acc1 != acc0 and acc1 != "broken-by-probe",
      "len %s→%s" % (len(acc0), len(acc1)))
    # 这条是本门禁的核心：服务端 refresh 是轮换的，新 refresh 必须落盘；
    # 曾经因为少查一层 `tokens` 嵌套，把已作废的旧 refresh 写回文件，
    # 结果"刷新成功"之后下一条命令必然 401，用户以为令牌坏了。
    g(rep, ctx, "C", "C3 刷新后 refresh 也更换并落盘",
      bool(ref1) and ref1 != ref0, "changed=%s" % (ref1 != ref0))
    g(rep, ctx, "C", "C4 文件里不再留旧 refresh", ref1 != ref0 and "broken" not in acc1, "ok=%s" % (ref1 != ref0))

    # 双坏：必须 rc=3 并明确要求重新登录，而不是 rc=2（"服务端有问题"）让人去查服务。
    tok.write_text("access=bad-acc\nrefresh=bad-ref\n", encoding="utf-8")
    rc, out = run_cli(ctx.exe, ["ls"], env)
    g(rep, ctx, "C", "C5 access+refresh 双坏 → rc=3", rc == RC_AUTH, "rc=%s" % rc)
    g(rep, ctx, "C", "C6 双坏提示去登录", "login" in out.lower(), "命中=%s" % ("login" in out.lower()))

    # --token / SA_TOKEN 是"令牌文件坏了也要能救急"的通道，绕不过去就等于没有。
    # 注意仍要用 ctx.env()：干净环境会把 SA_BIND 一起剔掉，于是命令连默认的
    # 127.0.0.1:8088，rc=1 报到"连不上"——看起来像 --token 失效，其实是没告诉它去哪。
    good = acc1
    rc, out = run_cli(ctx.exe, ["ls", "--token", good], ctx.env())
    g(rep, ctx, "C", "C7 --token 绕过坏文件", rc == RC_OK, "rc=%s" % rc)
    rc, out = run_cli(ctx.exe, ["ls"], ctx.env({"SA_TOKEN": good}))
    g(rep, ctx, "C", "C8 SA_TOKEN 环境变量直传", rc == RC_OK, "rc=%s" % rc)

    # 恢复成可用状态，供后续组和并发项使用
    rc, _ = run_cli(ctx.exe, ["login"], env)
    g(rep, ctx, "C", "C9 重新登录恢复", rc == RC_OK, "rc=%s" % rc)


def group_c_race(rep, ctx, n=8, rounds=3):
    """并发刷新：服务端 refresh 只认第一次，N 个进程同时刷新时只有一个能成。

    其余必须靠"重读令牌文件、用对端换出来的新令牌重试"自愈。没有这一条时，
    两个窗口同时跑 `ls` 就会有一个被要求重新登录——而这在多标签运维里是常态。
    """
    tok = ctx.token_file
    env = ctx.env()

    def one():
        return run_cli(ctx.exe, ["ls", "--limit", "1"], env, timeout=120)[0]

    all_ok = True
    detail = []
    for r in range(1, rounds + 1):
        _, ref = read_tokens(tok)
        tok.write_text("access=race-%d\nrefresh=%s\n" % (r, ref), encoding="utf-8")
        with cf.ThreadPoolExecutor(max_workers=n) as pool:
            codes = list(pool.map(lambda _: one(), range(n)))
        ok = all(c == RC_OK for c in codes)
        all_ok = all_ok and ok
        detail.append("第%d轮%s" % (r, codes))
        if not ok:
            break
    g(rep, ctx, "C", "C10 %d 进程并发刷新全成功" % n, all_ok, " | ".join(detail))

    # 并发之后再单跑一次：若并发把令牌文件写坏（半行/交替写），这里会立刻暴露。
    rc, _ = run_cli(ctx.exe, ["ls", "--limit", "1"], env)
    g(rep, ctx, "C", "C11 并发后单跑仍正常", rc == RC_OK, "rc=%s" % rc)


# ---------------------------------------------------------------- D 组：输出契约
def group_d(rep, ctx):
    env = ctx.env()
    marker = "探针任务-列出五种水果"

    rc, out = run_cli(ctx.exe, ["submit", marker, "--task-type", "writing", "--mode", "sync"], env, timeout=180)
    g(rep, ctx, "D", "D1 submit(sync) rc=0", rc == RC_OK, "rc=%s" % rc)
    g(rep, ctx, "D", "D2 submit 回显含 HTTP 状态", "已提交" in out and "HTTP" in out,
      (out.strip().splitlines() or [""])[0][:50])
    g(rep, ctx, "D", "D3 submit 中文 goal 不改字", marker in out, "命中=%s" % (marker in out))

    rc, out = run_cli(ctx.exe, ["submit", "--json", "--task-type", "writing", "--mode", "sync",
                                "探针任务-json 提交"], env, timeout=180)
    try:
        doc = json.loads(out.strip())
        task_id = doc.get("id", "")
    except Exception:  # noqa: BLE001
        task_id = ""
    g(rep, ctx, "D", "D4 submit --json 纯净可解析", rc == RC_OK and bool(task_id),
      "rc=%s id=%s" % (rc, task_id[:8] if task_id else "None"))

    rc, out = run_cli(ctx.exe, ["ls", "--limit", "5"], env)
    lines = [l for l in out.splitlines() if l.strip()]
    g(rep, ctx, "D", "D5 ls rc=0", rc == RC_OK, "rc=%s" % rc)
    g(rep, ctx, "D", "D6 ls 表头存在", bool(lines) and all(c in lines[0] for c in ("id", "状态", "类型")),
      lines[0][:40] if lines else "无输出")

    # 逐列按**显示宽度**切窗口比对。这是本门禁最硬的一条：
    # 中文在终端占两列，若 pad 按字符数补空格，表头就会整体右移、和数据行错位，
    # 而这种错位不影响任何 rc，人的眼睛却天天在看。
    if len(lines) >= 2:
        bad = []
        for i, want in enumerate(HEADER_CELLS):
            got = cell(lines[0], i)
            if got != want:
                bad.append("列%d应为%r实为%r" % (i, want, got))
        data = lines[1]
        if not UUID_RE.match(cell(data, 0)):
            bad.append("id 列不是完整 UUID：%r" % cell(data, 0))
        if not cell(data, 1):
            bad.append("状态列为空")
        if not TOKENS_RE.match(cell(data, 3)):
            bad.append("tokens 列格式不对：%r" % cell(data, 3))
        if not TS_RE.match(cell(data, 4)):
            bad.append("时间列格式不对：%r" % cell(data, 4))
    else:
        bad = ["行数不足，无法比对列"]
    g(rep, ctx, "D", "D7 表头与数据行按显示宽度逐列对齐", not bad, "；".join(bad[:4]))

    # 完整 UUID 才能直接复制到 show。截断成 `204750aa-…` 时，运维只能回去用 --json。
    full_ids = [cell(l, 0) for l in lines[1:] if UUID_RE.match(cell(l, 0))]
    g(rep, ctx, "D", "D8 全行 id 都是完整 UUID", len(full_ids) >= 2, "完整数=%s" % len(full_ids))
    g(rep, ctx, "D", "D9 输出无替换符与问号", "?" not in out and "\ufffd" not in out,
      "问号=%s 替换符=%s" % ("?" in out, "\ufffd" in out))

    foot = lines[-1] if lines else ""
    g(rep, ctx, "D", "D10 页脚标了 UTC 口径", "共" in foot and "时间为 UTC" in foot, foot[:70])

    rc, out = run_cli(ctx.exe, ["ls", "--json", "--limit", "5"], env)
    ok = False
    try:
        j = json.loads(out.strip())
        ok = isinstance(j, dict) and isinstance(j.get("items"), list) and "total" in j and bool(j["items"])
        if ok:
            first = j["items"][0]
            ok = all(k in first for k in ("id", "status", "goal", "created_at"))
    except Exception:  # noqa: BLE001
        ok = False
    g(rep, ctx, "D", "D11 ls --json 可解析且字段齐", ok, "rc=%s 解析=%s" % (rc, ok))

    rc, out = run_cli(ctx.exe, ["ls", "--json", "--status", "succeeded"], env)
    statuses = []
    try:
        statuses = [it.get("status") for it in json.loads(out.strip()).get("items", [])]
    except Exception:  # noqa: BLE001
        pass
    g(rep, ctx, "D", "D12 --status 过滤生效", rc == RC_OK and statuses and set(statuses) == {"succeeded"},
      "rc=%s 状态集=%s" % (rc, sorted(set(statuses))))

    rc, out = run_cli(ctx.exe, ["ls", "--q", "水果"], env)
    g(rep, ctx, "D", "D13 --q 中文关键词能过滤", rc == RC_OK and "水果" in out and "hello" not in out,
      "rc=%s 命中水果=%s 命中hello=%s" % (rc, "水果" in out, "hello" in out))

    # 演练模式下 ls 可能一行数据都没有，这里不能直接下标——脚本崩掉时，
    # 汇总行和 [DRILL-OK] 都不会打印，门禁看起来像"挂"而不是"变红"。
    some_id = full_ids[0] if full_ids else "00000000-0000-4000-8000-000000000001"
    rc, out = run_cli(ctx.exe, ["show", some_id], env)
    g(rep, ctx, "D", "D14 show rc=0", rc == RC_OK and some_id in out, "rc=%s" % rc)
    # 详情里的时间同样要标 UTC：正文里一个裸时间比表格里更容易被误读成本地时间。
    g(rep, ctx, "D", "D15 show 时间标 (UTC)", "(UTC)" in out, "命中=%s" % ("(UTC)" in out))

    rc, out = run_cli(ctx.exe, ["show", "00000000-0000-4000-8000-000000000000"], env)
    g(rep, ctx, "D", "D16 show 不存在 rc=2", rc == RC_SERVER, "rc=%s" % rc)
    g(rep, ctx, "D", "D17 show 不存在说人话", "没有这个任务" in out,
      (out.strip().splitlines() or [""])[0][:50])


# ---------------------------------------------------------------- E 组：判据有效
def group_e(rep, ctx):
    # 连不上服务：必须是 rc=1（用法/环境问题），不能是 0（假装成功）、
    # 也不能是 3（会把人引去重新登录，而真正的问题是服务没起）。
    # 令牌文件保持有效、只把地址指到丢包口：这样测的才是"连接失败"，
    # 而不是"没登录"——否则 401 那条路先命中，rc=3 混进来。
    dead = "127.0.0.1:%d" % find_free_port(19100, 40)
    dead_env = ctx.env({"SA_BIND": dead})
    for cmd in (["ls"], ["show", "x"], ["submit", "连不上测试"]):
        rc, out = run_cli(ctx.exe, cmd, dead_env, timeout=60)
        label = "E1 连不上服务 %s rc=1" % cmd[0]
        g(rep, ctx, "E", label, rc == RC_USAGE, "rc=%s" % rc)

    rc, out = run_cli(ctx.exe, ["ls"], dead_env, timeout=60)
    readable = ("连不上" in out) or ("拒绝" in out) or ("失败" in out)
    g(rep, ctx, "E", "E2 连不上给了可读提示", readable, (out.strip().splitlines() or [""])[0][:60])

    # doctor 是"服务起不来时唯一的入口"，它必须只读。若它顺手刷新/覆盖令牌文件，
    # 一个只想体检的人会莫名其妙被登出。
    before = ctx.token_file.read_text(encoding="utf-8") if ctx.token_file.exists() else ""
    run_cli(ctx.exe, ["doctor", "--json"], clean_env({"SA_DATA_DIR": str(ctx.workdir / "doc")}))
    (ctx.workdir / "doc").mkdir(parents=True, exist_ok=True)
    after = ctx.token_file.read_text(encoding="utf-8") if ctx.token_file.exists() else ""
    g(rep, ctx, "E", "E3 doctor 不碰令牌文件", before == after, "变=%s" % (before != after))

    # 只读命令不许改服务端数据：连续 3 次 ls，total 必须纹丝不动。
    t0 = None
    st0, body0 = http_json(ctx.base, "GET", "/api/v1/tasks?limit=50", token=ctx.reg_token)
    if st0 == 200:
        t0 = body0.get("total")
    for _ in range(3):
        run_cli(ctx.exe, ["ls", "--limit", "5"], ctx.env())
    st1, body1 = http_json(ctx.base, "GET", "/api/v1/tasks?limit=50", token=ctx.reg_token)
    t1 = body1.get("total") if st1 == 200 else None
    g(rep, ctx, "E", "E4 ls 不改服务端数据", t0 is not None and t0 == t1, "total %s→%s" % (t0, t1))


# ---------------------------------------------------------------- F 组：跟随事件流
# `submit --follow` 最容易做成"假跟随"：提交后 sleep 一下、再 GET 一次任务，
# 打印出来的东西长得跟真的事件流一模一样，区别只在中间态全被跳过了。
# 所以这里不看"像不像"，只看三件能证伪的事：
#   1) 有没有真的收到多帧（seq 是否从 0 起、是否严格递增）；
#   2) 退出码是不是从终态翻译出来的（失败任务必须非 0）；
#   3) 同步任务加 --follow 时有没有如实说"没有中间进度"，而不是假装跟了一路。

FRAME_RE = re.compile(r"^\s*\[(\d+)\]\s+(\S+)")
TERM_RE = re.compile(r"终态\s+(\S+)")


def _frames(out):
    """抽出 (seq, status) 序列。只认 CLI 自己的帧格式，不猜别的行。"""
    seqs = []
    for line in out.splitlines():
        m = FRAME_RE.match(line)
        if m:
            seqs.append((int(m.group(1)), m.group(2)))
    return seqs


def _terminal(out):
    m = TERM_RE.search(out)
    return m.group(1) if m else None


def group_f(rep, ctx):
    exe = ctx.exe
    env = ctx.env()

    # F1/F2：正常任务跟到底
    rc, out = run_cli(exe, ["submit", "写一个 hello world 函数", "--mode", "async",
                            "--follow"], env, timeout=200)
    frames = _frames(out)
    term = _terminal(out)
    g(rep, ctx, "F", "F1 真的收到多帧（首帧 seq=0）",
      len(frames) >= 2 and frames[0][0] == 0,
      "帧数=%d 序列=%s" % (len(frames), [s for s, _ in frames][:8]))
    g(rep, ctx, "F", "F2 成功任务：终态 succeeded 且 rc=0",
      term == "succeeded" and rc == RC_OK,
      "终态=%s rc=%s" % (term, rc))

    # F3：帧序号必须严格递增。原地重复的 seq 意味着一帧被回放多次——
    # 这正是"重连后拿快照拼时间线"的典型长相，肉眼看不出来。
    mono = all(b[0] > a[0] for a, b in zip(frames, frames[1:])) if len(frames) > 1 else False
    g(rep, ctx, "F", "F3 帧序号严格递增（无回放）", mono,
      "序列=%s" % [s for s, _ in frames][:8])

    # F4：失败任务必须非 0 退出。这一条是整个子命令的价值所在：
    # 若"看到终态"就返回 0，那 `submit --follow && 干活` 会在失败任务上继续往下跑。
    # 预算数字与 runner 步数耦合（sim runner 单步 500~3600 tokens），runner 改了要同步这里。
    rc2, out2 = run_cli(exe, ["submit", "写一个 hello world 函数", "--mode", "async",
                              "--follow", "--budget-tokens", "200"], env, timeout=200)
    term2 = _terminal(out2)
    g(rep, ctx, "F", "F4 非成功终态时 rc 非 0",
      term2 is not None and term2 != "succeeded" and rc2 != RC_OK,
      "终态=%s rc=%s" % (term2, rc2))

    # F5：同步任务 + --follow 不许装样子。同步跑完没有中间态，
    # 静默返回 0 会让人以为"跟完了、一切正常"，实际什么都没跟。
    #
    # 目标句必须命中类型关键词（"总结"→writing）。首版写成"说明 tcp 与 udp 的区别"，
    # 未命中任何信号被 422 拒在门口，红色落在了门禁上——却跟 follow 毫无关系。
    # 用例设计错和被测对象错长得一样，只有把 goal 换回可识别类型才能分清。
    rc3, out3 = run_cli(exe, ["submit", "写一句话总结 tcp 与 udp 的区别",
                              "--mode", "sync", "--follow"], env, timeout=200)
    g(rep, ctx, "F", "F5 同步+follow 如实说无进度可跟",
      "没有中间进度可跟" in out3 and not _frames(out3) and rc3 == RC_OK,
      "提示=%s 帧数=%d rc=%s 原文=%s"
      % ("没有中间进度可跟" in out3, len(_frames(out3)), rc3,
         " ".join(out3.split())[:150]))

    # F6：--json 时每一行都得是独立可解析的 JSON——脚本要能 jq 逐行读。
    # 只要混进一行人类提示，管道下游就断在那一行上。
    rc4, out4 = run_cli(exe, ["submit", "写一个 hello world 函数", "--mode", "async",
                              "--follow", "--json"], env, timeout=200)
    docs, bad = [], []
    for line in out4.splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            docs.append(json.loads(s))
        except Exception:  # noqa: BLE001
            bad.append(s[:40])
    last_status = docs[-1].get("status") if docs and isinstance(docs[-1], dict) else None
    g(rep, ctx, "F", "F6 --json 每行独立可解析且末行是终态",
      not bad and len(docs) >= 2 and last_status in ("succeeded", "failed",
                                                     "aborted_budget", "cancelled"),
      "行数=%d 坏行=%s 末状态=%s" % (len(docs), bad or "无", last_status))


# ---------------------------------------------------------------- 主流程
def bootstrap(ctx, rep):
    """起服务 + 注册账号 + login。服务起不来时全部记红并继续往下跑（drill 需要）。"""
    port = find_free_port()
    ctx.token_file = ctx.workdir / "token"
    ctx.proc, ctx.log, msg = start_service(ctx.exe, port, ctx.workdir / "data", ctx.workdir / "srv.log",
                                           wait=8.0 if DRILL else 30.0)
    ctx.alive = msg == "就绪"
    print("[BOOT] 服务：%s（%s）" % ("就绪" if ctx.alive else "未起来", msg))
    if not ctx.alive:
        return
    ctx.port = port
    ctx.hostport = "127.0.0.1:%d" % port
    ctx.base = "http://127.0.0.1:%d" % port

    reg = {"username": DEFAULT_ACCOUNT, "email": DEFAULT_ACCOUNT + "@example.com",
           "password": DEFAULT_PASSWORD, "display_name": "CLI HTTP 探针"}
    for _ in range(40):
        st, body = http_json(ctx.base, "POST", "/api/v1/auth/register", reg, timeout=20)
        if st in (200, 201, 409):
            toks = body.get("tokens") or {}
            ctx.reg_token = toks.get("access_token") or body.get("access_token") or ""
            break
        time.sleep(0.5)
    print("[BOOT] 注册：token=%s 位" % (len(ctx.reg_token) or "0"))


DRILL = False


def main():
    global DRILL
    ap = argparse.ArgumentParser(description="门禁 23：CLI 客户端子命令（login/ls/show/submit）行为探针")
    ap.add_argument("--exe", default=str(DEFAULT_EXE), help="被测可执行文件")
    ap.add_argument("--fast", action="store_true", help="跳过并发刷新项（省约 10s）")
    ap.add_argument("--drill", action="store_true", help="负向演练：换一个不是本服务的程序，确认门禁会成片变红")
    ap.add_argument("--json", default=None, help="把结果写成 JSON")
    args = ap.parse_args()

    exe = pathlib.Path(args.exe)
    if args.drill:
        DRILL = True
        exe = pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "where.exe"
        if not exe.exists():
            print("[ERR] 演练用程序不存在：%s" % exe)
            return 2
    elif not exe.exists():
        print("[ERR] 被测可执行文件不存在：%s" % exe)
        print("      先跑 scripts/build-exe.py 或 scripts/sync_build.py build")
        return 2

    workdir = pick_workdir()
    print("门禁 23 · CLI 客户端子命令探针" + ("（演练模式）" if args.drill else ""))
    print("被测：%s" % exe)
    print("现场：%s" % workdir)
    print("-" * 78)

    ctx = Ctx(exe, workdir)
    rep = Report()
    try:
        bootstrap(ctx, rep)
        group_a(rep, ctx)
        # --fast 只砍并发那一项（8 进程 × 3 轮，约 10s）。
        # 其余一律照跑：D 组要靠登录后的令牌，若把 B/C 也跳过，
        # D 组会因为"没登录"整片红，快是快了但门禁变成假的。
        group_b(rep, ctx)
        group_c(rep, ctx)
        if not args.fast and not args.drill:
            group_c_race(rep, ctx)
        group_d(rep, ctx)
        group_e(rep, ctx)
        group_f(rep, ctx)
    finally:
        stop(ctx.proc, ctx.log)

    print("-" * 78)
    print(rep.summary())

    if args.drill:
        red = len(rep.failed)
        if red >= 8:
            print("[DRILL-OK] 换掉被测程序后有 %d 项转红，说明判据有判定力。" % red)
            result = 0
        else:
            print("[DRILL-BAD] 只有 %d 项转红——门禁在空跑，断言没咬住东西。" % red)
            result = 1
    else:
        result = 1 if rep.failed else 0
        if rep.failed:
            print("失败明细：")
            for r in rep.failed:
                print("  - [%s] %s：%s" % (r["group"], r["name"], r["detail"]))

    if args.json:
        payload = {"exe": str(exe), "drill": args.drill, "result": result,
                   "workdir": str(workdir), "rows": rep.rows,
                   "summary": {"total": len(rep.rows), "failed": len(rep.failed)}}
        pathlib.Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print("已写 %s" % args.json)

    return result


if __name__ == "__main__":
    sys.exit(main())
