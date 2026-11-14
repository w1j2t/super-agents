# 84 · 功能缺口审计（声明 vs 实现逐项对账）

> 目的：回答"功能还有没有漏的"。
> 方法：三条互相独立的线索各自取证，任一线索报红即算缺口——不靠肉眼读文档。
> 结论口径：**能力清单（docs/02）是承诺；源码是事实**。冲突时以源码为准，文档订正或功能补齐，二者必居其一。

审计日期：2026-11-13

---

## 1. 审计方法（可复现）

| 线索 | 判据 | 覆盖的问题 |
| --- | --- | --- |
| A 契约完整性 | 路由注册表（`src/api/mod.rs` 括号配平解析）与 `docs/openapi.json` 的 path+method 集合必须相等 | 有没有"写了接口但前端拿不到契约" |
| B 指标落点 | `CapabilitySpec.observability_signals` 声明的每个 `sa_*` 名，必须在**除 `capability.rs` 以外**的源码里出现（声明文件本身不算落点） | 有没有"承诺了信号但没人打点" |
| C 能力落地 | 对每个能力，按 `trigger` / `permission_boundary` / `degradation_or_abort` 三段原文，在源码中找对应实现点 | 有没有"整项能力只是文字" |

---

## 2. 线索 A：契约完整性 —— 通过

- 代码侧：v1 路由 **29 条操作**，公共路由（healthz / readyz / metrics）**3 条**，合计 **32 条操作 / 28 个 path**。
- 契约侧：`docs/openapi.json` **28 个 path / 32 个操作**。
- 双向差集：**代码有契约无 = 0**，**契约有代码无 = 0**。

这条是干净的。说明之前担心的"生成器漏路由、门禁 6 仍绿"并不存在：`gen-openapi.py` 覆盖了全部注册路由。

> 顺带确认一个口径：门禁 4 记录的 `paths=28 operations=32` 与本次独立解析一致，两个数字不是抄的。

---

## 3. 线索 B：44 项声明指标，22 项在源码里零落点

### 3.1 总账

| 分类 | 数量 |
| --- | --- |
| 能力清单声明的 `sa_*` 指标 | 44 |
| 源码中存在真实落点 | 22 |
| 源码中**零出现**（仅存在于声明字符串） | **22** |

### 3.2 22 项零落点逐条定性

分三类，处置方式完全不同：

**(a) 功能已实现、只缺打点 —— 6 项（补打点即可，不必写新逻辑）**

| 声明指标 | 所属能力 | 已实现的事实（可验证） |
| --- | --- | --- |
| `sa_audit_dropped_total` | self_purification | `src/obs/audit.rs:181 push_bounded` 有界丢最旧，且单测 `capacity_drops_oldest` 通过；只是丢弃时不加一 |
| `sa_sessions_revoked_total` | self_purification | `src/services/auth.rs:428 revoke_all`、`logout` 均已实现并撤销 `revoked_at` |
| `sa_correctness_checks_total` | correctness_check | `src/orchestration/executor.rs:716` 已有 `agreement_score < 0.6` 判定与 `partially_succeeded` 流转 |
| `sa_agents_loaded_total` / `sa_context_tokens_saved_total` | lazy_loading | `src/orchestration/context.rs:87` 装配时按相关度取前 N 条、超预算摘要优先 |
| `sa_sandbox_violations_total` | sandbox | 沙箱画像（`sandbox_profile`）已在 agent 注册表与 catalog 暴露 |

**(b) 有等价信号、但名字对不上 —— 5 项（订正文档口径即可）**

| 声明指标 | 实际在用的等价信号 | 位置 |
| --- | --- | --- |
| `sa_rate_limited_total` | `sa_task_submit_rate_limited_total` | `src/api/handlers/task.rs:413` |
| `sa_tasks_created_total` | `sa_tasks_started_total` | 任务受理链 |
| `sa_tokens_used_total` | `sa_tokens_per_task` | 执行器预算统计 |
| `sa_http_5xx_total` | `sa_http_requests_total{status="5xx"}` 标签维度 | `src/obs/metrics.rs:61` |
| `sa_tool_calls_total` | `sa_custom_runner_steps_total`（弱等价：按步骤计，不按工具计） | 自定义 runner |

**(c) 整项能力零实现 —— 4 项能力、11 项指标（这才是真缺口）**

