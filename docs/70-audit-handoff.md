# 70 · 审计交接书（给第三方 AI / 人核对用）

> 这份文档只解决一个问题：**让你在不信任我的前提下，5 分钟内判断这份交付是真的还是编的。**
>
> 规则只有一条：**本文件里每一条"结论"，都必须能对应到下表中一条可执行命令**。
> 凡是我无法用命令证明的，一律标注为「设计设想（未实现）」，不得当作事实引用。

> ⏱ **这是 2026-11-09 的历史快照，请勿据此判断当前状态。**
> 它写得比后来几轮更细（20 条主张与证据逐条对照），作为方法论样例仍有价值；
> 但其中的测试数、门禁数、默认值配置都已被后续迭代改变。
> **当前事实以桌面《Marvis-后端交接文档.md》为准**（2026-11-11：131 单测 / 156 项冒烟 / 14 道门禁）。
> 两份文档冲突时，一律以源码与 `docs\evidence\` 下最新输出为准。

- 生成时间：2026-11-09
- 项目：personal super agents（个人超级 agents 系统）后端 + 方案文档
- 技术栈：Rust + axum（后端，已实现）／TypeScript（前端，**尚未写**）／Python worker（架构设计中，**尚未实现**）

---

## 1. 交付物清单（磁盘实况，可逐项 `dir` 验证）

根目录：`C:\Users\94355\Desktop\新建文件夹(7)`

| 路径 | 是什么 | 规模 | 真实性 |
|---|---|---|---|
| `src/` | 后端 Rust 源码（44 个 `.rs`，8536 行） | 44 文件 | 真实实现，`cargo test` 80 项全绿 |
| `Cargo.toml` / `Cargo.lock` | 依赖清单（15 个直接依赖） | — | 可 `cargo tree` 复现 |
| `docs/00-project-plan.md` | 项目总纲：目标、边界、模块、验收口径 | 131 行 | 文档（设计意图） |
| `docs/10-verification-report.md` | 验收报告 + 3 轮变更史 + §10 缺口全表 | 285 行 | 结论可复现，见 §3 |
| `docs/20-rust-python-architecture.md` | Rust 热路径 + Python 编排的落地设计 | 150 行 | **设计文档，Python 侧尚未实现** |
| `docs/30-ts-frontend-plan.md` | TS 前端方案（选型/目录/数据流/路由/实施阶段） | 251 行 | **规划，无代码** |
| `docs/40-api-contract-ts.md` | API 契约导读 + 全量 TS 类型 + 端点表 | 522 行 | 已按生成物逐行复核 |
| `docs/50-build-and-verify.md` | 构建与验证手册（含冒烟脚本用法） | 196 行 | 步骤可照做 |
| `docs/60-review-checklist.md` | 核对清单（A–F 节，每条带证伪方法） | 185 行 | — |
| `docs/openapi.json` | **接口契约**（OpenAPI 3.1，24 路径 / 28 操作 / 50 schema） | 4608 行 | 机器生成，可 `--check` |
| `generated/types.generated.ts` | **前端 TS 类型**（40 interface + 9 联合类型） | 404 行 | 机器生成，可 `--check` |
| `scripts/gen-openapi.py` | 源码 → `openapi.json` 生成器（零侵入） | — | 可读、可跑 |
| `scripts/gen-ts-types.py` | 源码 → TS 类型生成器（零侵入） | — | 可读、可跑 |
| `scripts/check-contract.py` | 40 号文档 interface ⇄ 源码 struct 字段比对 | — | 可读、可跑 |
| `scripts/check-openapi.py` | `openapi.json` ⇄ 源码 schema 一致性（字段/可选性/枚举） | — | 可读、可跑 |
| `scripts/smoke.py` | 端到端冒烟（拉起真 exe 打真 HTTP，123 项） | — | 可跑 |
| `scripts/swap_drill.py` | 换件演练（存储/执行/审计三部件互换 + 静态守护，22 项） | — | 可跑 |
| `scripts/check-modularity.py` | 模块化静态守护（15 项：业务层不得引用具体后端） | — | 可跑 |
| `scripts/regress_all.py` | 一键跑全部 7 道门禁 | — | 可跑 |
| `docs/71-modularity-and-upgrade.md` | 部件地图 + 三份换件手册 + 不变式 + 演练实测 | — | — |
| `docs/evidence/*.txt` | 上述脚本的**原始输出留档**（6 份，含 `round3-summary.txt` 一页式汇总） | — | 可重跑对照 |

> **没有任何一个是"只存在于对话里"的。** 你在对话框里看到的结论，磁盘上都有落点。

---

## 2. 五分钟证伪路径（最少 4 条命令）

在 PowerShell 里依次执行（第 2 条需要 Rust 工具链，见 `docs/50-build-and-verify.md`）：

```powershell
cd "C:\Users\94355\Desktop\新建文件夹(7)"

# ① 契约是否与源码一致（<1 秒，不需要编译）
python scripts\gen-openapi.py --check          # 期望：CHECK: 一致 | paths=24 operations=28 schemas=50
python scripts\check-openapi.py                # 期望：检查 struct schema: 40 | 枚举 schema: 9 | 0 处不一致
python scripts\check-contract.py               # 期望：一致 21 | 有差异 0 | 查无同名 0

# ② 单测是否真的全绿（需要 Rust，约 30 秒首编）
#    注意必须在 ASCII 副本里跑，中文路径会让 MinGW 的 ld 失败（见 50 号文档 §3）
$Dst = "$env:LOCALAPPDATA\sa-build"
robocopy "." "$Dst" /MIR /XD target .git /NFL /NDL /NJH /NJS /NP | Out-Null
$env:CARGO_TARGET_DIR = "$Dst\target"
Set-Location $Dst
cargo +stable-x86_64-pc-windows-gnu test --lib # 期望：80 passed; 0 failed

# ③ 端到端是否真的通（需要 exe，约 2 分钟）
Set-Location "C:\Users\94355\Desktop\新建文件夹(7)"
python scripts\smoke.py                        # 期望：合计 123 项 | 通过 123 | 失败 0
```

**四条命令，全部退出码 0，才算"没报假"。** 任何一条不符，直接判定交付不合格，不用看文档怎么解释。

---

## 3. 主张 → 证据 → 复现（每条都可独立验证）

| # | 我的主张 | 源码/产物落点 | 怎么验 | 2026-11-09 实测 |
|---|---|---|---|---|
| 1 | 单测 80 项全绿 | `src/**/*.rs` 的 `#[cfg(test)]` 模块 | `cargo test --lib` | 80 passed / 0 failed |
| 2 | 端到端冒烟 123 项全绿 | `scripts/smoke.py` + `docs/evidence/smoke-latest.txt` | `python scripts/smoke.py` | 123 / 123（json 与 memory 双后端各一遍） |
| 3 | 共 24 路径 / 28 操作 | `src/api/mod.rs` 的 `router()`；`docs/openapi.json` 的 `paths` | `python -c "import json;d=json.load(open('docs/openapi.json',encoding='utf-8'));print(len(d['paths']),sum(len(v) for v in d['paths'].values()))"` | 24 / 28 |
| 4 | 根路径三个运维端点不带 `/api/v1` | `/healthz` `/readyz` `/metrics` | 同上（看 paths 里有无这三个 key） | 存在 |
| 5 | 默认监听 8088 | `src/config.rs:50`（`SA_BIND` 可覆盖） | 读该行 | 8088（**冒烟脚本会用随机端口如 18099 起服务**，避免占用 8088，属正常现象） |
| 6 | 任务状态恰好 7 值 | `src/domain/task.rs:81` | 读该 enum | queued/running/succeeded/failed/partially_succeeded/aborted_budget/cancelled |
| 7 | 角色恰好 3 值 | **`src/security/rbac.rs:7`**（不在 `domain/user.rs`） | 读该 enum | owner/admin/member |
| 8 | 用户状态 2 值 | `src/domain/user.rs:9` | 读该 enum | active/disabled |
| 9 | 权限恰好 7 项 | `src/security/rbac.rs:43` | 读该 enum | read/write/sandbox_exec/memory_write_shared/memory_write_system/cross_agent/admin |
| 10 | 任务提交限流 60 次/小时/用户 | `src/api/handlers/task.rs:21`（`TASK_SUBMIT_QUOTA`）、`:83`（`create()` 第 0 步检查） | 读两行 + `cargo test --lib task_submit_quota` | 冒烟 S87 实测第 56 次后命中 429（前面已消耗配额），`Retry-After=59` |
| 11 | 限流**先于**参数校验 | `task.rs::create()` 里 `limiter.check` 在 `validator` 之前 | 冒烟 S92：已限流用户提交非法参数 → 返回 429 而非 422 | 429 |
| 12 | 预算耗尽转 `aborted_budget`，不静默放行 | `src/orchestration/executor.rs` 预算分支 | 冒烟 S71b/S71c | 状态 `aborted_budget`，`result_summary` 含"预算" |
| 13 | 越权回 403，不再是 422 | `src/error.rs` + `src/security/rbac.rs` | 冒烟 S74/S76/S77c | 403 + `code=forbidden` |
| 14 | 执行后端可替换且治理层不动 | `src/orchestration/executor.rs:119`（`trait StepRunner`）、`:128`（`SimRunner`）、`:561`（impl） | 单测 `custom_step_runner_replaces_backend_and_keeps_governance`（`:1006`） | ok |
| 15 | 鉴权失败先于执行动作 | 同上 | 单测 `authz_denial_precedes_step_runner`（`:1046`） | ok |
| 16 | 业务代码无 `todo!` / `unimplemented!` / `FIXME` / `TODO` | 37 个 `.rs` 全文 | `Select-String -Path src\**\*.rs -Pattern "todo!|unimplemented!|FIXME|TODO"` | 0 处 |
| 17 | `/metrics` 不是裸奔 | `src/obs/metrics.rs` + 中间件 | 冒烟 S05（无凭据 401）/ S06（正确令牌 200）/ S09（错令牌 401） | 401 / 200 / 401 |
| 18 | `openapi.json` 与源码无漂移 | `docs/openapi.json` | `python scripts\gen-openapi.py --check` | `CHECK: 一致` |
| 19 | TS 类型与源码无漂移、无手改 | `generated/types.generated.ts` | `python scripts\gen-ts-types.py --check` | 一致（40 interface + 9 联合） |
| 20 | `openapi.json` 的**可选性**没错标 | 40 个 schema | `python scripts\check-openapi.py` | 0 处不一致 |

