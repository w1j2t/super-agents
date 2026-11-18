#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_cli.py —— CLI 外壳（serve / doctor）行为探针（门禁 22）。

为什么需要这一道，而前面 21 道都不够：
  门禁 2/3 冒烟、8 执行端口、14 响应形状……它们全部以「[exe] 无参」启动被测程序，
  也就是默认走 serve。这个约定一旦被打破（例如加了 CLI 却没把「无子命令」映射成
  serve），那 21 道会集体变红——但它们只会告诉你"服务起不来"，不会告诉你
  **默认语义被改了**，排查得从 main.rs 重新看一遍。
  这一道的价值就是把"默认行为不许变"做成一条随时可跑的断言。
  同时 doctor 是运维在"服务起不来"时唯一的入口，它的退出码是脚本判据而非散文——
  退出码三分、JSON 纯净性、端口"自己/他人"区分，都必须钉死。

四组断言（失效方式各不相同，所以分组）：
  A 参数面    子命令与参数是否真的存在，拼错会不会被拒绝。快、不起进程。
  B 默认语义  无参必须等价 serve、显式 serve 也必须能起。—— 旧脚本的隐含契约。
  C 机器契约  doctor --json 必须是纯 JSON（stdout 不能被日志污染）、字段齐全、
              检查项名单与顺序固定、文本与 JSON 两种输出不许互相打架。
  D 判据有效  退出码 0/1/2 在真实场景里真的会分开；doctor 只读
              （不留监听、不写业务数据）。

退出码：0 = 全部通过；1 = 有 failed；2 = 脚本自身配置错（--exe 不存在等）。

用法：
  python scripts/probe_cli.py
  python scripts/probe_cli.py --fast     # 只跑 A/C 组（不起进程，约 2s）
  python scripts/probe_cli.py --drill    # 负向演练：换一个不像 CLI 的程序，确认本门禁真会红
  python scripts/probe_cli.py --json out.json
