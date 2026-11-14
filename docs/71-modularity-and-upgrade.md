# 71 · 模块化部件图与换件手册（第二轮：三个可插拔部件 + 22 项换件实测）

> 一句话：**「换一个部件不殃及全部」在本项目里不是设计口号，而是一条被 22 项行为断言 + 15 项静态断言守着的不变式。**
> 所有主张都能用两条命令复现：`python scripts/swap_drill.py`、`python scripts/check-modularity.py`；不放心就 `python scripts/regress_all.py` 跑全套 7 道门禁。

生成时间：2026-11-09 深夜（第二轮）｜对应代码实况：单测 **80 绿** / 冒烟 **123 绿 ×2 后端** / 换件演练 **22 绿** / 静态守护 **15 绿**

---

## 1. 结论卡

| 编号 | 结论 | 证据 |
|---|---|---|
| C1 | 后端现在有 **3 个真·可插拔部件**：存储后端、执行后端、审计出口。换它们**只改环境变量，不动一行业务代码** | `SA_STORAGE` / `SA_RUNNER` / `SA_AUDIT` + `swap_drill.py` D1/D2/D4 全绿 |
| C2 | 换完之后上层看到的东西**逐个字段一致**：状态码序列 18/18、响应结构签名 18/18、计划步骤名序、权限档位、trace 口径、token 记账 | `swap_drill.py` D1c/D1d/D2b–D2g/D4c/D4d |
| C3 | 业务层对具体后端**零引用**，由静态检查长期守着，不靠自觉 | `check-modularity.py` 15/15（M2 扫 `api/handlers`、`services`、`domain`、`orchestration` 全部 `.rs`） |
| C4 | 运维**不必猜**当前装的是哪个部件 | `overview.audit_backend` / `overview.runner_backend`（D4e 断言） |
| C5 | 三个部件**都能被"坏配置"拒绝启动**，绝不静默降级成"看起来正常" | D4g（`SA_AUDIT` 非法值拒启）、D4h（`SA_AUDIT_FILE` 指目录拒启）、执行后端同理 |
| C6 | 本轮抓出 **2 个新缺陷**，都属于"不报错、只悄悄丢东西"那类 | §7.2 停进程即丢数据、§7.3 落盘却查不到历史 |

---

## 2. 部件地图

部件 = 一个可以整块换掉、且换掉后其它部件代码不必改的东西。

| # | 部件 | 端口（契约） | 现有实现 | 装配点（唯一） | 怎么换 | 换件影响面 |
|---|---|---|---|---|---|---|
| 1 | **存储后端** | `trait Backend`（`store/backends/mod.rs`） | `JsonBackend`（内存主存 + 写后落盘 + 定期全量快照）／`MemoryBackend`（纯内存） | `store::backends::open(kind, path)` | `SA_STORAGE=json\|memory` | 仅 `Store` 内部；handler/service/domain/测试 0 改动 |
| 2 | **执行后端** | `trait StepRunner`（`orchestration/executor.rs`） | `SimRunner`（确定性模拟）／`ProcessRunner`（白名单脚本子进程） | `runners::build(config)` | `SA_RUNNER=sim\|process` + `SA_RUNNER_DIR` | 仅 `Executor.runner` 字段；治理链路（鉴权/预算/重试/熔断/审计/指标/飞轮）不动 |
| 3 | **审计出口** | `trait AuditSink`（`obs/audit.rs`，只需实现 `record` + `events` + `kind`） | `MemoryAuditSink`（有界环形）／`FileAuditSink`（环形 + JSONL 落盘，启动回读） | `AuditKind::open(capacity, path)`（`config.audit.open`） | `SA_AUDIT=memory\|file` + `SA_AUDIT_FILE` | 仅 `AppState.audit`；handlers/services/orchestration 只见 `&dyn AuditSink` |
| 4 | 治理外壳 | `Executor`（无 trait） | 单一实现 | `handlers::task::create` | 改源码 | 全链路 —— 故列为"非可插拔" |
| 5 | 指标 | `Metrics`（具体结构，手写 Prometheus 文本） | 单一实现 | `state.rs` | 改源码 | 中 |
| 6 | 限流 | `RateLimiter`（具体结构，令牌桶） | 单进程内存 | `state.rs` | 改源码 | 中 |

