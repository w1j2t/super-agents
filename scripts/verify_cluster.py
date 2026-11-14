#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_cluster.py —— 多节点集群的真人实录（真的起两个进程）。

回答一个问题：`GET /api/v1/cluster` 在**真有两台进程**时，是否如实反映
"谁在线、谁在扛活、谁已经死了"？

单测只能证明函数级逻辑（`peers()` 认得自己、TTL 判得准），证明不了
"两个独立进程通过共享目录互相看见"——那取决于环境变量解析、心跳循环是否
真的在跑、装配顺序对不对，这一层只有真进程能验。

演练 8 个场景：
  C1  单节点起步：只有自己，`source=local`，`offline_nodes=0`
  C2  双节点互通：A、B 各自的 `/cluster` 都能看见对方，且自己永远排第一
  C3  跨节点负载：去 B 上提交任务，A 侧汇总精确等于 B 的提交数
  C4  心跳实时性：B 再提交几个，一个心跳周期内 A 侧数字跟着涨
  C5  崩溃掉线：强杀 B（不走优雅退出）后 B 标 `stale`、计入 `offline_nodes`，
      但**不计入 totals**——陈旧值混进汇总会让"集群正在跑几个任务"永远不下降
  C6  遗物清理：超过 6×TTL 的孤儿心跳文件被物理删除，节点从列表消失
  C7  优雅退出：Ctrl+Break（真实用户按 Ctrl+C 的同一路径）后心跳文件立刻消失，
      别人不必等 TTL 才发现"它走了"
  C8  单机开关：`SA_CLUSTER=off` 的进程既不写也不读，`/cluster` 只报自己

为什么 C5 要强杀：TerminateProcess 不给进程任何收尾机会，正是"断电 / 蓝屏"
的最坏情况。**能被 TTL 兜住的，就不该依赖优雅退出**——优雅退出是加分项，
不是正确性前提。脚本按这个顺序验，就是为了证明这两条路各自都成立。

用法：
  python scripts/verify_cluster.py
  python scripts/verify_cluster.py --exe <绝对路径> --skip-prune   # 跳过 30s 清理等待
  python scripts/verify_cluster.py --keep                          # 保留临时数据目录

退出码：0 = 全部通过；1 = 有失败项。
"""

import argparse
import datetime as dt
import json
import os
import pathlib
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

try:  # Windows 控制台默认 GBK，中文断言失败信息会炸成 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_EXE = ROOT.parent / "build_sa1" / "target-gnu" / "debug" / "super-agents.exe"
METRICS_TOKEN = "cluster-drill-metrics-token-0123456789"
# 心跳周期取 1s（clamp 下限）、TTL 取 5s（clamp 下限）：
# 演练要等 TTL 过期，用默认的 20s 会让 C5/C6 白等一分钟。
TTL_SECS = 5
HEARTBEAT_SECS = 1
# 6 × TTL：cluster_registry.rs 的 PRUNE_TTL_FACTOR
PRUNE_WAIT_SECS = TTL_SECS * 6

RESULTS = []


# ---------------------------------------------------------------- 基础工具

def rec(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("[%s] %s%s" % ("OK  " if ok else "FAIL", name, ("  -> " + str(detail)) if detail else ""))


def expect(name, got, want, detail=""):
    rec(name, got in want, detail or ("实际=%s 期望=%s" % (got, want)))


def free_port():
    """让系统分配一个空闲端口，避免固定端口被占导致"假失败"。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def call(base, method, path, body=None, token=None, headers=None, timeout=20):
    url = base + path
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    if token:
        hdrs["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace")
            code = resp.status
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        code = e.code
    except Exception as e:
        return 0, "连接失败: %s" % e
    try:
        return code, json.loads(text)
    except Exception:
        return code, text


def parse_utc(s):
    """把响应里的 ISO 时刻转成本地可比较的 datetime（带时区）。"""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return dt.datetime.fromisoformat(s)


def sleep_until(ts, extra=0.0, cap=60.0):
    delay = (ts - dt.datetime.now(dt.timezone.utc)).total_seconds() + extra
    delay = max(0.0, min(delay, cap))
    time.sleep(delay)


# ---------------------------------------------------------------- 进程管理