"""

import argparse
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]

try:
    from sync_build import resolve_build

    DEFAULT_EXE = resolve_build() / "target-gnu" / "debug" / "super-agents.exe"
except Exception:  # noqa: BLE001
    DEFAULT_EXE = (pathlib.Path(os.environ.get("LOCALAPPDATA", ".")) / "sa-build"
                   / "target-gnu" / "debug" / "super-agents.exe")

# collect() 的产出顺序就是这张表。顺序本身也是契约：
# 脚本里若假定"第三行是审计"，换个顺序它就会读到限流。
CHECK_NAMES = ["配置加载", "数据目录", "存储槽位", "审计槽位", "限流槽位",
               "指标槽位", "执行槽位", "监听端口", "模型端点", "集群心跳"]

# 同一个级别有两套写法，这不是不统一，是两种读者：
#   JSON 给小写机器值（方便 jq/grep，不必按大小写敏感去猜）；
#   文本给大写标签，且方括号内补空格对齐（[OK  ] / [WARN] / [SKIP] / [ERR ]），
#   让人一眼看出十行里哪一行不一样。
# 关键不是"统一"，而是两套表示必须能逐项对上，这才是 C6 要钉的东西。
LEVELS = {"ok", "warn", "skip", "error"}
LEVEL_LABEL = {"ok": "OK", "warn": "WARN", "skip": "SKIP", "error": "ERR"}


def text_level(out, level, name):
    """在 doctor 文本输出里找"某个检查项是否为某个级别"。

    直接比对 "[ERR] 模型端点" 会失败：真实输出是 "[ERR ] 模型端点"（方括号内补齐对齐）。
    所以这里容忍方括号内任意空格，也只认同一行里标签与名称的配对，
    避免"文件里出现过 ERR"就算命中这种假绿。
    """
    pat = r"\[\s*%s\s*\]\s*%s" % (LEVEL_LABEL[level], re.escape(name))
    return re.search(pat, out) is not None

PORT_BASE = 18310


def find_free_port(start=PORT_BASE, tries=60):
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
            except OSError:
                continue
            return p
    raise RuntimeError("在 %d~%d 之间找不到空闲端口" % (start, start + tries))


def port_is_free(port, timeout=1.0):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def http_get(addr, path, timeout=2.0):
    try:
        with urllib.request.urlopen("http://%s%s" % (addr, path), timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code          # 4xx/5xx 同样证明"活着"，要回真实码
    except Exception:          # noqa: BLE001
        return None


def clean_env(extra=None):
    """每条断言都在干净环境里跑：外部 shell 残留的 SA_* 会让结论不可复现。"""
    env = dict(os.environ)
    for k in [k for k in env if k.startswith("SA_")]:
        env.pop(k, None)
    env["PYTHONIOENCODING"] = "utf-8"
    if extra:
        env.update(extra)
    return env


def run_cli(exe, args, env_extra=None, timeout=60):
    try:
        r = subprocess.run([str(exe)] + list(args), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=clean_env(env_extra),
                           timeout=timeout)
        return r.returncode, (r.stdout or ""), (r.stderr or "")
    except subprocess.TimeoutExpired:
        return None, "", "超时（%ss）" % timeout


def start_service(exe, port, data_dir, wait=25.0):
    """起服务并等到 /healthz 200。返回 (Popen|None, 状态码, 说明)。"""
    env = clean_env({"SA_BIND": "127.0.0.1:%d" % port,
                     "SA_DATA_DIR": str(data_dir),
                     "SA_JWT_SECRET": "x" * 40})
    proc = subprocess.Popen([str(exe)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", env=env,
                            cwd=str(ROOT))
    addr = "127.0.0.1:%d" % port
    deadline = time.time() + wait
    code = None
    while time.time() < deadline:
        if proc.poll() is not None:
            tail = (proc.stdout.read() if proc.stdout else "")[-300:]
            return None, None, "进程提前退出 rc=%s %s" % (proc.returncode, tail)
        code = http_get(addr, "/healthz", timeout=1.5)
        if code is not None:
            return proc, code, "就绪"
        time.sleep(0.25)
    return proc, None, "%ss 内 /healthz 无响应" % int(wait)


def stop(proc):
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


class Report:
    def __init__(self, verbose=False):
        self.rows = []
        self.verbose = verbose

    def add(self, group, name, ok, detail=""):
        ok = bool(ok)
        self.rows.append({"group": group, "name": name, "ok": ok, "detail": detail})
        print("%s %-2s %-30s %s" % ("[PASS]" if ok else "[FAIL]", group, name, detail))
        return ok

    @property
    def failed(self):
        return [r for r in self.rows if not r["ok"]]

    def summary(self):
        return "合计 %d 项 | 通过 %d | 失败 %d" % (
            len(self.rows), len(self.rows) - len(self.failed), len(self.failed))


# ------------------------------------------------------------------ A 组：参数面
def group_a(rep, exe):
    rc, out, err = run_cli(exe, ["--help"], timeout=30)
    text = out + err
    rep.add("A", "A1 --help 列出 serve/doctor",
            rc == 0 and "serve" in text and "doctor" in text,
            "rc=%s serve=%s doctor=%s" % (rc, "serve" in text, "doctor" in text))

    rc, out, err = run_cli(exe, ["doctor", "--help"], timeout=30)
    text = out + err
    rep.add("A", "A2 doctor 参数齐 --json/--strict",
            rc == 0 and "--json" in text and "--strict" in text,
            "rc=%s json=%s strict=%s" % (rc, "--json" in text, "--strict" in text))

    # 拼错子命令必须被拒绝。否则 `super-agents docotr` 会静默起一个服务，
    # 而运维以为自己在做体检——"跑了但跑的不是那个"这类事故最难发现。
    rc, out, err = run_cli(exe, ["nosuchcmd"], timeout=30)
    low = (out + err).lower()
    rep.add("A", "A3 未知子命令被拒绝不当 serve",
            rc not in (0, None) and ("unrecognized" in low or "invalid" in low or "error" in low),
            "rc=%s 首行=%s" % (rc, (err.strip().splitlines() or [""])[0][:70]))

    rc, out, err = run_cli(exe, ["--version"], timeout=30)
    rep.add("A", "A4 --version 输出版本号",
            rc == 0 and any(ch.isdigit() for ch in out),
            "rc=%s 输出=%s" % (rc, out.strip()[:40]))


# ------------------------------------------------------------------ B 组：默认语义
def group_b(rep, exe, workdir):
    # B1/B2/B3 是同一件事的三面：无参 = serve。前面 21 道门禁全靠这条隐含契约活着。
    port = find_free_port()
    d = workdir / ("b1-p%d" % port)
    d.mkdir(parents=True, exist_ok=True)
    proc, code, note = start_service(exe, port, d)
    rep.add("B", "B1 无参启动 /healthz 200", code == 200, "HTTP=%s %s" % (code, note))
    rep.add("B", "B2 无参进程保持前台", proc is not None and proc.poll() is None,
            "存活=%s" % (proc is not None and proc.poll() is None))
    stop(proc)
    time.sleep(0.8)
    rep.add("B", "B3 退出后端口释放", port_is_free(port), "端口 %d 已释放" % port)

    # 显式 serve 也得能用，否则是"只对了默认分支"
    port2 = find_free_port()
    d2 = workdir / ("b4-p%d" % port2)
    d2.mkdir(parents=True, exist_ok=True)
    proc2, code2, note2 = start_service(exe, port2, d2)
    rep.add("B", "B4 显式 serve 同样可用", code2 == 200, "HTTP=%s %s" % (code2, note2))
    stop(proc2)


# ------------------------------------------------------------------ C 组：机器契约
def group_c(rep, exe, workdir):
    d = workdir / "c-json"
    d.mkdir(parents=True, exist_ok=True)
    env = {"SA_DATA_DIR": str(d), "SA_JWT_SECRET": "x" * 40,
           "SA_BIND": "127.0.0.1:%d" % find_free_port()}
    rc, out, err = run_cli(exe, ["doctor", "--json"], env_extra=env, timeout=120)

    # C1 纯 JSON。stdout 混进一行日志，对人是"多一点提示"，对 CI 就是解析崩溃。
    try:
        doc = json.loads(out)
        ok1, note1 = True, "可解析"
    except Exception as e:  # noqa: BLE001
        doc, ok1, note1 = None, False, "解析失败：%s" % str(e)[:60]
    rep.add("C", "C1 --json stdout 是纯 JSON", ok1, note1)
    if doc is None:
        for n in ["C2 必需字段齐全", "C3 名单与顺序固定", "C4 level 取值封闭",
                  "C5 文本与 JSON 同数同码", "C6 文本标签与 JSON 逐项一致"]:
            rep.add("C", n, False, "上游 C1 失败，跳过判定")
        return

    need_top = {"version", "exit_code", "checks", "summary"}
    need_sum = {"total", "ok", "warn", "skip", "error"}
    checks = doc.get("checks") or []
    sum_ = doc.get("summary") or {}
    miss = (need_top - set(doc)) | ({"checks 为空"} if not checks else set())
    miss |= {"summary." + k for k in (need_sum - set(sum_))}
    rep.add("C", "C2 必需字段齐全", not miss, "缺 %s" % sorted(miss) if miss else "齐")

    names = [c.get("name") for c in checks]
    rep.add("C", "C3 名单与顺序固定", names == CHECK_NAMES,
            "一致" if names == CHECK_NAMES else "实际=%s" % names)

    bad = [c.get("level") for c in checks if c.get("level") not in LEVELS]
    rep.add("C", "C4 level 取值封闭", not bad,
            "越界=%s" % bad if bad else "ok/warn/skip/error")

    # C5 同一次运行，文本与 JSON 的总数/退出码不许各说各话：
    # 人读文本、机器读 JSON，两边打架时通常没人会发现，直到某天 CI 与人工结论相反。
    rc2, out2, _ = run_cli(exe, ["doctor"], env_extra=env, timeout=120)
    n = len(checks)
    rep.add("C", "C5 文本与 JSON 同数同码",
            ("合计 %d 项" % n) in out2 and rc2 == doc.get("exit_code"),
            "JSON %d 项/rc=%s，文本 rc=%s" % (n, doc.get("exit_code"), rc2))

    # C6 比 C5 更狠一层：不只比总数，而是逐项比级别。
    # 只比总数的漏洞在于——文本把"审计"写成 OK、把"心跳"写成 WARN，总数照样对得上。
    # 两类读者看到的结论不一致，却没有任何一道门禁会红。
    drift = [c["name"] for c in checks
             if c.get("level") in LEVELS and not text_level(out2, c["level"], c["name"])]
    rep.add("C", "C6 文本标签与 JSON 逐项一致", not drift,
            "漂移项=%s" % drift if drift else "%d 项逐项对上" % n)


# ------------------------------------------------------------------ D 组：判据有效
def group_d(rep, exe, workdir):
    # D1 干净环境必须 rc=0，否则 doctor 会天天报警，最后没人看。
    d1 = workdir / "d1-clean"
    d1.mkdir(parents=True, exist_ok=True)
    rc, out, _ = run_cli(exe, ["doctor", "--json"],
                         env_extra={"SA_DATA_DIR": str(d1), "SA_JWT_SECRET": "x" * 40,
                                    "SA_BIND": "127.0.0.1:%d" % find_free_port()}, timeout=120)
    rep.add("D", "D1 干净环境 rc=0", rc == 0, "rc=%s" % rc)

    # D2 --strict 只在真有 WARN 时抬到 1。没有 WARN 就不许乱抬，
    # 否则 --strict 变成"永远失败"，等于没有。
    d2 = workdir / "d2-strict"
    d2.mkdir(parents=True, exist_ok=True)
    rc, out, _ = run_cli(exe, ["doctor", "--strict", "--json"],
                         env_extra={"SA_DATA_DIR": str(d2), "SA_JWT_SECRET": "x" * 40,
                                    "SA_BIND": "127.0.0.1:%d" % find_free_port()}, timeout=120)
    try:
        warn = len([c for c in json.loads(out)["checks"] if c["level"] == "warn"])
    except Exception:  # noqa: BLE001
        warn = -1
    rep.add("D", "D2 --strict 仅在有 WARN 时 rc=1",
            (rc == 1) == (warn > 0), "WARN=%s rc=%s" % (warn, rc))

    # D3 模型端点写了但没写全（half-configured）。这类"看起来配了"是最常见的坑：
    # 服务能起、请求全挂。必须在诊断阶段就报 ERR。
    d3 = workdir / "d3-half-model"
    d3.mkdir(parents=True, exist_ok=True)
    rc, out, _ = run_cli(exe, ["doctor"],
                         env_extra={"SA_DATA_DIR": str(d3), "SA_JWT_SECRET": "x" * 40,
                                    "SA_BIND": "127.0.0.1:%d" % find_free_port(),
                                    "SA_LLM_BASE": "http://127.0.0.1:9/v1"}, timeout=120)
    rep.add("D", "D3 模型端点半配 rc=2 且报 ERR",
            rc == 2 and text_level(out, "error", "模型端点"),
            "rc=%s 命中=%s" % (rc, text_level(out, "error", "模型端点")))

    # D4 选了 process 执行器却把目录指到不存在的地方：doctor 的主要用途就是这个。
    # 注意（H2 之后）：不设 SA_RUNNER_DIR 不再等于报错——默认 process 会沿
    # cwd/workers → exe 同目录 workers 自动发现随产物分发的脚本目录。
    d4 = workdir / "d4-runner"
    d4.mkdir(parents=True, exist_ok=True)
    missing = d4 / "no-such-workers"
    rc, out, _ = run_cli(exe, ["doctor"],
                         env_extra={"SA_DATA_DIR": str(d4), "SA_JWT_SECRET": "x" * 40,
                                    "SA_BIND": "127.0.0.1:%d" % find_free_port(),
                                    "SA_RUNNER": "process",
                                    "SA_RUNNER_DIR": str(missing)}, timeout=120)
    rep.add("D", "D4 process 目录不存在 rc=2",
            rc == 2 and text_level(out, "error", "执行槽位"),
            "rc=%s 命中=%s" % (rc, text_level(out, "error", "执行槽位")))

    # D4b 默认诚实：没有任何 SA_RUNNER 配置时，doctor 必须说"真实执行"，
    # 且脚本目录能自动发现（否则默认 process 就是默认起不来）。
    d4b = workdir / "d4b-default"
    d4b.mkdir(parents=True, exist_ok=True)
    rc_b, out_b, _ = run_cli(exe, ["doctor"],
                             env_extra={"SA_DATA_DIR": str(d4b), "SA_JWT_SECRET": "x" * 40,
                                        "SA_BIND": "127.0.0.1:%d" % find_free_port(),
                                        "SA_RUNNER": ""}, timeout=120)
    rep.add("D", "D4b 缺省即真实执行且目录可发现",
            rc_b == 0 and ("runner=process" in out_b),
            "rc=%s process命中=%s" % (rc_b, "runner=process" in out_b))

    # D5/D6 "端口被占"要分得清是自己还是别人。分不清的话，
    # 用户得不到"服务已经在跑了，别重启"这句关键信息，只会反复重启。
    d5 = workdir / "d5-foreign"
    d5.mkdir(parents=True, exist_ok=True)
    port = find_free_port()
    squat = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    squat.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        squat.bind(("127.0.0.1", port))
        squat.listen(1)
        rc, out, _ = run_cli(exe, ["doctor"],
                             env_extra={"SA_DATA_DIR": str(d5), "SA_JWT_SECRET": "x" * 40,
                                        "SA_BIND": "127.0.0.1:%d" % port}, timeout=120)
    finally:
        squat.close()
    rep.add("D", "D5 端口被他人占用 rc=2", rc == 2 and "端口已占用" in out,
            "rc=%s 命中=%s" % (rc, "端口已占用" in out))

    d6 = workdir / "d6-ours"
    d6.mkdir(parents=True, exist_ok=True)
    port6 = find_free_port()
    proc, code, _ = start_service(exe, port6, d6)
    try:
        rc6, out6, _ = run_cli(exe, ["doctor"],
                               env_extra={"SA_DATA_DIR": str(d6), "SA_JWT_SECRET": "x" * 40,
                                          "SA_BIND": "127.0.0.1:%d" % port6}, timeout=120)
    finally:
        stop(proc)
    rep.add("D", "D6 端口被本服务占 → SKIP 且 rc=0",
            rc6 == 0 and text_level(out6, "skip", "监听端口"),
            "rc=%s 命中=%s" % (rc6, text_level(out6, "skip", "监听端口")))

    # D7 doctor 是只读诊断。若它自己留下监听、或往数据目录写业务文件，
    # 那"服务起不来时跑 doctor 看看"这句话就不成立了。
    d7 = workdir / "d7-readonly"
    d7.mkdir(parents=True, exist_ok=True)
    port7 = find_free_port()
    run_cli(exe, ["doctor"], env_extra={"SA_DATA_DIR": str(d7), "SA_JWT_SECRET": "x" * 40,
                                        "SA_BIND": "127.0.0.1:%d" % port7}, timeout=120)
    biz = [x.name for x in d7.rglob("*")
           if x.is_file() and not x.name.startswith("doctor")]
    rep.add("D", "D7 诊断后端口空闲、不落业务文件",
            port_is_free(port7) and not biz,
            "端口空闲=%s 多出文件=%s" % (port_is_free(port7), biz or "无"))

    # D8 密钥过短：必须是"报错退出"，不是 panic。panic 会让人以为是程序坏了。
    d8 = workdir / "d8-secret"
    d8.mkdir(parents=True, exist_ok=True)
    rc, out, err = run_cli(exe, ["doctor"],
                           env_extra={"SA_DATA_DIR": str(d8), "SA_JWT_SECRET": "short",
                                      "SA_BIND": "127.0.0.1:%d" % find_free_port()}, timeout=120)
    panicked = "panicked at" in (out + err)
    rep.add("D", "D8 密钥过短 rc=2 且不 panic",
            rc == 2 and text_level(out, "error", "配置加载") and not panicked,
            "rc=%s ERR命中=%s panic=%s" % (rc, text_level(out, "error", "配置加载"), panicked))


# ------------------------------------------------------------------ 主流程
def pick_workdir():
    """在 target/cli-probe 下递增新建 run-N。

    刻意不清理旧目录：探针要能被反复跑、每次留下现场供事后翻查；
    而"清空目录"这一步在本机是被拦截的动作，脚本里留着它等于把自己废掉。
    """
    base = ROOT / "target" / "cli-probe"
    base.mkdir(parents=True, exist_ok=True)
    for i in range(1, 1000):
        d = base / ("run-%d" % i)
        if not d.exists():
            d.mkdir(parents=True)
            return d
    raise RuntimeError("cli-probe 已积累过多运行目录，请人工归档")


def main():
    ap = argparse.ArgumentParser(description="门禁 22：CLI 外壳（serve/doctor）行为探针")
    ap.add_argument("--exe", default=str(DEFAULT_EXE), help="被测可执行文件")
    ap.add_argument("--fast", action="store_true", help="只跑 A/C 组（不起服务进程）")
    ap.add_argument("--drill", action="store_true",
                    help="负向演练：换一个不像 CLI 的程序，确认门禁会变红")
    ap.add_argument("--json", default=None, help="把结果写成 JSON")
    args = ap.parse_args()

    exe = pathlib.Path(args.exe)
    if args.drill:
        # where.exe 是货真价实的程序，但不是我们的 CLI：它的 --help 不认识 serve、
        # 也不接受 doctor。若此时门禁仍是"全绿"，说明它根本没在测东西。
        exe = pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "where.exe"
        if not exe.exists():
            print("[ERR] 演练用程序不存在：%s" % exe)
            return 2
    elif not exe.exists():
        print("[ERR] 被测可执行文件不存在：%s" % exe)
        print("      先跑 scripts/build-exe.py 或 scripts/sync_build.py build")
        return 2

    workdir = pick_workdir()
    print("门禁 22 · CLI 外壳行为探针" + ("（演练模式）" if args.drill else ""))
    print("被测：%s" % exe)
    print("现场：%s" % workdir)
    print("-" * 68)

    rep = Report()
    group_a(rep, exe)
    if args.fast:
        print("-" * 68)
        print("（--fast：跳过 B/C/D 组）")
    else:
        group_c(rep, exe, workdir)
        if not args.drill:
            group_b(rep, exe, workdir)
            group_d(rep, exe, workdir)
        else:
            # 演练时 B 组必然起不来，D 组的结论也没有意义；只保留 C 组，
            # 让"红"来自 A/C 的成片失败而不是一堆超时，读起来干净。
            print("[drill] B/D 组依赖真实服务，演练模式跳过")

    print("-" * 68)
    print(rep.summary())

    if args.drill:
        red_a = len([r for r in rep.rows if r["group"] == "A" and not r["ok"]])
        red_other = len([r for r in rep.rows if r["group"] != "A" and not r["ok"]])
        red = red_a + red_other
        if red >= 3:
            print("[DRILL-OK] 换掉被测程序后有 %d 项转红（A 组 %d），说明判据有判定力。"
                  % (red, red_a))
            result = 0
        else:
            print("[DRILL-BAD] 只有 %d 项转红（A 组 %d）——门禁在空跑，断言没咬住东西。"
                  % (red, red_a))
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
        pathlib.Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print("已写 %s" % args.json)

    return result


if __name__ == "__main__":
    sys.exit(main())