> 说明：4~6 是"**逻辑上独立的模块，但不是可替换部件**"（没有 trait 端口）。这不算失败，但要写清楚——见 §10 改造路线。
> 审计出口曾是第 4 类（只有具体结构），**本轮已升级为第 3 个真·可插拔部件**，这是这一版与原版的实质差别。

---

## 3. 换件手册 A：换存储后端

### 3.1 只换现成的（0 行代码）

```powershell
$env:SA_STORAGE="memory"   # json | memory
.\super-agents.exe
```

等价别名（`StorageKind::parse` 接受）：`file|snapshot` → json，`mem|ephemeral` → memory。
非法值**拒绝启动**，不静默降级。

### 3.2 新增第三种后端（如 SQLite / 远端 KV）

只有 **5 个地方**要动，全在 `store/backends/` 一个目录内：

| 步 | 文件 | 改什么 |
|---|---|---|
| 1 | `store/backends/sqlite.rs`（新建） | `impl Backend for SqliteBackend`：`kind/path/with_read/with_write/flush/flush_if_dirty/is_dirty` |
| 2 | `store/backends/mod.rs` | `pub mod sqlite;` |
| 3 | 同上 | `enum StorageKind` 加一个变体 `Sqlite` |
| 4 | 同上 | `ALL` 数组补 `"sqlite"`、`as_str()` 补分支、`parse()` 补别名 |
| 5 | 同上 | `open()` 的 `match` 补一行 |

**不需要动**：`store/mod.rs`（门面）、`api/`、`services/`、`domain/`、`orchestration/`、任何测试、任何脚本。
理由：业务层拿到的类型是 `Store`，`Store` 内部持有 `Box<dyn Backend>`，编译期就切断了依赖。

> 端口硬要求（写在 trait 注释里）：`with_read` / `with_write` **必须恰好调用一次闭包**，一次读写必须原子完成。违反它 = 数据竞争或丢更新，`Store` 层会立刻暴露。

---

## 4. 换件手册 B：换执行后端

### 4.1 只换现成的

```powershell
$env:SA_RUNNER="process"
$env:SA_RUNNER_DIR="D:\my-scripts"   # 每个 agent 一个 <agent-id>.cmd
.\super-agents.exe
```

`process` 模式缺 `SA_RUNNER_DIR` 会**拒绝启动**并提示可选值——不会悄悄退回模拟（"假成功"比报错危险得多）。

### 4.2 新增第三种执行后端（如容器 / HTTP 推理网关）

只有 **4 个地方**：

| 步 | 文件 | 改什么 |
|---|---|---|
| 1 | `orchestration/runners/container.rs`（新建） | `impl StepRunner`，实现 `fn run_step(&self, req: &StepRequest) -> StepOutcome` |
| 2 | `orchestration/runners/mod.rs` | `pub mod container;` + `enum RunnerKind` 加变体 |
| 3 | 同上 | `ALL` / `as_str` / `parse` 三处补齐 |
| 4 | 同上 | `build(&Config)` 的 `match` 补一支 |

**不需要动**：`executor.rs` 的治理逻辑、`handlers/`、`services/`、`domain/`。
新后端返回值只需表达三件事：`Verdict::Ok{tokens,output}` / `Retryable(msg)` / `Fatal(msg)`，外加可选 `counters` 与 `extra_trace`。
**重试、退避、预算降档、熔断、审计、指标、飞轮沉淀全部由治理层做，后端不许自己重试。**

---

## 5. 换件手册 C：换审计出口（本轮新增）

### 5.1 只换现成的

```powershell
# 默认：有界内存，进程重启即失
.\super-agents.exe

# 落盘：内存环形 + JSONL 追加写，重启后回读（运维排查要用的就是它）
$env:SA_AUDIT="file"
$env:SA_AUDIT_FILE="D:\sa-data\audit.jsonl"
.\super-agents.exe
```

等价别名：`mem|ephemeral` → memory，`jsonl|disk|durable` → file。
坏配置一律**拒启**：`SA_AUDIT=乱填` → 退出码 1；`SA_AUDIT=file` 但 `SA_AUDIT_FILE` 指到目录 → 退出码 1。

### 5.2 新增第三种实现（如 Loki / CLS / Kafka）

只有 **2 个方法**要写，因为分页、过滤、拒绝计数都在 trait 默认实现里：

