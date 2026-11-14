#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
regress_all.py —— 一条命令跑完全部验收门禁，输出一张结论表。

它不新增任何业务逻辑，只做顺序编排 + 结果收口，防止"某个脚本忘了跑"。

门禁共 24 道（顺序有讲究：先证"声明与源码一致"，再证"代码能跑"，最后证"端到端真能用"）：
  0  编译与新鲜度      build-exe.py    cargo build + 自证 exe 比源码新（防"测旧二进制"假绿）
  1  单元测试          cargo test（治理 / 存储 / API 层）
  2  冒烟（json 后端）  smoke.py        端到端业务正确性
  3  冒烟（memory 后端）smoke.py + SA_STORAGE=memory  存储可替换且行为等价
  4  OpenAPI 语义一致   check-openapi.py  字段集合 / 必填性 / 枚举取值逐个对齐
  5  OpenAPI ⇄ TS 同步  gen-ts-types.py --check  （TS 类型 === 契约）
  6  OpenAPI 未漂移     gen-openapi.py --check  docs/openapi.json == 源码现算结果
  7  TS 契约文档一致    check-contract.py   docs/40-api-contract-ts.md 的 interface 与源码一致
  8  执行端口实测      verify_runner.py  真跑外部进程
  9  可插拔部件矩阵    matrix.py  逐案换件（存储/审计/限流/指标/执行）+ 写穿硬杀 + 配额可配
 10  换件演练 + 静态守护 swap_drill.py   （内含 check-modularity.py）
 11  审计启动自检      audit-probe-test.py  写得出/查得到/拒启/不假装落盘
 12  需求覆盖对齐      check-prompt-coverage.py
 13  防幻觉打点实测    probe_hallucination.py
 14  真实响应形状      check-response-shape.py  真起进程，拿真实 JSON 打 openapi schema
 15  多节点集群双进程  verify_cluster.py  真起两台进程互发心跳：互通 / 跨节点可见 / 掉线 / 清理 / 优雅退出
 16  worker 分词质量    probe_worker_text.py  25 条真实业务目标：脏词 / 丢词 / 排序退化即红
 17  异步受理与缺省    probe_async_task.py  缺省必须 202+queued；分支头 / 事件流 / 并发上限 / 取消全链
18  模型路径真采用    probe_llm_soak.py  真模型跑 3 轮：判"产出被采用"而非"接口 200"；缺模型记 SKIP 不记 PASS
  19  可观测告警行为    probe_alerting.py  压阈值必然命中：命中→去重抑制→过窗口复发→风暴只留严重→指标守恒
  20  主题取词质量      probe_topic_guard.py  垂直 agent 的 topic 必须是人能用的词，不是半句/碎片/维度词
  21  垂直域判据        probe_vertical_verdict.py  三域各自的判据表在不在、用没用上、会不会乱答别域
 22  CLI 外壳行为      probe_cli.py  无参必须仍是 serve（21 道的隐含前提）/ 拼错子命令不被当 serve /
                                            doctor 的 JSON 与文本逐项一致、退出码 0-1-2 真能分开
 23  CLI 客户端行为    probe_cli_http.py  登录落盘 / 401 自动刷新并回写 / 并发刷新自愈 /
                                            表格按显示宽度对齐且 UUID 完整 / 时间为 UTC / --json 纯净

第 15 道必须真起两个进程：单测只证明 `peers()` 认得自己、TTL 判得准，
证明不了"两个独立进程通过共享目录互相看见"——那取决于环境变量解析、
心跳循环是否真在跑、装配顺序对不对，这一层只有真进程能验。

第 11 道的位置也不是凑数：它验证的是一条**安全属性**——"看起来有审计"和"真有审计"
是两件事。前 10 道全绿也可能带着一个写不进磁盘的审计跑一整周，只有真起进程写一条
boot 再读回来才能证伪。

分层（--tier）：门禁"多"本身不是慢的原因，**少数几道重门**才是
  full  24 道 ≈ 400s   交付口径只认这一档
  api   18 道 ≈  80s   动了接口 / 存储 / 异步 / 指标层
  quick 11 道 ≈  20s   改一处纯逻辑、只想确认没捅娄子
时间账（2026-11-14 全量 24 道实测 397.3s）：门禁 18 模型 176.1s + 15 集群 48.6s +
10 换件 35.1s + 17 异步 24.6s + 9 矩阵 24.2s + 23 CLI 客户端 17.2s = 325.8s，占 82%；
其余 18 道合计 71.5s。
也就是说：**加门禁几乎不花时间，跑真模型和起多进程才花时间。**
所以正确做法是**分层跑**，不是砍门禁——砍掉的恰好是最能证伪的那几道。
--tier 非 full 的结论一律不是"全绿"：报告首行会写明"本次跑 X/24"，证据落到
evidence/<tier>/，不覆盖正式证据。

