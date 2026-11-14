#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""审计启动自检实测：证明"审计真的写得进去"，而不是"配置里写着 file"。

为什么单独有这么一个脚本：
    `record()` 按契约不返回错误（审计失败不许阻断业务），所以磁盘满、目录权限被改、
    网络盘掉线这些情况在**运行期**只会留下一条 warn 日志，从外部完全看不出来。
    启动自检把这件事提到启动时刻：真写一条 `audit.boot`，再重新打开文件读回来比对。
    写不进 → 拒绝启动。本脚本就是给这条机制出具证据的。

验证的四个事实（每个都取自真实进程，不看代码猜）：
    1  SA_AUDIT=file 的实例能起来，且 `/readyz` 如实体报告 audit.durable=true / degraded=false
    2  `audit.jsonl` 里真的多出一条 `audit.boot`，并且能从审计 API 里查到（不只是磁盘上有）
    3  重启一次 → boot 记录累加到 2 条（台账顺带成了"重启时间线"）
    4  路径不可用时进程**拒绝启动**（rc != 0，日志给出原因），而不是带病跑起来
    5  SA_AUDIT=memory 的实例不假装落盘：durable=false、不产生 audit.jsonl

用法：
    python scripts/audit-probe-test.py --exe <path-to-super-agents.exe>