| 能力 id | 承诺原文（摘要） | 源码现状 |
| --- | --- | --- |
| `observability_alerting` | 阈值命中 → 告警；**同一告警 5 分钟内去重**；风暴时只留严重级别、其余聚合 | 全仓 `alert` 一词仅出现在声明与两处注释；无阈值判定、无去重窗口、无 `sa_alerts_total` 上报 |
| `evaluation_driven` | 每轮结束评估 + 每 100 次漂移检查；成功率环比降超 15% 告警；连续两轮降档 | `sa_eval_*` 三项零落点；`agreement_score` 取自规划置信度，不是评估结果 |
| `rag_tool_augmentation` | 先检索再生成；引用必须带来源；检索为空显式声明"未命中知识库" | 只有上下文装配的内存检索，无 RAG 检索链、无来源标识、无 `sa_rag_*` |
| `tool_invocation` | 工具白名单 + 参数 schema 校验；**禁止调用未注册工具**；同工具同参失败 2 次即停用 | 无工具注册表、无白名单、无 schema 校验层；执行实际由 worker 脚本进程承担 |
| `org_composition` | agent 注册、编组、并发与配额；超并发排队不丢弃 | `sa_agents_registered` / `sa_agent_concurrency` 零落点；注册表（catalog）只读展示，无并发口径 |

---

## 4. 线索 C：能力的"骨架"与"血肉"分级

按三段原文去源码找落点后，26 项能力分三级：

| 级别 | 含义 | 能力 |
| --- | --- | --- |
| 完整（证据+门禁+文档齐） | 有端到端证据、有脚本断言、有门禁守护 | automation · security · orchestration · memory · async_sse · cluster_observability（部分）· worker_text_quality |
| 骨架（逻辑在、口径缺） | 逻辑跑得通，但承诺的信号/文档对不上 | lazy_loading · correctness_check · self_purification · sandbox · cost_compute_optimization |
| 文字（仅文档承诺） | 声明在、代码里连功能词都不存在 | **observability_alerting** · **evaluation_driven** · **rag_tool_augmentation** · **tool_invocation** · **org_composition** |

---

## 5. 线索 D：文档失实（必须订正，否则查漏本身也在说谎）

`docs/02-capability-catalog.md` 的"验证"一栏声称"改坏 `tests/spec_matrix.rs` 后 `check-prompt-coverage.py` 会红"。

事实：**项目根本没有 `tests/` 目录，该文件从未存在**。真正在守门的是：

- `scripts/check-prompt-coverage.py`（判据 A/B/C/D 四条）
- `scripts/check-contract.py`
- `scripts/check-response-shape.py`

处置：文档改为指向真实门禁脚本，并说明"判据改坏→转红"的负向演练方式（白名单环境变量指向改坏副本）。

---

## 6. 缺口排序与处置建议

| 优先级 | 缺口 | 为什么优先 | 预估价 |
| --- | --- | --- | --- |
| **P0** | `observability_alerting` 整项缺失 | 26 项能力里唯一"告警"承诺；运维视角最刚需；纯后端可闭环、可硬断言 | 1 轮（接口+阈值+去重+指标+门禁18+文档） |
| P1 | `evaluation_driven` | 涉及"质量自省"，可基于已有 `agreement_score` 与任务台账算成功率/漂移 | 1 轮 |
| P1 | 6 项"只缺打点" | 成本极低（每项 1~5 行），补齐后 44 项声明指标零落点归零 | 0.5 轮 |
| P1 | 5 项"名字不对" | 纯文档订正 | 0.2 轮 |
| P2 | `tool_invocation` 白名单与 schema 校验 | 涉及执行面重构，牵动 worker 协议 | 2 轮 |
| P2 | `rag_tool_augmentation` | 需要文档源与检索后端，先做"未命中显式声明"的降级口径 | 2 轮 |
| P2 | `org_composition` 并发与配额 | 与集群"派得动"同源，适合一起做 | 1 轮 |

**判定**：能力清单不是"全都做完了"，而是"骨架都立起来了、5 项还只是文字"。这份清单就是后续逐功能打磨的队列。

> 本节是 **2026-11-13 的排序**，此后已推进两项，最新状态见第 9 节；下表 P0 行保留原文以留痕。

---

## 7. 本轮实测（多轮压测）

见 `docs/evidence/soak-*.json` 与本文第 8 节（数据在压测结束后回填）。

---

## 8. 附：审计脚本判据备忘

- 路由解析：按 `.route(` 后括号配平取整段，再从中提取 `get/post/put/patch/delete`——**不能用逐行正则**，链式 `.post()` 会漏。
- 指标落点：必须排除 `src/domain/capability.rs`，否则声明串本身会被误判为落点（这是本次审计踩过的坑：第一版比对判定"44 项全有落点"，是假绿）。

---

## 9. 后续进展（按时间倒序，本节持续追加）

### 2026-11-14 · 全量首跑抓到两处"假红"：报告把演练写成了 FAIL、门禁 17 没构造出前置条件