```rust
impl AuditSink for LokiSink {
    fn record(&self, actor: &str, action: &str, target: &str,
              allowed: bool, reason: &str, request_id: &str) { /* 发 HTTP，失败只 warn 不 panic */ }
    fn events(&self) -> Vec<AuditEvent> { /* 返回本地可见的近端缓存，最旧在前 */ }
    fn kind(&self) -> &'static str { "loki" }
}
```

登记 4 处（与存储/执行同构）：`pub mod`、`enum AuditKind` 加变体、`ALL` / `as_str` / `parse` 补齐、`open()` 的 `match` 补一支。

**这是刻意设计的**：如果每个后端各自实现 `page/deny_count/list`，三种后端迟早出现三种分页口径，前端就要按后端分支写——那就等于"换件殃及前端"。把语义收在 trait 默认实现里，是防止这件事最省力的办法。

### 5.3 换成 file 之后的行为变化（必须知道的两点）

| 项 | memory | file |
|---|---|---|
| 进程重启后 `/observability/audit` 还能查 | 不能（清空） | **能**（启动回读尾部，D4i 断言） |
| `denied` 计数是否跨重启 | 不跨 | **跨**（D4i 断言同一 token 仍有效且台账仍在） |

---

## 6. 换件之后必须仍然成立的不变式

这就是"不殃及全部"的可执行定义，全部由 `swap_drill.py` 断言：

| 不变式 | 断言方式 | 本轮结果 |
|---|---|---|
| I1 接口行为不随存储改变 | 同一套 18 个请求在 json / memory 两后端跑，**状态码序列逐条相等** | 18/18 一致 |
| I2 响应结构不随存储改变 | 逐响应比较**结构签名**（递归字段集合 + 类型，值折叠） | 18/18 一致 |
| I3 计划不随执行后端改变 | 同一任务在 sim / process 下 `plan[].name` 序列相等 | 一致（4 步同名同序） |
| I4 权限档位不随执行后端改变 | `plan[].permission` 序列相等 | 一致（cross_agent/write/read/write） |
| I5 审计口径不随执行后端改变 | `trace[].phase+outcome` 序列相等 | 一致（5 条 vs 5 条） |
| I6 预算记账不随执行后端改变 | 两种后端 `tokens_used > 0` 且步骤结果字段集合相同 | 一致 |
| I7 业务层不依赖任何具体后端 | 扫 `api/handlers`、`services`、`domain`、`orchestration`，禁止出现 `JsonBackend/MemoryBackend/ProcessRunner/SimRunner/MemoryAuditSink/FileAuditSink`；`SA_STORAGE/SA_RUNNER/SA_AUDIT*` 只许 `config.rs` 读 | 15/15 通过 |
| I8 换审计后端不改接口行为 | memory / file 两套跑同一序列，状态码 18/18、结构签名 18/18 相同 | 一致 |
| I9 落盘型审计重启可追溯 | 起 → 注册 → **硬杀** → 同数据目录重启 → 同一 token 仍有效且 `audit.total ≥` 重启前 | 1 条 → 1 条，通过 |
| I10 坏配置必须拒启 | `SA_AUDIT` 非法值、`SA_AUDIT_FILE` 指目录，退出码必须非 0 | 均 rc=1，通过 |

---

## 7. 两轮演练抓到的 3 个真缺陷（最重要的三节）

### 7.1 缺陷一：沙箱记账错挂在模拟后端上（第一轮抓到）

**现象**：`swap_drill.py` 首次跑 D2e 直接红——`sim: 5 条 trace`，`process: 4 条`，换成真执行后端后 `sa_sandbox_runs_total` 一次都不涨。

**根因**：`if step.sandbox { 记账 }` 写在了 `SimRunner::run_step` 里。而 `step.sandbox` 是**治理层的规划决策**，不是后端行为。写在模拟后端里，等价于"只有模拟执行才开沙箱、才留审计"。

**危险点**：不崩、不报错，只是**换件之后审计与指标悄悄变形**——运营看到"沙箱 0 次"以为没开，实际是换了个后端。这是"殃及全局"最隐蔽的形态：**不是崩，是撒谎**。

**修法**：把记账权收回治理层执行通用路径；若后端自带 `phase == "sandbox"` 轨迹则跳过以免重复。

**守护**：`sandbox_accounting_is_governance_owned_not_backend_owned`、`sandbox_trace_not_double_counted_with_builtin_runner`。

---

### 7.2 缺陷二：进程一停，最近一段写入就消失（第二轮抓到）