退出码 0 = 全部通过；1 = 有失败项。
"""

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

OK, FAIL = "PASS", "FAIL"
rows = []


def rec(case, name, ok, detail=""):
    rows.append((case, name, OK if ok else FAIL, detail))
    print("[%s] %s | %s%s" % ("OK  " if ok else "FAIL", case, name,
                              ("  ->  " + detail) if detail else ""), flush=True)


def call(base, method, path, body=None, token=None):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(base + path, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    except Exception as e:  # noqa: BLE001 - 连接失败也要能报出来
        return 0, {"transport_error": repr(e)}
    text = raw.decode("utf-8", "replace")
    try:
        return status, (json.loads(text) if text else None)
    except Exception:  # noqa: BLE001
        return status, text


def wait_http(base, path, seconds=45):
    t0 = time.time()
    while time.time() - t0 < seconds:
        status, _ = call(base, "GET", path)
        if status == 200:
            return True
        time.sleep(0.2)
    return False


def boot_lines(audit_file):
    """读台账里所有 start 记录。坏行跳过：这跟服务端加载逻辑保持同一口径。"""
    if not audit_file.exists():
        return []
    out = []
    for line in audit_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if ev.get("action") == "audit.boot":
            out.append(ev)
    return out


def start(exe, port, data_dir, log_path, extra_env):
    env = dict(os.environ)
    env["SA_DATA_DIR"] = str(data_dir)
    env["SA_BIND"] = "127.0.0.1:%d" % port
    env.pop("SA_AUDIT", None)
    env.pop("SA_AUDIT_FILE", None)
    env.update(extra_env)
    log = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen([str(exe)], env=env, stdout=log, stderr=subprocess.STDOUT)
    return proc, log


def stop(proc, log):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()
    try:
        log.close()
    except Exception:  # noqa: BLE001
        pass


def case_file_sink(exe, root):
    """场景 1-3：file 后端真落盘、boot 可查、重启累加。"""
    case = "A file 后端"
    data_dir = root / "data-file"
    log_path = root / "file.log"
    port = 18131
    base = "http://127.0.0.1:%d" % port
    audit_file = data_dir / "audit.jsonl"

    proc, log = start(exe, port, data_dir, log_path, {"SA_AUDIT": "file"})
    try:
        up = wait_http(base, "/readyz")
        rec(case, "实例启动并就绪", up, "端口 %d" % port)
        if not up:
            return
        status, body = call(base, "GET", "/readyz")
        audit = (body or {}).get("audit") or {}
        rec(case, "/readyz 如实报告审计状态",
            status == 200 and audit.get("backend") == "file" and audit.get("durable") is True
            and audit.get("degraded") is False,
            json.dumps(audit, ensure_ascii=False))

        # 审计 API 里也要能看到 boot —— 磁盘上有、接口查不到等于运维看不见
        uname = "probe_%d" % int(time.time())
        status, body = call(base, "POST", "/api/v1/auth/register",
                            {"username": uname, "email": uname + "@example.com",
                             "password": "ProbePass!2026"})
        token = ((body or {}).get("tokens") or {}).get("access_token") if status == 201 else None
        rec(case, "注册 owner 账号", status == 201, "实际=%s" % status)
        status, body = call(base, "GET", "/api/v1/observability/audit?limit=20", token=token)
        items = (body or {}).get("items") or []
        boot_in_api = [it for it in items if it.get("action") == "audit.boot"]
        rec(case, "审计 API 能查到启动记录", status == 200 and len(boot_in_api) == 1,
            "实际=%s 命中=%d" % (status, len(boot_in_api)))
        rec(case, "启动记录不被计入拒绝数",
            (body or {}).get("denied") == 0, "denied=%s" % (body or {}).get("denied"))

        boots = boot_lines(audit_file)
        rec(case, "audit.jsonl 里存在 boot 行", len(boots) == 1,
            "命中=%d 文件=%s" % (len(boots), audit_file.name))
        if boots:
            ev = boots[0]
            rec(case, "boot 行内容完整（system/allow/路径可读）",
                ev.get("actor") == "system" and ev.get("decision") == "allow" and bool(ev.get("id")),
                "actor=%s decision=%s" % (ev.get("actor"), ev.get("decision")))
    finally:
        stop(proc, log)

    # 场景 3：同一个数据目录再起一次 —— boot 应当累加，成为重启时间线
    proc, log = start(exe, port, data_dir, log_path, {"SA_AUDIT": "file"})
    try:
        up = wait_http(base, "/readyz")
        rec(case, "重启后就绪", up)
        boots = boot_lines(audit_file)
        rec(case, "每次启动都留下一条 boot（重启可追溯）", len(boots) == 2,
            "累计=%d" % len(boots))
        ids = {b.get("id") for b in boots}
        rec(case, "两条 boot 是不同事件（id 不重复）", len(ids) == len(boots),
            "唯一 id=%d" % len(ids))
    finally:
        stop(proc, log)


def case_refuse_start(exe, root):
    """场景 4：审计路径不可用 → 拒绝启动，而不是降级成内存后继续跑。"""
    case = "B 拒绝启动"
    data_dir = root / "data-refuse"
    log_path = root / "refuse.log"
    # 造一个"占了路径的目录"：open(append) 必然失败，等价于磁盘/权限出问题的效果
    bad = root / "bad-audit.jsonl"
    bad.mkdir(parents=True, exist_ok=True)

    proc, log = start(exe, 18132, data_dir, log_path,
                      {"SA_AUDIT": "file", "SA_AUDIT_FILE": str(bad)})
    try:
        t0 = time.time()
        while proc.poll() is None and time.time() - t0 < 30:
            time.sleep(0.2)
        rc = proc.poll()
        rec(case, "不可用路径下进程拒绝启动", rc is not None and rc != 0, "退出码=%s" % rc)
        text = log_path.read_text(encoding="utf-8", errors="replace")
        hit = [l.strip() for l in text.splitlines() if "审计" in l]
        rec(case, "日志给出可定位的原因",
            bool(hit) and ("不可用" in text or "自检" in text),
            " / ".join(hit[:1]))
    finally:
        stop(proc, log)


def case_memory_sink(exe, root):
    """场景 5：memory 后端不假装落盘。"""
    case = "C memory 后端"
    data_dir = root / "data-memory"
    log_path = root / "memory.log"
    port = 18133
    base = "http://127.0.0.1:%d" % port
    proc, log = start(exe, port, data_dir, log_path, {"SA_AUDIT": "memory"})
    try:
        up = wait_http(base, "/readyz")
        rec(case, "实例就绪", up)
        if not up:
            return
        status, body = call(base, "GET", "/readyz")
        audit = (body or {}).get("audit") or {}
        rec(case, "如实报告不落盘（durable=false / degraded=false）",
            audit.get("backend") == "memory" and audit.get("durable") is False
            and audit.get("degraded") is False,
            json.dumps(audit, ensure_ascii=False))
        rec(case, "内存后端不产生 audit.jsonl", not (data_dir / "audit.jsonl").exists())
    finally:
        stop(proc, log)


def main():
    ap = argparse.ArgumentParser()
    default_exe = (pathlib.Path(os.environ.get("LOCALAPPDATA", ".")) / "sa-build"
                   / "target-gnu" / "debug" / "super-agents.exe")
    ap.add_argument("--exe", default=str(default_exe))
    ap.add_argument("--keep", action="store_true", help="保留临时目录（排查用）")
    args = ap.parse_args()

    exe = pathlib.Path(args.exe)
    if not exe.exists():
        print("找不到可执行文件：%s" % exe)
        return 2

    root = pathlib.Path(tempfile.mkdtemp(prefix="sa-audit-probe-"))
    print("工作目录：%s" % root)
    try:
        case_file_sink(exe, root)
        case_refuse_start(exe, root)
        case_memory_sink(exe, root)
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)

    failed = [r for r in rows if r[2] == FAIL]
    print("\n" + "=" * 74)
    print("合计 %d 项 | 通过 %d | 失败 %d" % (len(rows), len(rows) - len(failed), len(failed)))
    for case, name, _, detail in failed:
        print("  FAIL %s | %s | %s" % (case, name, detail))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