> 第 20 条是专门为"前端别乱写"加的：`Option<T>` 字段若被错标成 `required`，前端会写出必填校验、真调用时崩。
> 校验器把源码 `Option<>` / `serde(default)` / `skip_serializing_if` 三种情况都算进"可选"，与 schema 的 `required` 反向比对。

---

## 4. 明确"没做"的清单（**这一节比上面更重要**）

| # | 项 | 真实状态 | 请不要误以为 |
|---|---|---|---|
| 1 | **前端代码** | **一行都没写**（用户将自行编写 TS 前端） | 有 `30-ts-frontend-plan.md` ≠ 有前端 |
| 2 | Python worker / 编排 | **未实现**，仅在 `20` 号文档中设计 | 架构图不是代码 |
| 3 | 任务真实执行 | **模拟**：`SimRunner` 按规则合成 trace/tokens/result | 会真的调用大模型 |
| 4 | SSE / 实时推送 | 未实现，前端只能轮询 | 契约文档里预留了 `GET /tasks/{id}/events`，但**后端没有这个路由** |
| 5 | Swagger UI / 运行时校验 | 没有。只有静态 `openapi.json` | 后端**没有** `/api-docs` HTTP 端点，`openapi.json` 只能从磁盘读 |
| 6 | 数据库 / 事务 | 无。`Store::in_memory()` + JSON 快照 | 多实例、崩溃恢复不可靠 |
| 7 | `sa_http_requests_total` 指标 | enum 里有、`inc_http()` 有实现，但**生产代码无调用点**（仅 `metrics.rs:167` 测试里调用） | `/metrics` 里看不到它，冒烟只打 `[INFO]` 不打 `[OK]` |
| 8 | 目录端点鉴权 | `GET /agents`、`GET /capabilities` **完全免鉴权**（静态元数据，无用户数据） | 若将来目录里混入用户数据 = 泄露 |
| 9 | 限流覆盖 | 只有注册 / 登录 / 刷新 / 任务提交 四类 | 其余端点可被刷 |
| 10 | 错误语义统一性 | 越权 403、校验 422 已分开；但"他人资源"在不同端点回 403 或 404 **不统一** | 前端错误分支要按端点区分 |