这一轮做的是**验收结论本身可信不可信**，不是加功能。全量第一次带演练跑完，退出码 1：24 道里 1 红、5 项演练报 3 项未按预期。逐项查下去，两处都是探针/报告的口径问题，功能没退化。

| 面 | 症状 | 真因 | 处置 |
| --- | --- | --- | --- |
| 演练摘要 | rc=0、演练 5/5 按预期，报告里整行却写着 `FAIL market …` | 演练行复用了门禁的 `tail_lines`（关键词含"通过"），抽到的全是 `2/12通过` 这类明细，真正的结论行 `[DRILL-OK] … 判据有判定力` 一个关键词都不沾 | 新增 `DRILL_KEYS` / `drill_tail()`：只抽自证结论；`run(..., drill=True)` 的 status 改 `DRILL-OK` / `DRILL-FAIL`；每行前置判定语"按预期变红 / 未按预期" |
| 门禁 17 A7 | 全量时 `A7/A7b/A7c/A7g` 4 项红，单跑 25/25 全绿 | 首连只取 2 帧、且拿**最后一帧**当锚点；全量时机器正忙，首连接上时任务已收尾，两帧正好是"快照 + 终态"，三次尝试全部落空 | 取 8 帧、锚点改为流里**第一个非终态帧**、尝试 3→6 次；断线窗口仍落在任务运行中 |
| 复核 | **24 道全绿 + 演练 5 项全按预期，rc=0** | `docs/evidence/regress-all.*` 同批刷新 | 门禁 17 用 `--slow-ms 60` 的极端参数也稳（续传补到 seq=41） |

值得单独记一条：`A7` 这次是**第三次**在同一处踩坑。前两次分别是"断言必须有帧"（服务端按设计 0 帧 EOF，误报）和"锚点落在终态帧"（首连第 2 帧约两成概率就是 succeeded）。三次的治法都一样——**不放宽断言，而是把前置条件构造出来**；顺带把踩坑路径本身立成正向断言（`A7g`：锚点=终态帧必须秒回 EOF），让"曾经的误报路径"变成被守护的语义。

---

### 2026-11-14 · CLI 从"只能起服务"补到能用，门禁 22/23 钉住外壳与客户端

起因是个很朴素的问题：接这套系统的人第一件事是想"提交个任务看看"，而此前唯一办法是手搓 curl，
且 curl 的失败样子与成功样子太像——401 也吐 JSON，不看状态码就会误判成功。设计口径见 **`docs/87-cli-client.md`**。

| 面 | 结论 | 依据 / 证据 |
| --- | --- | --- |
| 七个入口 | `serve` / `doctor` / `login` / `logout` / `submit` / `ls`（`list`）/ `show`（`get`）；**无子命令仍是 serve** | 20 余处门禁以 `[exe]` 无参启动，这是全套验收的隐含前提 |
| 两条真缺陷 | ① 刷新取值链漏了 `tokens` 一层，把**已作废的旧 refresh 写回文件**，下一条命令必 401（"再试一次就好"掩盖了它）；② 表格按字符数补空格，中文占两列致 UUID 截成 `204750aa-…?` | 修法各一行注释：两条链必须同样宽；`pad` 按显示宽度 |
| 并发刷新 | 三级退避重读（`Rescued` / `WaitedButStillOld` / `Hopeless`），第一版"只重读一次"实测 6 并发里半数 rc=3 | 8 进程 × 3 轮、12 进程 × 5 轮 `codes` 全 0 |
| 门禁 22 外壳 | 无参=serve、未知子命令被拒（不许静默当服务）、doctor 退出码 0/1/2 真能分开、退出后端口释放 | `probe_cli.py` **22/22**，约 4.9s，进 quick 层；`--drill` 10 项全红 |
| 门禁 23 客户端 | 真登录真提交：令牌落盘与覆盖、401→刷新→重试并回写、并发自愈、按显示宽度对齐、UTC 标签、`--json` 可 `json.loads`、连不上必须 rc=1 | `probe_cli_http.py` **53/53**，约 17.2s，只进 full 层；`--drill` 51 项全红 |
| 口径订正 | `docs/01`（2 处）、`docs/06`（标题/编号区间/表 3 行/演练表 3 行/命令清单/准出"18/18"）、`docs/README.md`（5 处）、`docs/86`（§4 第 12 条、§6 证据表、§3"十二处"→十三处）统一到 **24 道** | 全量实测输出对账 |
| 门禁 | **24 道全绿 + 负向演练 5 项全按预期**，耗时 397.3s | `docs/evidence/regress-all.txt` 与逐门禁 latest |