**怎么发现的**：写 D4i（验证 file 审计重启可追溯）时，断言红了：

```
[FAIL] D4i 重启后历史台账仍可查询  -> 重启前=1 条 / 重启后=None 条
```

顺手验 token：重启后 `GET /auth/me` 返回 **401「账号不存在或已被删除」**——不是审计丢，是**刚注册的账号根本没落盘**。

**根因**（两处叠加，缺一不可）：

1. 数据落盘只靠后台**全量快照**，默认 `SA_SNAPSHOT_INTERVAL_SECS=15`：进程被停时，最近 ≤15 秒的写入全在内存里，随进程消失。
2. 优雅退出只等 `tokio::signal::ctrl_c()`：容器 `docker stop`、systemd、K8s 发的都是 **SIGTERM**；Windows 的注销/关机走 `CTRL_CLOSE`/`CTRL_SHUTDOWN`。这些信号一个都没接 → 进程被等满超时后 `SIGKILL`/`TerminateProcess` 强杀 → 最后一段写入照样丢。

**危险点**：这是"看起来能落盘、其实不可靠"。开发者本地按 Ctrl+C 测，一切正常；一上 `docker stop` 就丢数据，且**没人会怀疑是落盘机制**。

**修法**（两步，都不动业务代码）：

```rust
// ① main.rs：写后落盘窗口，独立于 15 秒全量快照
let every = Duration::from_millis(state.config.flush_interval_ms.max(50));
tokio::spawn(async move { loop { ticker.tick().await; state.store.flush_if_dirty()?; } });

// ② main.rs：把"只等 Ctrl+C"补全成四种信号
tokio::select! {
    _ = tokio::signal::ctrl_c() => {},
    _ = other => {},   // Windows: CTRL_BREAK / CTRL_CLOSE / CTRL_SHUTDOWN；Unix: SIGTERM
}
```

新增环境变量 `SA_FLUSH_INTERVAL_MS`（默认 500，可调 50~60000）。

**实测（修复后）**：

| 场景 | 结果 |
|---|---|
| 注册 → 等 1.2 s → **硬杀** → 同目录重启 | 账号仍在（`/auth/me` 200）、审计 1 条仍在 |
| `SA_FLUSH_INTERVAL_MS=50` → 等 0.25 s → 硬杀 → 重启 | 0 条丢失（D4j） |
| 单测 | `file_sink_reload_after_restart` 等 3 条新单测守护 |

**仍有的边界（诚实写）**：Windows `TerminateProcess` 不给进程任何收尾机会，**最多丢最后一个窗口内的写入**（默认 500 ms，可调到 50 ms）。要真正零丢失得换 WAL/SQLite 后端——见 §10。

---

### 7.3 缺陷三：`SA_AUDIT=file` 落了盘，但接口查不到历史（第二轮抓到）

**现象**：JSONL 文件明明一行行在长，D4f 断言"文件存在、7 行、无非法行"也绿；但进程重启后 `/observability/audit` 返回 **total=0**。

**根因**：`FileAuditSink::open()` 只以 append 方式打开文件，**从未把已有内容读回内存队列**。而所有查询（`page`/`list`/`denied_count`）都走 `events()` → 内存环形队列。于是"落盘"只是给了一个好看的文件，接口侧等于没有历史。

**危险点**：安全排查最需要历史台账的时刻，恰恰是进程重启之后。这时候恰恰什么都没有。

**修法**：启动时回读尾部并填回环形队列，三条硬约束同时守住：

| 约束 | 为什么 |
|---|---|
| 只读尾部 `MAX_LOAD_BYTES = 8 MB`，且丢弃可能被截断的首行 | 历史文件可能几个 G，一次性读进内存就是新的故障源 |
| 坏行 / 半截行**跳过不 panic** | 一条脏数据不能让整个后端起不来 |
| 只保留最后 `capacity` 条 | 与运行期"有界 + 丢最旧"语义一致，不能因为重启就变成无界 |

**守护**：`file_sink_reload_after_restart`（跨重启条数 + deny 计数一致）、`file_sink_skips_corrupt_lines`（坏行跳过且之后仍可写）、`file_sink_reload_respects_capacity`（100 条写入 + capacity=64 → 只留最后 64 条，且最后一条是第 100 条）。

---

## 8. 实测记录（可复跑，本次实跑输出）