---

## 5. 关于前端：为什么一个字都不写

用户明确要求「前端的你不要乱写，我准备后期自己写」。因此本交付对前端只做三件事，**不做第四件**：

| 做 | 内容 | 性质 |
|---|---|---|
| ✔ 做 | `30-ts-frontend-plan.md`：技术选型 + 理由（含"为什么不选 Next.js / Redux"）、目录结构、数据流分层、双令牌会话、路由与页面清单、任务状态机映射、性能预算、5 阶段实施顺序 | 规划 |
| ✔ 做 | `generated/types.generated.ts`：从 Rust 源码机械生成的 TS 类型（40 interface + 9 联合） | 机器产物，非手写 |
| ✔ 做 | `40-api-contract-ts.md`：端点表、字段含义、queryKey 规范、失效规则、联调核对清单 | 导读 |
| ✘ 不做 | 任何 `.tsx` / `.ts` 业务代码、任何组件、任何样式、任何前端脚手架 | **没写** |

前端动手时的唯一真相优先级（**冲突时按此顺序，不看文档措辞**）：

```
docs/openapi.json（机器生成）
  > generated/types.generated.ts（机器生成）
    > 40-api-contract-ts.md（人工导读）
      > 30-ts-frontend-plan.md（设计意图）
```

前端生成类型的正确命令（**注意：从本地文件生成，后端没有 `/api-docs` 端点**）：