顺带记一条判据经验：门禁 23 的 D 组要按列位切表格，格式串列间还有分隔空格，第 `i` 列偏移是
`sum(COL_WIDTHS[:i]) + i`；少算这一个偏移会取到"上一列尾巴 + 下一列开头"，断言在**看起来有值**时恒绿。
这正是"新增一道门禁必须附一次负向演练"的由来。

---

### 2026-11-14 · 垂直域扩容 9→12、撤并 `collab`、9 处悬空引用落地

这轮做的是"造几个垂直智能体"和"去掉不必要的智能体"。两件事都不许拍脑袋，所以先定判据再动手，
判据与操作手册见 **`docs/86-vertical-agents.md`**（四道准入 + 十三处同步清单）。

| 面 | 结论 | 依据 / 证据 |
| --- | --- | --- |
| 新增 4 个垂直域 | `game` / `finance` / `legal` / `data` 立册，`task_types` 组合互不重合、也不被既有通用 agent 包含 | 四道准入逐条对过；各带独立 `docs/kb/*.md` |
| 撤并 1 个 | `collab-agent` 四条准入**全不合格**（任务类型是 `doc-agent` 的子集、无独立产物/判据/知识库），职责并入 `doc-agent` 的 `Collaboration` | `workers/collab-agent.py` 已删，`docs/03` 残留 0 |
| **没有**继续删的理由 | 余下 12 个在"同 task_type 关键词零重复、跨 agent 全量零重复"下各有独立覆盖；再删会削掉能力面 | 关键词唯一性由测试锁住，不靠人记 |
| 新增 2 条护栏 | `doc_refs_point_to_existing_files`（13 处 `doc_refs` 曾全是空引用）、`keywords_are_unique_within_task_type`（同分按表序取胜负） | `cargo test` **174 passed / 0 failed** |
| 补掉 9 处悬空引用 | `probe_llm_soak.py` / `doc-agent.py` / `alert.rs` / `observability.rs` 共 9 处注释写着"见 `docs/85`"，而 `docs/85` **此前不存在** | 新落盘 `docs/85-llm-evidence-and-boundaries.md`，把散落的实测数字集中成证据表 |
| 口径漂移订正 | 5 处"N 个 agent"（10/9 → 12）；README「18 道门禁」→ 20 道、「123 项冒烟」→ 157 项、openapi 27/31/39 → 29/33/51 | 全量门禁实测输出对账 |
| 门禁 | **首次跑出含第 20 道的完整全量：21 道全绿 + 负向演练 2 项按预期** | `docs/evidence/regress-all.txt` 与 `evidence/20-probe_topic_guard-latest.txt` |

同一轮还查清了一个"看着像功能退化、其实是探针自己撞墙"的假缺陷：`soak-v1` 110 轮在 G1/G4/G5
集体转红，根因是**单账号提交配额 60 次/小时**、第 83 轮后提交全被 429 挡下，而那 40 轮模型轮
G3 反而全过。修法不是调高配额，而是 ① 探针按用户轮转（`--users`，默认 3）；② 新增 G8 断言
"第 61 次必须 429 且带 `Retry-After`"——轮转负责跑得完，G8 负责限流没被绕过。详见 `docs/85` §5。

---

### 2026-11-14 · P0 告警能力已闭环，不再只是文字

`observability_alerting` 从第 4 节的"文字级"升为"有门禁守护"。做法与判据：

| 面 | 落地 | 判据要点 |
| --- | --- | --- |
| 阈值与去重 | 阈值经环境变量注入，5 分钟去重窗口在进程内生效 | 去重不能靠"指标没涨"来证——指标不涨既可能是没触发、也可能是被抑制，两者长得一样 |
| 风暴抑制 | 超阈值时只保留严重级别，其余聚合计数 | 断言"严重级别仍上报、非严重被聚合"两个数字同时成立 |
| 指标落点 | 告警计数真落点（`sa_*` 上报） | 逐段读指标，不看日志文本 |
| 门禁 | `scripts/probe_alerting.py`（第 19 道） | 阈值压到必然命中，逐段断言 命中 → 窗口内抑制 → 过窗复发 → 风暴只留严重 → 指标落点；三阶段**各起独立进程**，避免同进程改参数自证 |
| 负向演练 | `--drill-no-dedup`（关掉去重） | 期望 A8 / B3 转红；**入口已备，尚未留正式演练记录**（见 docs/06 §2.1 待办） |

仍未动的（与第 6 节一致，按优先级）：

1. **6 项"只缺打点"** —— 功能都在、每项 1~5 行即可让"44 项声明指标零落点"归零，性价比最高。
2. **5 项"名字对不上"** —— 纯文档订正，口径统一后线索 B 才能稳定报警。
3. `evaluation_driven`、`tool_invocation`、`rag_tool_augmentation`、`org_composition` —— 仍为骨架/文字。