class Node:
    """一个真进程 + 它的数据目录 / 端口 / 日志。"""

    def __init__(self, name, exe, root, env_extra=None, cluster_on=True):
        self.name = name
        self.port = free_port()
        self.base = "http://127.0.0.1:%d" % self.port
        self.dir = root / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log = self.dir / "server.log"
        self.proc = None
        self.env_extra = dict(env_extra or {})
        self.cluster_on = cluster_on
        self.exe = exe

    def start(self):
        env = dict(os.environ)
        env.update({
            "SA_DATA_DIR": str(self.dir),
            "SA_BIND": "127.0.0.1:%d" % self.port,
            "SA_METRICS_TOKEN": METRICS_TOKEN,
            "SA_NODE_ID": self.name,
            "SA_CLUSTER": "on" if self.cluster_on else "off",
            "SA_CLUSTER_TTL_SECS": str(TTL_SECS),
            "SA_CLUSTER_HEARTBEAT_SECS": str(HEARTBEAT_SECS),
            # 演练目录是本次会话独占的，显式指定避免误读真实数据目录下的心跳
            "SA_CLUSTER_DIR": str(SHARED_DIR),
            "RUST_LOG": "info",
        })
        env.update(self.env_extra)
        flags = 0
        if os.name == "nt":
            # 独立进程组：后面的 CTRL_BREAK 只会打到自己，不会波及本脚本
            flags = subprocess.CREATE_NEW_PROCESS_GROUP
        self.proc = subprocess.Popen(
            [str(self.exe)], env=env, cwd=str(self.dir),
            stdout=open(self.log, "wb"), stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        return self

    def wait_ready(self, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            code, _ = call(self.base, "GET", "/readyz", timeout=3)
            if code == 200:
                return True
            if self.proc.poll() is not None:
                raise RuntimeError("%s 启动即退出（rc=%s），日志尾部：\n%s"
                                   % (self.name, self.proc.returncode, self.tail()))
            time.sleep(0.2)
        raise RuntimeError("%s 在 %ss 内未就绪，日志尾部：\n%s" % (self.name, timeout, self.tail()))

    def login(self):
        """注册首用户（首用户即 owner，才有读 /cluster 的权限）。"""
        stamp = "%s_%d" % (self.name.replace("-", "_"), int(time.time() * 1000) % 1000000)
        code, body = call(self.base, "POST", "/api/v1/auth/register", {
            "username": stamp,
            "email": "%s@example.com" % stamp,
            "password": "Dr1ll!Passw0rd",
            "display_name": "Drill %s" % self.name,
        })
        if code != 201:
            raise RuntimeError("%s 注册失败 rc=%s body=%s" % (self.name, code, body))
        self.token = body["tokens"]["access_token"]
        self.is_owner = body["user"]["role"] == "owner"
        return self.token

    def cluster(self):
        return call(self.base, "GET", "/api/v1/cluster", token=self.token)

    def submit(self, goal):
        return call(self.base, "POST", "/api/v1/tasks",
                    {"mode": "sync", "goal": goal, "task_type": "coding"}, token=self.token)

    def tail(self, n=25):
        try:
            lines = self.log.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(lines[-n:])
        except Exception:
            return "<无日志>"

    def kill_ungraceful(self):
        """TerminateProcess：不给收尾机会，模拟断电。"""
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=15)
            except Exception:
                pass

    def stop_graceful(self):
        """Ctrl+Break：与用户按 Ctrl+C 走同一条优雅退出路径。"""
        if self.proc and self.proc.poll() is None:
            try:
                os.kill(self.proc.pid, signal.CTRL_BREAK_EVENT)
            except Exception as e:
                rec("C7 发送 Ctrl+Break 失败", False, e)
                self.proc.terminate()
            try:
                return self.proc.wait(timeout=20)
            except Exception:
                self.proc.kill()
                return None
        return self.proc.returncode if self.proc else None


def heartbeat_file(node_id):
    """心跳文件名规则与 cluster_registry.rs 的 sanitize() 一致。"""
    safe = "".join(ch if (ch.isascii() and (ch.isalnum() or ch in "._-")) else "_"
                   for ch in node_id)[:96] or "node"
    return SHARED_DIR / (safe + ".json")


def find_node(view, node_id):
    for n in (view or {}).get("nodes") or []:
        if n.get("node_id") == node_id:
            return n
    return None


# ---------------------------------------------------------------- 演练主体

def run(exe, skip_prune):
    root = pathlib.Path(tempfile.mkdtemp(prefix="sa-cluster-drill-"))
    global SHARED_DIR
    SHARED_DIR = root / "shared-cluster"
    SHARED_DIR.mkdir(parents=True, exist_ok=True)

    print("演练目录: %s" % root)
    print("共享心跳目录: %s" % SHARED_DIR)
    print("被测二进制: %s" % exe)
    print("心跳 TTL=%ss  周期=%ss  清理线=%ss\n" % (TTL_SECS, HEARTBEAT_SECS, PRUNE_WAIT_SECS))

    a = Node("node-a", exe, root)
    b = Node("node-b", exe, root)
    off = Node("node-off", exe, root, cluster_on=False)

    try:
        # ---------------- C1 单节点起步
        print("--- C1 单节点起步 ---")
        a.start()
        a.wait_ready()
        a.login()
        rec("C1 首用户拿到 owner 角色（否则读不了 /cluster）", a.is_owner, "role=%s" % ("owner" if a.is_owner else "member"))

        # 刚起来的进程还没写过心跳，先给一个周期让它落盘
        time.sleep(HEARTBEAT_SECS + 1.2)
        code, v = a.cluster()
        expect("C1 /cluster 返回 200", code, [200])
        nodes = (v or {}).get("nodes") or []
        expect("C1 只有自己一个节点", len(nodes), [1])
        rec("C1 自己是 source=local", nodes and nodes[0].get("source") == "local",
            "source=%s" % (nodes[0].get("source") if nodes else None))
        rec("C1 自己的 node_id 用的是 SA_NODE_ID", nodes and nodes[0].get("node_id") == "node-a",
            "node_id=%s" % (nodes[0].get("node_id") if nodes else None))
        rec("C1 单节点时 offline_nodes=0", (v or {}).get("totals", {}).get("offline_nodes") == 0,
            "offline_nodes=%s" % (v or {}).get("totals", {}).get("offline_nodes"))
        rec("C1 心跳文件已落盘", heartbeat_file("node-a").exists(),
            heartbeat_file("node-a").name)
        expect("C1 目录里此刻只有自己一份心跳（peer 的心跳不该凭空出现）",
               len(list(SHARED_DIR.glob("*.json"))), [1])

        # ---------------- C2 双节点互通
        print("\n--- C2 双节点互通 ---")
        b.start()
        b.wait_ready()
        b.login()
        time.sleep(HEARTBEAT_SECS + 1.2)

        code, va = a.cluster()
        code_b, vb = b.cluster()
        expect("C2 A 侧 /cluster 返回 200", code, [200])
        expect("C2 B 侧 /cluster 返回 200", code_b, [200])

        na = (va or {}).get("nodes") or []
        nb = (vb or {}).get("nodes") or []
        expect("C2 A 看见 2 个节点", len(na), [2])
        expect("C2 B 看见 2 个节点", len(nb), [2])
        rec("C2 A 的列表里自己排第一（刷新不该换位置）",
            na and na[0].get("node_id") == "node-a" and na[0].get("source") == "local",
            "首位=%s/%s" % (na[0].get("node_id") if na else None, na[0].get("source") if na else None))
        rec("C2 B 的列表里自己排第一",
            nb and nb[0].get("node_id") == "node-b" and nb[0].get("source") == "local",
            "首位=%s/%s" % (nb[0].get("node_id") if nb else None, nb[0].get("source") if nb else None))

        peer_b_in_a = find_node(va, "node-b")
        rec("C2 A 把 B 认成 peer（不是 local）", peer_b_in_a is not None and peer_b_in_a.get("source") == "peer",
            "source=%s" % (peer_b_in_a.get("source") if peer_b_in_a else None))
        rec("C2 A 侧看到的 B 不 stale", peer_b_in_a is not None and peer_b_in_a.get("stale") is False,
            "stale=%s" % (peer_b_in_a.get("stale") if peer_b_in_a else None))
        rec("C2 peer 节点带 updated_at（看板才能显示'几秒前'）",
            bool(peer_b_in_a and peer_b_in_a.get("updated_at")),
            "updated_at=%s" % (peer_b_in_a.get("updated_at") if peer_b_in_a else None))

        ta = (va or {}).get("totals") or {}
        expect("C2 汇总节点数=2", ta.get("nodes"), [2])
        rec("C2 汇总 offline_nodes=0", ta.get("offline_nodes") == 0, "offline_nodes=%s" % ta.get("offline_nodes"))
        # 明细求和 = 汇总：这是"账对得上"的核心断言，早前两处采集口径不一的病根就在这
        sum_agents = sum(n.get("agents_total", 0) for n in na)
        rec("C2 totals.agents_total == 各节点求和", ta.get("agents_total") == sum_agents,
            "汇总=%s 明细和=%s" % (ta.get("agents_total"), sum_agents))
        sum_cap = sum(n.get("async_capacity", 0) for n in na)
        rec("C2 totals.async_capacity == 各节点求和", ta.get("async_capacity") == sum_cap,
            "汇总=%s 明细和=%s" % (ta.get("async_capacity"), sum_cap))
        sum_tasks = sum((n.get("tasks") or {}).get("total", 0) for n in na)
        rec("C2 totals.tasks.total == 各节点求和", (ta.get("tasks") or {}).get("total") == sum_tasks,
            "汇总=%s 明细和=%s" % ((ta.get("tasks") or {}).get("total"), sum_tasks))

        # ---------------- C3 跨节点负载可见
        print("\n--- C3 跨节点负载在 A 侧可见 ---")
        before_a_tasks = ((va or {}).get("totals", {}).get("tasks") or {}).get("total", 0)
        expect("C3 提交前 A 侧集群任务总数=0（干净基线）", before_a_tasks, [0])

        submitted = 3
        for i in range(submitted):
            code, body = b.submit("集群演练提交任务 %d：把这一步写清楚" % (i + 1))
            if code != 201:
                rec("C3 B 上提交任务 %d 失败" % (i + 1), False, "rc=%s body=%s" % (code, str(body)[:160]))
                break
        else:
            rec("C3 B 上 3 个任务全部 201", True)

        time.sleep(HEARTBEAT_SECS + 1.3)
        code, va2 = a.cluster()
        t2 = (va2 or {}).get("totals") or {}
        bnode = find_node(va2, "node-b")
        rec("C3 A 侧看到 B 的 tasks.total 反映了 3 个任务",
            bnode is not None and (bnode.get("tasks") or {}).get("total") == submitted,
            "B.tasks.total=%s" % ((bnode.get("tasks") or {}).get("total") if bnode else None))
        rec("C3 A 侧集群合计 == B 实际提交数（跨节点汇总不是拍脑袋）",
            (t2.get("tasks") or {}).get("total") == submitted,
            "totals.tasks.total=%s 期望=%s" % ((t2.get("tasks") or {}).get("total"), submitted))
        an = find_node(va2, "node-a")
        rec("C3 A 自己的 tasks.total 仍是 0（别人的活不该记在自己头上）",
            an is not None and (an.get("tasks") or {}).get("total") == 0,
            "A.tasks.total=%s" % ((an.get("tasks") or {}).get("total") if an else None))

        # ---------------- C4 心跳实时性
        print("\n--- C4 心跳实时性：一个周期内数字跟着涨 ---")
        prev_updated = parse_utc(bnode.get("updated_at")) if bnode and bnode.get("updated_at") else None
        for i in range(2):
            b.submit("集群演练增量任务 %d" % (i + 1))
        time.sleep(HEARTBEAT_SECS + 1.3)
        code, va3 = a.cluster()
        t3 = (va3 or {}).get("totals") or {}
        bnode3 = find_node(va3, "node-b")
        rec("C4 总数从 3 涨到 5（心跳把新负载带过来了）",
            (t3.get("tasks") or {}).get("total") == submitted + 2,
            "totals.tasks.total=%s 期望=%s" % ((t3.get("tasks") or {}).get("total"), submitted + 2))
        now_updated = parse_utc(bnode3.get("updated_at")) if bnode3 and bnode3.get("updated_at") else None
        rec("C4 B 的 updated_at 被推进（不是一份永远不变的旧快照）",
            bool(prev_updated and now_updated and now_updated > prev_updated),
            "旧=%s 新=%s" % (prev_updated, now_updated))

        # ---------------- C5 崩溃掉线
        print("\n--- C5 崩溃掉线：标 stale，且不污染汇总 ---")
        b.kill_ungraceful()
        last_beat = parse_utc(bnode3.get("updated_at"))
        sleep_until(last_beat, extra=TTL_SECS + 2.5)
        rec("C5 强杀后 B 的心跳文件还在（没人来得及删，正是要验的情形）",
            heartbeat_file("node-b").exists(), heartbeat_file("node-b").name)

        code, va4 = a.cluster()
        t4 = (va4 or {}).get("totals") or {}
        bnode4 = find_node(va4, "node-b")
        rec("C5 B 仍然可见但已标 stale（'某某节点 8 秒前失联'比凭空消失有用）",
            bnode4 is not None and bnode4.get("stale") is True,
            "stale=%s" % (bnode4.get("stale") if bnode4 else None))
        rec("C5 totals.offline_nodes=1（集群缩水一眼可见）",
            t4.get("offline_nodes") == 1, "offline_nodes=%s" % t4.get("offline_nodes"))
        expect("C5 totals.nodes 仍为 2（含离线）", t4.get("nodes"), [2])
        rec("C5 离线的 B 不计入 totals.tasks.total（陈旧值不该让'在跑几个'永远不下降）",
            (t4.get("tasks") or {}).get("total") == 0,
            "totals.tasks.total=%s 期望=0（B 离线后它的 5 个任务不再计入）" % (t4.get("tasks") or {}).get("total"))
        rec("C5 离线的 B 不计入 totals.agents_total",
            t4.get("agents_total") == (an or {}).get("agents_total"),
            "totals.agents_total=%s 在线节点和=%s" % (t4.get("agents_total"), (an or {}).get("agents_total")))

        # ---------------- C6 遗物清理
        print("\n--- C6 遗物清理 ---")
        if skip_prune:
            print("[SKIP] 按要求跳过 6×TTL 清理等待")
        else:
            sleep_until(last_beat, extra=PRUNE_WAIT_SECS + 3.0, cap=PRUNE_WAIT_SECS + 10)
            code, va5 = a.cluster()
            nodes5 = (va5 or {}).get("nodes") or []
            rec("C6 超过 6×TTL 的孤儿心跳文件被物理删除",
                not heartbeat_file("node-b").exists(), heartbeat_file("node-b").name)
            rec("C6 节点从列表里消失，只剩在线的那台",
                [n.get("node_id") for n in nodes5] == ["node-a"],
                "nodes=%s" % [n.get("node_id") for n in nodes5])
            expect("C6 清理后汇总节点数=1", ((va5 or {}).get("totals") or {}).get("nodes"), [1])

        # ---------------- C7 优雅退出
        print("\n--- C7 优雅退出：Ctrl+Break 后心跳文件立刻消失 ---")
        rc = a.stop_graceful()
        rec("C7 进程收到 Ctrl+Break 后正常退出（rc=0，不是被强杀）",
            rc == 0, "rc=%s" % rc)
        rec("C7 退出时删掉了自己的心跳文件（不必让同伴等 TTL）",
            not heartbeat_file("node-a").exists(), heartbeat_file("node-a").name)

        # ---------------- C8 单机开关
        print("\n--- C8 SA_CLUSTER=off 的进程 ---")
        off.start()
        off.wait_ready()
        off.login()
        time.sleep(HEARTBEAT_SECS + 1.2)
        code, vo = off.cluster()
        expect("C8 关闭心跳时 /cluster 仍返回 200（接口不因开关消失）", code, [200])
        nodeso = (vo or {}).get("nodes") or []
        rec("C8 只报自己一个节点（不读别人的心跳）",
            [n.get("node_id") for n in nodeso] == ["node-off"],
            "nodes=%s" % [n.get("node_id") for n in nodeso])
        rec("C8 关闭后不在共享目录留下任何文件",
            not heartbeat_file("node-off").exists(), heartbeat_file("node-off").name)
        rec("C8 也看不见共享目录里已有的心跳（on/off 是彻底的两条路）",
            ((vo or {}).get("totals") or {}).get("nodes") == 1,
            "totals.nodes=%s" % ((vo or {}).get("totals") or {}).get("nodes"))

        off.stop_graceful()

    finally:
        for n in (a, b, off):
            try:
                if n.proc and n.proc.poll() is None:
                    n.proc.kill()
            except Exception:
                pass

    return root


def main():
    ap = argparse.ArgumentParser(description="多节点集群双进程演练")
    ap.add_argument("--exe", default=str(DEFAULT_EXE), help="被测 super-agents.exe 绝对路径")
    ap.add_argument("--skip-prune", action="store_true", help="跳过 6×TTL 清理等待（约 30s）")
    ap.add_argument("--keep", action="store_true", help="保留临时数据目录")
    args = ap.parse_args()

    exe = pathlib.Path(args.exe)
    if not exe.exists():
        print("找不到被测二进制: %s" % exe)
        print("先跑 python scripts\\sync_build.py build")
        return 2

    root = run(exe, args.skip_prune)

    ok = sum(1 for _, p, _ in RESULTS if p)
    bad = [(n, d) for n, p, d in RESULTS if not p]
    print("\n" + "=" * 68)
    print("合计 %d 项 | 通过 %d | 失败 %d" % (len(RESULTS), ok, len(bad)))
    if bad:
        print("\n失败明细：")
        for n, d in bad:
            print("  - %s%s" % (n, ("  -> " + str(d)) if d else ""))
    print("演练目录: %s%s" % (root, "" if args.keep else "（可用 --keep 保留以便复查）"))

    if not args.keep:
        shutil.rmtree(root, ignore_errors=True)
    return 0 if not bad else 1


SHARED_DIR = pathlib.Path(tempfile.gettempdir()) / "sa-cluster-drill-unused"

if __name__ == "__main__":
    sys.exit(main())