```bash
pnpm openapi-typescript ../docs/openapi.json -o src/shared/api/types.gen.ts
```

---

## 6. 本轮自己抓出来的 7 个真缺陷（自证的诚实度）

如果一份交付宣称"一次就全过"，通常意味着**没测**。本轮机器化自检抓出并修复了 7 处（其中第 2–4 条同属 40 号文档 §2.3 的修订表）：

| # | 缺陷 | 若不修的后果 | 证据 |
|---|---|---|---|
| 1 | `gen-openapi.py` 用**函数名**索引 handler，`memory::list` 被 `task::list` 覆盖 | `GET /memories` 的响应会被写成 `TaskList`，**前端照此建模必崩** | 已改为 `(模块名, 函数名)` 作键 |
| 2 | 40 号文档 `POST /tasks` 响应写成 `TaskView` | 实际是 `TaskDetail`（含 trace + memory_refs），前端拿不到追踪数据 | 40 号 §2.3 修订表 |
| 3 | 40 号文档 `/tasks/{id}/trace` 写成 `{ trace: [...] }` | 实际是**裸数组** `TraceEntry[]`，前端解包会 `undefined` | 同上 |
| 4 | 40 号文档 `/onboarding/skip|reset` 写成 `StepChangeView`；`/auth/logout` 写成需 Bearer | 实际都是 `OnboardingView`；logout 免鉴权（凭 body 的 refresh_token） | 同上 |
| 5 | `openapi.json` 的 `servers.url` 写成 `:8080` | 源码默认 `127.0.0.1:8088`（`config.rs:50`），别人按文档联调连不上 | 已改 8088 并注明 `SA_BIND` |
| 6 | `60-review-checklist.md` 把 `Role` 标成 `domain/user.rs` | 真在 `security/rbac.rs:7`，审阅者按文档找不到 → 误判"文档造假" | C 节已纠正并留痕 |
| 7 | TS 类型混入内部装配结构 `AuthService`（含 `&'a Config`） | 前端类型里出现不可序列化结构，误导建模 | `SKIP_STRUCTS` 增补，输出 41 → 40 interface |

---

## 7. 我最不放心的三处（请优先挑这里）

诚实交代核查薄弱点，比宣称"全都对"更有用：

1. **`gen-openapi.py` 的类型描述等价性**：它把 `Option<String>` 写成 `{"$ref": "String"}` 之类还是 `{"type":"string","nullable":true}`，只保证"字段存在 + 可选性正确"，**不保证类型描述 100% 语义等价**。OpenAPI 3.1 已废弃 `nullable`，本文件用"不进 required"表达可选——这是**约定式**做法，严格来说不属于 JSON Schema 的 `null` 类型联合。若前端生成器对此敏感，需按 `openapi-typescript` 的实际行为核对一次。
2. **元组字段退化**：源码里 3 处 `Vec<(String, DateTime<Utc>)>` 之类的元组，在 schema 中退化为 `array<array>`。**前端不要依赖这三个字段的精确结构**（它们在审计/配额相关结构里）。
3. **50 号文档的断言分组数字**：由 `scripts/smoke.py` 里的 `Sxx` 标签机械统计得出（第四轮实测：S01–S09=10、S10–S25=16、S26–S37=12、S38–S51=19、S52–S58=7、S59–S71=24、S72–S77=8、S78–S83=18、S84–S86=3、S87–S92=6，合计 123），与脚本打印的“合计 123 项”同口径；**分组归类由我判断，断言文本未逐条人工复核**。