第 20 道的由来：门禁 16 守的是 `_common.py` 的**公共分词**，它刻意不判 topic（topic 是原文
切片，判脏会造假阳性）。但 narrative / market / qa 三个垂直 worker 的产出质量全压在 topic
上——"在聊什么"错了，后面每一项检查都对着错的对象在问。更要紧的一课：这道门禁的第一版
自己手写域词表喂给取词函数，**全绿，而真实 worker 产出是脏的**，因为 worker 传的是分词
结果，跟手写词表不是一回事。现在关键词与域词表都与线上同源（`_common.keywords` +
`_topic_guard.DOMAINS`），并另带 `--drill-no-guard` 负向演练自证会变红。

用法：
  python scripts/regress_all.py [--exe <path>] [--skip-cargo]
  python scripts/regress_all.py --fast          # 跳过等时钟的断言（省约 30s），证据写 evidence/fast/
  python scripts/regress_all.py --tier quick    # 只跑 11 道秒级门禁（≈20s），证据写 evidence/quick/
  python scripts/regress_all.py --tier api      # 18 道（≈80s），证据写 evidence/api/
退出码：0 = 全绿；非 0 = 有门禁失败（逐条列出）
"""

import argparse
import os
import pathlib
import re
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
EVID = ROOT / "docs" / "evidence"
sys.path.insert(0, str(ROOT / "scripts"))
try:  # 与 sync_build 共用同一套镜像目录解析：两套镜像会让"测的"和"跑的"不是同一份二进制
    from sync_build import resolve_build as _resolve_build
    DEFAULT_BUILD = _resolve_build()
except Exception:  # noqa: BLE001 - 脚本被单独拷走时仍要能跑
    DEFAULT_BUILD = pathlib.Path(os.environ.get("LOCALAPPDATA", ".")) / "sa-build"


def tail_lines(text, keep=3, keys=("test result", "通过", "合计", "结论", "换件演练", "冒烟", "不一致", "差异", "rc=")):
    picked = [l.strip() for l in text.splitlines() if any(k in l for k in keys)]
    return picked[-keep:] if picked else [l.strip() for l in text.splitlines() if l.strip()][-keep:]


# 演练摘要专用关键词：只取自证结论，不取 [FAIL]/[PASS] 断言行。
# 为什么必须分开抽：演练本来就是要让断言**成片变红**，rc=0 才叫按预期。
# 用普通 tail_lines（关键词含"通过"）去抽，抽到的全是 `2/12通过` 这种明细，
# 而真正的结论行 `[DRILL-OK] … 说明判据有判定力` 一个关键词都不沾，于是整条演练
# 在报告里只剩 FAIL 字样 —— 实测真踩过：rc=0、演练 5/5 按预期，报告却整行写着 FAIL，
# 差点去查一个不存在的缺陷。证据里"结论"被明细淹没，比没有结论更坏。
DRILL_KEYS = ("DRILL-OK", "DRILL-FAIL", "演练按预期", "按预期变红", "具备变红能力",
              "未按预期", "未达预期")


def drill_tail(text, keep=2):
    picked = [l.strip() for l in text.splitlines() if any(k in l for k in DRILL_KEYS)]
    if not picked:  # 纯 rc 型演练（如 drill-openapi6.py）没有自证行，退回普通摘要
        return tail_lines(text)
    return picked[-keep:]


def gate_slug(name, cmd):
    """把一道门禁映射成稳定、可预测的证据文件名，供文档直接引用。

    取名规则：序号 + 被测脚本名（cargo test 特殊记为 cargo-test）。
    不用中文门禁名做文件名，是为了避免编码差异导致"文档引用得到、文件却不在"。
    """
    order = str(name).split(" ", 1)[0]
    parts = [str(c) for c in cmd]
    stem = "cargo-test" if parts and parts[0] == "cargo" else ""
    if not stem:
        for c in parts:
            base = os.path.basename(c)
            if base.endswith(".py"):
                stem = base[:-3]
    return "%02d-%s-latest.txt" % (int(order) if order.isdigit() else 0, stem or "gate")


def run(name, cmd, cwd=ROOT, env_extra=None, timeout=1800, drill=False):
    # 分层过滤：被 --tier 排除的道**根本不启动**，而不是启动了假装没跑。
    # 返回一个带 tier_skipped 的占位结果，_write_reports 会把它从"合计 N 道"里剔掉——
    # 否则 --tier quick 会打出"合计 N 道 | 通过 N"这种最坏的假绿：
    # 只看末行的人会以为全量跑完了，而实际上大部分门禁根本没执行。
    if _tier_skip(name):
        print("[TIER] %-26s 本次未执行（不在 --tier %s 内）" % (name, _ACTIVE_TIER))
        return {"name": name, "rc": 0, "dur": 0.0, "tail": ["未执行（--tier %s 过滤）" % _ACTIVE_TIER],
                "raw": "本次按 --tier %s 跳过，未运行。" % _ACTIVE_TIER,
                "slug": gate_slug(name, cmd), "tier_skipped": True}

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if env_extra:
        env.update(env_extra)
    t0 = time.time()
    try:
        r = subprocess.run([str(c) for c in cmd], cwd=str(cwd), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, env=env)
        out = (r.stdout or "") + "\n" + (r.stderr or "")
        rc = r.returncode
    except subprocess.TimeoutExpired:
        out, rc = "超时", 124
    dt = time.time() - t0
    # rc=3 是本仓库约定的 SKIP 语义（当前只有门禁 18 用）：环境不具备 ≠ 功能退化。
    # 但 SKIP 绝不能显示成 PASS —— 那只是换了个名字的假绿。
    # 演练行单独用 DRILL-OK / DRILL-FAIL：它的"成功"含义与门禁相反——
    # 断言**成片变红**才叫按预期，写 PASS 会让人以为断言没红过。
    if drill:
        status = "DRILL-OK" if rc == 0 else "DRILL-FAIL"
        tail = drill_tail(out)
    else:
        status = "PASS" if rc == 0 else ("SKIP" if rc == 3 else "FAIL")
        tail = tail_lines(out)
    print("[%s] %-26s %6.1fs  %s" % (status, name, dt,
                                     " | ".join(tail)[:160]))
    return {"name": name, "rc": rc, "dur": dt, "tail": tail, "raw": out,
            "slug": gate_slug(name, cmd)}


# ─────────────────────────────────────────────────────────────────────────────
# 分层：按"这次改动的性质"选一组门禁跑，而不是每次都等 5 分钟。
# 划分依据是各道门禁**能证伪哪一类改动**，不是按耗时排队：
#   quick —— 改一处纯逻辑：声明与源码是否对齐（4/5/6/7/12）+ 纯 Python 单测（16/20）
#            + 编译新鲜度（0）+ Rust 单测（1）+ 真实响应形状（14，真进程但只要 4s）
#   api   —— 动了路由 / 存储 / 异步 / 指标 / 告警：再加冒烟×2、执行端口、审计、防幻觉、
#            异步全链、告警行为
#   full  —— 全量。**只有 full 的结论能叫"全绿"**
# 注意：门禁 9/10/15/18 刻意只留在 full —— 换件矩阵、换件演练、多进程集群、真模型，
# 这四道正是历史上抓出假绿的那几道，任何"顺手跳过"都会把假绿放回来。
# 门禁总道数。**只在这里写一次**：三处文案都从这里取，
# 且 _write_reports 会拿它跟实际执行数对账——加了门禁忘改这里，full 跑会直接 rc=2。
TOTAL_GATES = 24

TIERS = {
    "quick": ["0", "1", "4", "5", "6", "7", "12", "14", "16", "20", "22"],
    "api": ["0", "1", "2", "3", "4", "5", "6", "7", "8", "11", "12", "13",
            "14", "16", "17", "19", "20", "22"],
    "full": None,
}
TIER_HINT = {"quick": "改一处纯逻辑后自查（≈20s）",
             "api": "动了接口 / 存储 / 异步 / 指标层（≈80s）",
             "full": "全量，交付口径（≈400s）"}
_ACTIVE_TIER = "full"   # 由 --tier 覆盖


def _tier_skip(name):
    """该道是否被 --tier 过滤掉。name 以序号开头，据此归属。"""
    allowed = TIERS.get(_ACTIVE_TIER)
    if allowed is None:
        return False
    m = re.match(r"(\d+)", name)
    return bool(m) and m.group(1) not in allowed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", choices=["quick", "api", "full"], default="full",
                    help="分层跑：quick≈20s / api≈80s / full≈400s（只有 full 的结论能叫全绿）")
    ap.add_argument("--exe", default=str(DEFAULT_BUILD / "target-gnu" / "debug" / "super-agents.exe"))
    ap.add_argument("--build-dir", default=str(DEFAULT_BUILD))
    ap.add_argument("--skip-cargo", action="store_true")
    ap.add_argument("--skip-sync", action="store_true", help="不把工作区源码同步进镜像（慎用：会测到旧代码）")
    # --fast 只放过"等时钟"的那一类断言（集群门禁里 6×TTL 的清理等待占 30s），
    # 逻辑断言一条不少。用来在改一处代码后快速自查；交付前仍要跑全量。
    ap.add_argument("--fast", action="store_true", help="跳过需要等时钟的断言（集群 6×TTL 清理），逻辑断言不减")
    ap.add_argument("--with-drills", action="store_true",
                    help="额外跑负向演练（验证门禁本身会不会红），不计入门禁数，失败照样让退出码变红")
    ap.add_argument("--out", default=str(EVID / "regress-all.txt"))
    args = ap.parse_args()

    global _ACTIVE_TIER
    _ACTIVE_TIER = args.tier
    print("[TIER] %s —— %s（本次最多跑 %s 道）"
          % (args.tier, TIER_HINT[args.tier],
             len(TIERS[args.tier]) if TIERS[args.tier] else TOTAL_GATES))
    if args.tier != "full":
        print("[TIER] 非 full 结论**不是全绿**：只代表这几道里没有红，其余门禁本次未执行。")

    # 先同步：保证测的是当前工作区代码，而不是镜像里的上一版
    if not args.skip_sync:
        try:
            from sync_build import sync as _sync
            _sync()
            print("[SYNC] 工作区 → 镜像完成：%s" % args.build_dir)
        except Exception as exc:  # noqa: BLE001
            print("[WARN] 同步失败（继续用镜像现有内容）：%s" % exc)

    exe = pathlib.Path(args.exe)
    if not exe.exists():
        print("找不到可执行文件：%s" % exe)
        return 2

    # 把"到底在测谁"打在开头。历史上并存过两套镜像（桌面 build_sa1 与 %LOCALAPPDATA%\sa-build），
    # 看交接文档照抄旧路径的人会测到一个停在几个月前的 exe —— 这是"假绿"的另一个入口。
    print("[TARGET] 源码  : %s" % ROOT)
    print("[TARGET] 镜像  : %s" % args.build_dir)
    print("[TARGET] 被测exe: %s" % exe)
    _other = [q for q in (ROOT.parent / "build_sa1",
                          pathlib.Path(os.environ.get("LOCALAPPDATA", ".")) / "sa-build")
              if q.resolve() != pathlib.Path(args.build_dir).resolve() and q.exists()]
    for q in _other:
        print("[TARGET] 另有镜像 %s —— 本次**不用它**，别照旧路径测出假绿" % q)
    py = sys.executable
    results = []

    # 第 0 道：先保证"被测的就是刚写的"。cargo test 不产出 super-agents.exe，
    # 少了这一步，后面九道门禁可能全在测上一版程序（真踩过：exe 18:48、源码 20:33）。
    build_gate = run("0 编译与二进制新鲜度",
                     [py, "scripts/build-exe.py", "--build-dir", args.build_dir, "--exe", exe],
                     timeout=1800)
    results.append(build_gate)
    if build_gate["rc"] != 0:
        print("[STOP] 二进制未就绪，后续门禁不再执行（否则结果是假绿）")
        _write_reports(results, args, aborted=True)
        return 1

    if not args.skip_cargo:
        results.append(run("1 单元测试 cargo test",
                           ["cargo", "+stable-x86_64-pc-windows-gnu", "test", "--quiet"],
                           cwd=args.build_dir,
                           env_extra={"CARGO_TARGET_DIR": str(pathlib.Path(args.build_dir) / "target-gnu")}))
    results.append(run("2 冒烟 json 后端", [py, "scripts/smoke.py", "--exe", exe]))
    results.append(run("3 冒烟 memory 后端", [py, "scripts/smoke.py", "--exe", exe, "--port", "18097"],
                       env_extra={"SA_STORAGE": "memory"}))
    results.append(run("4 契约一致性", [py, "scripts/check-openapi.py"]))
    results.append(run("5 OpenAPI 与 TS 同步", [py, "scripts/gen-ts-types.py", "--check"]))
    results.append(run("6 OpenAPI 未漂移", [py, "scripts/gen-openapi.py", "--check"], cwd=ROOT))
    results.append(run("7 TS 契约文档一致", [py, "scripts/check-contract.py"]))
    results.append(run("8 执行端口实测", [py, "scripts/verify_runner.py", "--exe", exe]))
    results.append(run("9 可插拔部件矩阵", [py, "scripts/matrix.py", "--exe", exe]))
    results.append(run("10 换件演练与静态守护", [py, "scripts/swap_drill.py", "--exe", exe]))
    results.append(run("11 审计启动自检", [py, "scripts/audit-probe-test.py", "--exe", exe]))
    # 第 12 道不碰进程、只比文档：它守的是"最初要的东西有没有整段漏掉"，
    # 前 11 道全绿也证不了这件事（跑得快和做得全不是一回事）。
    results.append(run("12 需求覆盖对齐", [py, "scripts/check-prompt-coverage.py"]))
    # 第 13 道：C25 的指标曾是"规格写了、代码没打点"，光看代码看不出来，
    # 只能真起进程造一次上下文裁剪，再去 /metrics 里找那一行。
    results.append(run("13 防幻觉打点实测", [py, "scripts/probe_hallucination.py", "--exe", exe]))
    # 第 14 道：前 13 道都只证明"源码里怎么写"，证不了"发出去的 JSON 到底长什么样"。
    # 历史上 GET /tasks/{id} 实际是平铺的，而 openapi 声明成嵌套 {task,trace,memory_refs}，
    # 14 道门禁一道都没红——因为校验器读的是源码 struct 字面，与生成器互相背书。
    # 这一道改成拿真实响应去打 schema：形状对不上就红，前端从此不会再拿到 undefined。
    results.append(run("14 真实响应形状 vs schema", [py, "scripts/check-response-shape.py", "--exe", exe]))
    # 第 15 道：前 14 道验的都是"单个进程自己的行为"。多节点集群是**进程之间**的事，
    # 单进程怎么测都测不到——互通、跨节点可见、掉线不污染汇总、遗物清理、优雅退出，
    # 全得真起两台。这一道约 50s（要等 6×TTL 的清理线），但它是集群功能唯一的硬证据。
    cluster_cmd = [py, "scripts/verify_cluster.py", "--exe", exe]
    if args.fast:
        cluster_cmd.append("--skip-prune")
    results.append(run("15 多节点集群双进程" + ("(fast)" if args.fast else ""), cluster_cmd))
    # 第 16 道：前 15 道没有一道在守「文字处理」。分词 / 关键词 / 主题提取是 9 个 worker
    # 共用的地基（workers/_common.py），它退化**不会**让任何一道门禁变红——只会让
    # doc-agent 的要点标题冒出「订单表加」「排查高」「在本机的」这类碎片：人眼看得出，
    # 机器看不出来。历史上它就是靠人肉读文档才发现的（9 类切词缺陷潜伏到 2026-11）。
    # 这一道把 25 条真实业务目标固化成用例，脏词 / 丢词 / 排序任一退化即红。
    results.append(run("16 worker 分词质量", [py, "scripts/probe_worker_text.py"]))
    # 第 17 道：前 16 道里没有任何一道在守"缺省值"。POST /tasks 的缺省曾是 sync，
    # 而 openapi / TS 类型 / 文档全都写着 default: async——契约与实现各说各话，
    # 所有门禁依然全绿，因为脚本自己也是显式或吃缺省、证不了这件事。
    # 代价出现在真模型上：同步提交实测 129.9s，客户端默认 30s 超时 → 任务明明成功却被判失败。
    # 这一道把"缺省就是 async"钉死，并且顺带守 202 的语义完整性（分支头 / Location /
    # running 中间态 / 并发上限 429 / 取消收敛），这些没有一条能靠单测覆盖。
    results.append(run("17 异步受理与缺省", [py, "scripts/probe_async_task.py", "--exe", exe]))
    # 第 18 道：前 17 道里**没有一道**碰过真模型。doc-agent 走的模型路径曾经是这样一种状态：
    # 2B 小模型 12 轮全部产出不合法 JSON → 静默回退规则骨架 → 任务照样 succeeded、
    # tokens 照样 > 0、所有门禁照样全绿，而"模型"这一层等于从未生效。
    # 这是本仓库出现过的最难发现的一类假绿：**每一次调用都成功，但没有一次采用**。
    # 这一道用真模型跑几轮（默认 3 模型 + 2 快 + 2 坏输入，约 100s），
    # 判据是"模型产出被采用"而不是"接口 200"；缺模型时记 SKIP 并写清原因，绝不记 PASS。
    results.append(run("18 模型路径真采用", [py, "scripts/probe_llm_soak.py", "--exe", exe,
                                          "--python", py, "--model-rounds", "3",
                                          "--fast-rounds", "2", "--guard-rounds", "2",
                                          "--allow-skip", "--tag", "soak-gate18"],
                       timeout=900))
    # 第 19 道：前 18 道里**没有一道**碰过「告警」。告警是最容易"做得像"的功能：
    # 接口能返回、字段齐全、文档写着"5 分钟去重"，但阈值算错一位、去重窗口根本没生效、
    # 风暴降级时把严重告警一起折掉——这三件事肉眼全看不出来，只会在真出事那天暴露。
    # 这一道把阈值压到必然命中，硬断言 36 个数字（命中→窗口内抑制→过窗口复发→
    # 风暴只留严重级别→指标命中=发出+压制 守恒），并分三个独立进程验三套策略：
    # 策略只能由环境变量在进程启动时给定，硬凑在一个进程里就是"靠顺序祈祷"的假门禁。
    # 负向演练：scripts/probe_alerting.py --drill-no-dedup（关掉去重，A8 判据必须转红）。
    results.append(run("19 可观测告警行为", [py, "scripts/probe_alerting.py", "--exe", exe], timeout=600))

    # 第 20 道：垂直 agent 的"在聊什么"。第 16 道管公共分词，但 topic 在那边是免检的
    # （原文切片，判脏即假阳性），而三个垂直 worker 的全部检查项都以 topic 为主语——
    # 取成「这段剧情的主线推进太慢」这种半句，后面每一项都在对着半句话做检查。
    # 本道吃 12 条真实任务原文，判两级（对象词命中 + 句中/碎片/维度词排查），
    # 关键词与域词表均与线上同源（手写词表曾让这道门禁假绿一轮）。
    # 负向演练：scripts/probe_topic_guard.py --drill-no-guard（拿掉守卫必须变红）。
    results.append(run("20 主题取词质量", [py, "scripts/probe_topic_guard.py"], timeout=300))
    # 21 号不进 quick / api：它要起 10 次 worker 子进程（每个 ~0.3s 启动，合计约 4s），
    # 但更关键的是它的断言面是"三个域的行为"，改动落在纯 Rust 层时它不可能红，
    # 放 quick 里只会拖长自查循环。改 workers/ 下任何文件才需要它。
    results.append(run("21 垂直域判据", [py, "scripts/probe_vertical_verdict.py"], timeout=600))

    # 22 号守的是一条**从没被写下来过的约定**：前面 21 道全部以「[exe] 无参」启动，
    # 也就是说"无子命令 = serve"是整个门禁体系的隐含前提。加了 CLI 之后，
    # 这个前提一旦被改坏，21 道会集体变红，但每道的报告只会说"服务起不来"——
    # 从"到底是服务坏了，还是分派坏了"这个岔路口重新排查一遍 main.rs。
    # 所以这里不测新功能有多好，只测两件事：默认语义没变、doctor 的退出码能当判据用。
    # 它也进 quick：A/C 组不开服务进程（约 3s），改了 main.rs / cli/ 就能立刻知道。
    results.append(run("22 CLI 外壳行为", [py, "scripts/probe_cli.py", "--exe", exe], timeout=600))

    # 23 号补的是 22 号管不到的那一半：22 号只测"外壳"（分派是否还在、doctor 的退出码），
    # 一次都不登录、不发一个业务请求。可 CLI 真正的坑全在客户端这一层，而且全都是
    # "看起来正常"的坑：401 后不刷新就整条命令失败、刷新回写时漏了 tokens 嵌套层
    # 导致下一条命令必 401、多进程同时刷新互相覆盖、表格按字符数而非显示宽度补空格
    # 把 UUID 截成 204750aa-…?、时间写 UTC 却只标"创建时间"。
    # 这一道真起服务、真登录、真提交，51~53 项断言；它**只进 full**——
    # 要跑两次同步任务与 8 进程 × 3 轮并发刷新，塞进 quick 只会拖慢自查循环。
    # 负向演练：scripts/probe_cli_http.py --drill（换 where.exe 当被测物，必须成片变红）。
    results.append(run("23 CLI 客户端行为", [py, "scripts/probe_cli_http.py", "--exe", exe], timeout=900))

    # ── 负向演练（可选）：问的是另一个问题——"门禁会不会红"，不是"功能对不对" ──
    # 刻意不塞进 results：门禁数量是给文档和交付承诺用的固定口径，
    # 演练是元层证据，混在一起会让"门禁共 N 道"这个数字随开关漂移。
    drills = []
    if getattr(args, "with_drills", False):
        print("\n--- 负向演练（验证门禁有判定力，不计入门禁数）---")
        drills.append(run("演练 · 门禁6 会不会红", [py, "scripts/drill-openapi6.py"], timeout=600, drill=True))
        drills.append(run("演练 · 门禁20 会不会红",
                          [py, "scripts/probe_topic_guard.py", "--drill-no-guard"], timeout=300, drill=True))
        # 清空 narrative 的抱怨词表 B 组必须变红——证明 21 号不是恒绿。
        drills.append(run("演练 · 门禁21 会不会红",
                          [py, "scripts/probe_vertical_verdict.py", "--drill"], timeout=600, drill=True))
        # 换一个"是程序但不是我们的 CLI"的东西当被测物（where.exe）：它的 --help 不认识
        # serve，也不接受 doctor。若此时 22 号仍全绿，说明它压根没在测东西。
        # 这一条比前三条更要紧：22 号的 A/C 组大半是"输出里有没有某个词"，
        # 正是最容易写成恒绿的一类断言。
        drills.append(run("演练 · 门禁22 会不会红",
                          [py, "scripts/probe_cli.py", "--drill"], timeout=600, drill=True))
        # 23 号的断言大半是"输出里第几列写了什么"。这类按列位取值的断言最容易写成恒绿：
        # cell() 偏移算错一位时，取到的是"上一列尾巴+下一列开头"，照样能凑出非空字符串。
        # 这一条要求换掉被测程序后成片转红（≥8 项），验证它真的在读真程序的输出。
        drills.append(run("演练 · 门禁23 会不会红",
                          [py, "scripts/probe_cli_http.py", "--drill"], timeout=900, drill=True))

    return _write_reports(results, args, drills)


def _write_reports(results, args, drills=(), aborted=False):
    """落盘证据：总表 + 逐门禁明细。抽成函数是因为第 0 道失败时也要留证。

    --fast 跑的结论（门禁 15 少 3 条等待型断言）**不能覆盖正式证据**，否则文档里引用的
    latest 文件会与"全量回归"口径不符——那正是"正文说 156 项、文件写 154 项"的老毛病。
    所以 fast 单独落到 evidence/fast/。
    """
    fast = bool(getattr(args, "fast", False))
    tier = getattr(args, "tier", "full")
    # 证据分流规则：只有"全量 + 非 fast"才写正式证据目录。
    # 半套结论写进 latest，会让文档里引用的数字和实际跑过的门禁数量对不上，
    # 那正是"正文说 156 项、文件写 154 项"的老毛病。
    sub = "fast" if fast else (tier if tier != "full" else "")
    out_dir = (EVID / sub) if sub else EVID
    skipped_tier = [r for r in results if r.get("tier_skipped")]
    results = [r for r in results if not r.get("tier_skipped")]
    out_dir.mkdir(parents=True, exist_ok=True)
    failed = [r for r in results if r["rc"] not in (0, 3)]
    skipped = [r for r in results if r["rc"] == 3]
    lines = ["# 全量回归（regress_all.py）", "生成时间: " + time.strftime("%Y-%m-%d %H:%M:%S"), ""]
    for r in results:
        mark = "" if r["rc"] == 0 else ("  <== SKIP（环境不具备，不是通过）" if r["rc"] == 3 else "")
        lines.append("%-24s rc=%-3d %6.1fs  %s%s" % (r["name"], r["rc"], r["dur"],
                                                    " | ".join(r["tail"])[:200], mark))
    lines.append("")
    lines.append("合计 %d 道门禁 | 通过 %d | 失败 %d | 跳过 %d"
                 % (len(results), len(results) - len(failed) - len(skipped), len(failed), len(skipped)))
    if skipped_tier:
        lines.append("")
        lines.append("⚠ 本次为 --tier %s，另有 %d 道**未执行**（%s）——"
                     "这不是全绿结论，交付前须跑 --tier full。"
                     % (tier, len(skipped_tier),
                        "、".join(r["name"].split(" ")[0] for r in skipped_tier)))
    if skipped:
        lines.append("跳过说明：" + "；".join("%s（rc=3，需在环境就绪后补跑）" % r["name"] for r in skipped))
    if drills:
        bad_drills = [r for r in drills if r["rc"] != 0]
        lines.append("")
        lines.append("负向演练（%d 项 | 按预期 %d | 未按预期 %d）"
                     "—— 守的是「门禁本身会不会红」，不计入上面的门禁数："
                     % (len(drills), len(drills) - len(bad_drills), len(bad_drills)))
        for r in drills:
            # 每行都自带判定语："按预期变红 / 未按预期"。
            # 演练 rc=0 只表示"红得够多"，光看 rc 与明细里的 [FAIL] 分不清哪边是结论。
            verdict = "按预期变红" if r["rc"] == 0 else "未按预期（门禁失去判定力）"
            lines.append("%-24s rc=%-3d %6.1fs  %s  %s" % (r["name"], r["rc"], r["dur"], verdict,
                                                          " | ".join(r["tail"])[:200]))
        if bad_drills:
            lines.append("!! 演练未按预期：门禁可能已失去判定力，比功能报错更该优先处理。")
    text = "\n".join(lines)
    outp = pathlib.Path(args.out)
    if sub and outp.parent.resolve() == EVID.resolve():
        outp = out_dir / outp.name
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(text + "\n", encoding="utf-8")
    (out_dir / "regress-all.raw.txt").write_text("\n\n".join(
        "===== %s (rc=%d) =====\n%s" % (r["name"], r["rc"], r["raw"]) for r in results), encoding="utf-8")

    # 逐门禁也落一份，文件名固定为 <序号>-<脚本>-latest.txt。
    # 为什么值得多写这几个文件：文档里引用的证据若停在"上一次单独跑"的版本，
    # 就会出现"正文说 156 项、证据文件里写着 154 项"的错位——比没有证据更糟，
    # 因为看的人会以为数字是编的。让 latest 永远与最近一次全量回归同步。
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    for r in results:
        f = out_dir / r["slug"]
        f.write_text(
            "# %s\n生成时间: %s\n命令由 regress_all.py 编排执行，rc=%d，耗时 %.1fs\n"
            "（原始输出如下，未做删改）\n%s\n%s\n" % (r["name"], stamp, r["rc"], r["dur"], "=" * 70, r["raw"]),
            encoding="utf-8")

    # 自证：full 档跑完的道数必须等于 TOTAL_GATES。
    # 否则会出现"实际跑了 24 道、文案还写 23 道"这类漂移——门禁数字一旦不准，
    # 文档里的每一个引用都跟着不准。按退出码四分，这属于脚本自身配置错（2），
    # 不是断言失败（1），要让人一眼分清是"代码坏了"还是"编排数错了"。
    # 已知扣减项：--skip-cargo 少跑门禁 1（它压根没被 append），--tier 过滤掉的另计。
    # 第一版自证没算这两项，于是自己骗了自己一次（实际 21 道被报成"编排数错"）。
    opt_out = 1 if getattr(args, "skip_cargo", False) else 0
    expected = TOTAL_GATES - len(skipped_tier) - opt_out
    # 提前中止是**合法**路径（门禁 0 已判定二进制不可信，继续跑才是假绿），
    # 此时 results 只有 1 道，拿它去比 TOTAL_GATES 必然"不符"——
    # 那是自证在诬告自己。所以中止场景不做道数自证，改为显式声明中止原因。
    count_mismatch = (tier == "full" and len(results) != expected
                      and not fast and not aborted
                      and not getattr(args, "only", None))

    print("\n" + "=" * 78)
    print(text)
    if aborted:
        print("[ABORT] 已在门禁 0 中止：编排自证跳过（道数无从谈起），"
              "失败原因见上方门禁 0。")
    if count_mismatch:
        print("!! 编排自证失败：实际执行 %d 道，声明 %d 道（扣除 --skip-cargo %d 道 / "
              "--tier 未执行 %d 道后应为 %d 道）。加/删门禁时忘了改常量 —— "
              "请修 regress_all.py 顶部 TOTAL_GATES。"
              % (len(results), TOTAL_GATES, opt_out, len(skipped_tier), expected))
        return 2
    print("明细写入 %s" % (out_dir / "regress-all.raw.txt"))
    print("逐门禁证据：%s\\<序号>-<脚本>-latest.txt（共 %d 个，与本表同批生成）" % (out_dir, len(results)))
    if fast:
        print("[FAST] 本次跳过了等时钟的断言，正式证据未刷新；交付前请跑一次不带 --fast 的全量。")
    if skipped_tier:
        print("=" * 78)
        print("[TIER] 本次只跑了 %d/%d 道（--tier %s），另有 %d 道未执行：%s"
              % (len(results), TOTAL_GATES, tier, len(skipped_tier),
                 "、".join(r["name"].split(" ")[0] for r in skipped_tier)))
        print("[TIER] 未执行 ≠ 通过，本次结论**不是全绿**；交付前必须跑 --tier full。")
        print("=" * 78)
    if skipped:
        # 退出码是 0（环境不具备不该拦住日常开发），但绝不让人误读成"全绿"：
        print("=" * 78)
        print("[SKIP] 本次有 %d 道门禁被跳过：%s" % (len(skipped), "、".join(r["name"] for r in skipped)))
        print("[SKIP] 跳过 ≠ 通过，本次结论**不是全绿**；环境就绪后必须补跑这几道再交付。")
        print("=" * 78)
    # 演练未按预期同样算失败：门禁失去判定力时，"全绿"本身就是不可信结论，
    # 比某条功能断言报错更该优先处理。
    bad_drills = [r for r in drills if r["rc"] != 0]
    if bad_drills:
        print("=" * 78)
        print("[DRILL] %d 项负向演练未按预期：%s"
              % (len(bad_drills), "、".join(r["name"] for r in bad_drills)))
        print("[DRILL] 说明门禁可能已失去判定力——本次「全绿」不可信。")
        print("=" * 78)
    return 0 if (not failed and not bad_drills) else 1


if __name__ == "__main__":
    sys.exit(main())