| 门禁 | 命令 | 结果 |
|---|---|---|
| 单元测试 | `cargo test --quiet`（ASCII 副本） | **80 passed / 0 failed** |
| 冒烟 · json 后端 | `python scripts/smoke.py` | **123 / 123** |
| 冒烟 · memory 后端 | `SET SA_STORAGE=memory` + `python scripts/smoke.py --port 18097` | **123 / 123** |
| 契约一致性 | `python scripts/check-openapi.py` | 0 处不一致 |
| OpenAPI ⇄ TS | `python scripts/gen-ts-types.py --check` | 一致 |
| 执行端口实测 | `python scripts/verify_runner.py` | **15 / 15** |
| 换件演练 | `python scripts/swap_drill.py` | **22 / 22** |
| 模块化静态守护 | `python scripts/check-modularity.py` | **15 / 15** |
| 一键全量 | `python scripts/regress_all.py` | **7 道门禁全绿（7/7）** |

换件演练 22 项分组：

| 组 | 项数 | 盯的是什么 |
|---|---|---|
| D1 存储互换 | 4 | 18 请求状态码 / 结构签名逐条相等 |
| D2 执行互换 | 7 | 终态、计划名序、权限档位、trace 口径、token 记账、结果字段集合 |
| D4 审计互换 | 10 | 行为一致、部件身份可见、JSONL 合法、**硬杀重启可追溯**、flush 窗口生效、坏配置拒启 |
| D3 静态守护 | 1 | 15 项静态检查聚合 |

留档：`docs/evidence/regress-all.txt`、`docs/evidence/regress-all.raw.txt`、`docs/evidence/modularity.txt`。

---

## 9. 诚实的边界（没做 / 不保证）

1. **不是热插拔**。换件 = 改环境变量 + 重启进程，运行期不能替换。做热切换需要"双实例 + 连接排空"，本项目未做，也不建议现在做。
2. **只有 3 个真·可插拔部件**（存储 / 执行 / 审计）。治理外壳、指标、限流仍是"独立模块但无 trait 端口"，换它们要改源码。
3. **没有 SQLite / 远端存储实现**。端口留好了；`JsonBackend` 的全量快照在数据量变大后是瓶颈，而且**全量快照本身无法做到零丢失**（无论窗口压多小）。上万记录或要求不丢数据，务必换 SQLite/WAL。
4. **`SA_RUNNER=process` 只认 Windows `.cmd` 白名单脚本**，没有容器隔离、没有 CPU/内存配额。它是"真执行"，不是"安全沙箱"。
5. **`process` 后端只在 Windows 验证过**；Unix 下 `.cmd` 不可执行，需要补 `.sh` 分支（未做）。
6. **file 审计回读上限 8 MB**：磁盘文件不丢，但重启后接口分页只能看到最近一段。
7. **并发写仍是单写锁**。`with_write` 串行化，吞吐受单进程限制；多实例共享存储未验证。

---

## 10. 下一批可插拔改造（按性价比排序，未做）

| 优先级 | 部件 | 端口建议 | 为什么值得做 | 代价 |
|---|---|---|---|---|
| P0 | 存储后端 SQLite（WAL） | 复用 `trait Backend` | 一次解决两个问题：全量快照写放大 + 硬杀丢窗口。这是唯一能真正做到"不丢已确认写入"的路径 | 2~3 天，仅新增文件 + 5 处登记 |
| P1 | 指标输出 | `trait MetricsExporter` | 现在手写 Prometheus 文本；将来要接 OTLP | 1 天 |
| P1 | 限流器 | `trait Limiter` | 多实例时需要 Redis 共享计数 | 1 天 + 外部依赖 |
| P2 | 身份提供方 | `trait IdentityProvider` | 现在只有本地密码 + JWT；接 OIDC 要改 handler | 3 天 |
| P2 | 任务执行队列 | `trait Scheduler` | 现在任务提交即同步跑完；长任务需要异步队列 + 进度 SSE | 3~5 天 |

> 排序逻辑：先做"换掉不会影响业务正确性、只影响运维形态"的部件（指标/限流），
> 再做"影响能力边界"的部件（存储/身份/队列）。每做一件，都必须同步给 `swap_drill.py` 增一段断言——否则等于没做。

---

*本文件第二轮更新于 2026-11-09 深夜，所有数字均来自当日实跑输出（`python scripts/regress_all.py`），原始日志在 `docs/evidence/`。*