---

## 8. 冲突裁决顺序

若你核对后发现与我文档不一致，**按此顺序判定谁对**，不必跟我争论：

```
1. src/**/*.rs 源码
2. cargo test 实际输出
3. docs/evidence/*.txt 里脚本的原始输出
4. docs/openapi.json / generated/types.generated.ts（机器生成的产物）
5. 40 / 50 / 60 号文档里的人工描述
6. 我在对话里说过的话（最不可靠，可能是口误或过期）
```

发现冲突时，请直接指出"哪一行、哪个文件、期望什么、实际什么"。**凡是我说错的地方，我按源码改文档，不按文档改源码。**

---

## 9. 第二轮核查（模块化改造 + 逐个模块打磨，2026-11-09 深夜）

这一节是**在原交付之上继续做出来的**，不是同一批东西的重复描述。

### 9.1 本轮做完的三件事

| # | 做了什么 | 证据 |
|---|---|---|
| 1 | 把**审计出口**做成第 3 个真·可插拔部件（`trait AuditSink`，memory / file 两实现，业务层零引用） | `swap_drill.py` D4a–D4j；`check-modularity.py` 15 项（原 9 项，新增 6 项专盯审计） |
| 2 | 修掉**两个会让数据悄悄丢失**的真缺陷（详见 `71` 号文档 §6.2 / §6.3） | D4i（硬杀重启后台账仍在）、D4j（`SA_FLUSH_INTERVAL_MS` 真生效）、3 条新单测 |
| 3 | 概览接口主动暴露"当前用的是哪个部件"，运维不必猜 | `GET /api/v1/observability/overview` 新增 `audit_backend`、`runner_backend`；契约文档与 `openapi.json` 同步更新，`check-openapi.py` 0 处不一致 |

### 9.2 必跑门禁与当前真实数字

```powershell
cd "C:\Users\94355\Desktop\新建文件夹(7)"
python scripts\regress_all.py      # 一键跑 7 道门禁，本次：7/7 通过
```

| 门禁 | 本次结果 |
|---|---|
| 单元测试 `cargo test` | **80 passed / 0 failed**（上轮 55） |
| 冒烟 json 后端 / memory 后端 | **123 / 123** 两遍（上轮 98） |
| 契约一致性 / OpenAPI ⇄ TS | 0 处不一致 / 一致 |
| 执行端口实测 `verify_runner.py` | **15 / 15** |
| 换件演练 `swap_drill.py` | **22 / 22**（上轮 12） |
| 模块化静态守护 `check-modularity.py` | **15 / 15**（上轮 9） |

### 9.3 本轮**新**暴露的诚实边界（不修饰）

1. **Windows 无法捕获"硬杀"**：`TerminateProcess` 不给进程任何收尾机会。已把写后落盘窗口压到 500 ms（`SA_FLUSH_INTERVAL_MS`，可调到 50 ms），但**最多仍会丢最后一个窗口内的写入**。这是平台事实，不是实现缺陷。
2. **file 审计的启动回读只取尾部 8 MB**：历史台账超过 8 MB 时，进程重启后内存里只有最近一段（磁盘文件本身不丢，只是接口分页看不到更早的）。
3. **热插拔仍未做**：换件一律是"改环境变量 + 重启"。

### 9.4 与上文的版本差异（以本节为准）

上文 §1–§8 写于第一轮（单测 55 / 冒烟 98 / 换件 12 / 静态 9），这些数字**已被本节取代**。若两处冲突，以本节 + `docs/evidence/regress-all.raw.txt` 为准。

---

*本文件由 Marvis 生成于 2026-11-09，所有数字均来自当日实跑输出，原始记录在 `docs/evidence/` 目录下。*
