# 60 · 交付核对清单（给另一个 AI agent 的作业本）

> 用法：把本仓库交给任意具备文件读写能力的 AI agent，让它**不使用本对话上下文**、只读源码，逐条核对下面每一项，并明确回答"一致 / 不一致 / 无法确认"。
> 清单里的每个断言都能被证伪：要么给源码位置，要么给可执行命令。
> **凡是本清单没有覆盖的结论，都不要采信——包括我口头说过的。**

---

## A. 先做三件事，判断这份交付是不是"编出来的"

| # | 动作 | 期望 | 不符意味着什么 |
|---|---|---|---|
| A1 | `cargo test --lib`（见 `50-build-and-verify.md`） | 编译通过、**`80 passed; 0 failed`**（2026-11-09 第四轮实测；若你的数字 < 80，说明拿到的是旧包） | 编译不过 = 代码是拼凑的，直接打回 |
| A2 | 统计 `src/**/*.rs` | **37 个 `.rs`，6572 行**（2026-11-09 实测） | 行数远少于这个数 = 有大段是空壳/`todo!()` |
| A3 | 全仓库搜索 `todo!` / `unimplemented!` / `FIXME` / `TODO` | **实测 0 处**（37 个 `.rs` 全文匹配，2026-11-09） | 命中 = 存在未实现却被声称"已完成"的功能 |

再补一条最有效的证伪手段：**随便挑 3 个端点，去 `src/api/handlers/` 里找它的实现函数**。找不到就是假的。

---

## B. 本轮两项改动的逐条核对

> **验证状态（2026-11-09 实跑）**：本节两项改动均已在 `%LOCALAPPDATA%\sa-build` 编译通过，`cargo test --lib` 结果 `80 passed; 0 failed`。
> 其中 B1 的两个测试、B2 的一个测试，其 `... ok` 行已逐条抓取确认**真实执行过**（不是被 filter 掉）：
> ```
> test orchestration::executor::tests::custom_step_runner_replaces_backend_and_keeps_governance ... ok
> test orchestration::executor::tests::authz_denial_precedes_step_runner ... ok
> test security::ratelimit::tests::task_submit_quota_is_60_per_hour_per_user ... ok
> ```
> 注意：**单测通过 ≠ 端到端可用**。48 项冒烟本轮未重跑，见 E 节。

### B1. 执行后端抽象（原计划"抽 trait Executor"）

| 断言 | 源码位置 | 核对方法 |
|---|---|---|
| 存在 `StepRequest` / `StepOutcome` / `trait StepRunner` / `SimRunner` | `src/orchestration/executor.rs:60 / :70 / :119 / :128` | 直接读这四段 |
| `Executor` 持有可选后端字段，默认 `None` 时行为与改造前一致 | `src/orchestration/executor.rs:39`；`src/state.rs` 的 `exec()` 传 `runner: None` | 读 `state.rs` 里 `Executor { ... }` 构造 |
| 治理层（鉴权、预算、重试、熔断、状态收敛）**没有**被挪进后端 | `executor.rs` 的 `run()` 主体：鉴权失败直接返回、预算扣减在循环内、`max_failed_steps` 熔断在循环内 | 读 `run()`，确认这些判断不在 `SimRunner::run_step` 里 |
| 后端名会被打进指标 | `executor.rs` 中 `sa_executor_backend_total` | `grep -n sa_executor_backend_total src/orchestration/executor.rs` |
| 有两个测试证明"可替换"和"越权先于后端" | `executor.rs:1006`（`custom_step_runner_replaces_backend_and_keeps_governance`）、`:1046`（`authz_denial_precedes_step_runner`） | 读两段测试体，确认断言不是空断言（有 `assert_eq!(runner.calls(), 0)` 这种硬断言） |

**为什么叫 `StepRunner` 而不是 `Executor`**：`Executor` 这个名字已被治理外壳占用，两者同名会造成"到底替换哪个"的歧义。见 `README.md` 的命名说明。核对时请判断：**目标（后端可替换、治理层不动）是否达成**，而不是名字是否与最初计划字面一致。

### B2. 任务提交限流（60 次/小时/用户）

| 断言 | 源码位置 | 核对方法 |
|---|---|---|
| 限额常量是 60/3600 秒 | `src/api/handlers/task.rs:21`：`Quota::new(60.0, 60.0 / 3600.0)` | 读该行 |
| 限流发生在 `POST /api/v1/tasks` 的最前面（在参数校验、路由规划、预算之前） | `task.rs` `create()` 内，注释标 `// 0) 提交限流`，位置在 `user.require(Permission::Write)?` 之后、`goal` 解析之前 | 读 `create()` 前 20 行 |
| 限流按用户维度隔离（不是全局、不是按 IP） | key 为 `format!("task_submit:{}", user.id)` | 读该行 |
| 超限返回 429 且带 `Retry-After` 头 | `ApiError::TooManyRequests { message, retry_after }`（`src/error.rs` 已有实现，会把 `retry_after` 写进 `Retry-After`） | 读 `error.rs` 里 `TooManyRequests` 的 `into_response` |
| 有单测锁定"60 次通过、第 61 次被拒、等待 60 秒、他人不受影响" | `src/security/ratelimit.rs:97`（`task_submit_quota_is_60_per_hour_per_user`） | 读测试体 |
| 令牌桶算法本身未改 | `src/security/ratelimit.rs:45` `check()` | 读函数体，确认仍是"按时间回填 + 消耗 1" |

---

## C0. 先跑机器化自检（不需要人眼，也不需要我参与）

```powershell
python scripts\check-contract.py       # 文档 interface ↔ 源码 struct 字段比对，退出码 0 = 一致
python scripts\gen-ts-types.py         # 从源码生成前端类型，并打印无法识别的类型（WARN 应为空）
python scripts\gen-ts-types.py --check # 生成结果是否与磁盘上的一致（防止有人手改生成文件）
python scripts\gen-openapi.py          # 从路由表 + handler 签名生成 docs/openapi.json
python scripts\gen-openapi.py --check  # 契约文件是否与源码漂移（退出码 1 = 源码改了没重新生成）
```

`check-contract.py` 做的事：把 `docs/40-api-contract-ts.md` 里每个 `export interface` 的字段集合，与 `src/**/*.rs` 里同名 `pub struct` 的 `pub` 字段集合**逐个比对**，输出三类问题：

1. 文档有、源码无的字段（前端会照此写出不存在的字段）；
2. 源码有、文档缺的字段（前端会漏接数据）；
3. 文档内重复字段（复制粘贴留下的痕迹）。

`gen-ts-types.py` 做的事：**不读文档，直接读 Rust 源码**，把 `pub struct` / `pub enum` 翻成 TS。两份产物（人工文档 vs 机器生成）互相印证，任何一方漏字段都会在交叉比对里暴露。

### 本次实跑结果（2026-11-09）

```
源码结构体: 56 个 | 文档 interface: 21 个
结果: 一致 21 | 有差异 0 | 查无同名 0          ← check-contract.py

文档 21 个 interface | 生成 40 个 interface + 9 个联合类型（404 行）
可交叉验证 21 个 | 字段差异 0                   ← 两条独立路径互证
共有枚举 7 个 | 取值差异 0
生成器 WARN: 0 条（无"无法识别"的类型）

openapi.json: 24 路径 | 28 操作 | 50 schema | WARN 0   ← gen-openapi.py
CHECK: 一致 | paths=24 operations=28 schemas=50          ← gen-openapi.py --check
```

第三条证据链（**接口契约**，只在前两轮不存在）：

```json
// docs/openapi.json 头部（注意 description 里如实写了来源，不是 utoipa）
"openapi": "3.1.0",
"info": { "title": "personal super agents", "version": "0.1.0",
          "description": "由 scripts/gen-openapi.py 从 src/ 源码静态解析生成；非 utoipa 注解产物。" },
"servers": [{ "url": "http://127.0.0.1:8088" }]
```

### 这套自检**真的抓到过问题**，不是摆设

跑出来过三处真问题，都是脚本发现、不是"我觉得没问题"：

1. **命名不符**：文档写 `OnboardingStepView`，源码真名 `StepView`（`src/api/handlers/onboarding.rs:14`），字段一致仅名字不同 → 已改。
2. **文档凭空多出类型**：`OnboardingStatus` 在源码里**不存在**任何同名 enum，其值实际来自 `onboarding.rs:144` 的 `status() -> &'static str`。→ 已在 40 号文档 §0.1 明确标注为"前端自建类型，非后端类型"。
3. **生成器自身漏字段**：第一版生成器把 4 个字段（`MemoryEntry.confidence`、`PlanStep.permission`、`StepChangeView.changed`、`TaskView.model_profile`）漏掉了，原因是这些字段上方有 `///` 文档注释，解析正则没剥掉。这条最值得注意：**工具也会有漏，所以必须两条独立路径互证**，不能只信一条。

**脚本的能力边界（必须知道）**：`check-contract.py` 只比**字段名集合**，不比字段**类型**（`number` vs `string | null`）、不比枚举取值。
这两项由 `gen-ts-types.py` 从源码推导覆盖（`Option<T>` → `T | null`、`serde rename_all` → 枚举字面量），二者合起来才闭环。
仍然不比的部分：**运行时实际行为**（状态码、错误码、限流阈值）。这些要跑起来才知道，见 `docs/50-build-and-verify.md`。

---

## C. 契约文档（`40-api-contract-ts.md`）的自查方式

契约文档最容易飘。核对它只需以下事实源，**全部以源码为准**：

| 事实 | 权威来源 | 必须先核对的点 |
|---|---|---|
| 路由与 HTTP 方法 | `src/api/mod.rs`（`router()` 函数，`/api/v1` 前缀由 `.nest("/api/v1", v1)` 决定） | 文档里的端点是否**多写**了不存在的路径，或**漏写**了 `/healthz`、`/readyz`、`/metrics` 这三个根路径端点（它们不带 `/api/v1`） |
| 监听端口 | `src/config.rs:50`，默认 `127.0.0.1:8088` | 文档若写 8080，就是错的 |
| 任务状态枚举 | `src/domain/task.rs:81` 的 `TaskStatus` | 必须恰好 7 个：`queued` / `running` / `succeeded` / `partially_succeeded` / `failed` / `aborted_budget` / `cancelled`。出现 `aborted_error`、`aborted_interrupted` 之类即伪造 |
| 角色 | **`src/security/rbac.rs:7`** 的 `Role`（**不在 `domain/user.rs`**——旧版本这行写错了，已纠正） | 必须恰好 3 个：`owner` / `admin` / `member` |
| 用户状态 | `src/domain/user.rs:9` 的 `UserStatus` | 必须恰好 2 个：`active` / `disabled` |
| 权限 | `src/security/rbac.rs:43` 的 `Permission` | 必须恰好 7 个：`read` / `write` / `sandbox_exec` / `memory_write_shared` / `memory_write_system` / `cross_agent` / `admin` |
| 请求/响应字段 | 对应 `handlers` 里的 `#[derive(Deserialize)]` 输入体与 `domain` 里的响应结构 | 字段名必须是 `snake_case`（serde 未做 rename 的字段就是 snake_case）；文档里出现 `camelCase` 字段即与后端不符 |
| 错误码字符串 | `src/error.rs` 的 `code()` 方法 | 文档里列的 code 必须能在该方法里逐个找到 |

**判定口径**：只要 40 文档有一处与上表冲突，就以源码为准，并把冲突行号列出来——这类冲突说明文档是凭记忆写的，不是对照源码写的。

---

## D. 前端文档（`30-ts-frontend-plan.md`）的边界：哪些是事实，哪些只是建议

| 类别 | 内容 | 该怎么对待 |
|---|---|---|
| **源码事实（必须遵守）** | 端点、字段、枚举、状态机取值、错误码、鉴权头与刷新流程 | 写前端时按这个来；不符就是 bug |
| **工程建议（可以不采纳）** | 不选 Next.js / 不选 Redux / 目录分层 / queryKey 命名 / 阶段拆分 A1–A5 | 这只是我给的方案，老板有权推翻。**它不是契约**，别拿它当"后端要求" |
| **尚未存在的能力** | SSE 流式、OpenAPI 自动生成、`/tasks` 的异步提交（当前 `POST /tasks` 是同步执行完才返回） | 这些**代码里没有**。前端不能假定它能用，否则一联调就炸 |

**特别提醒写前端的 AI**：`POST /api/v1/tasks` 目前是**同步**的——请求会阻塞到任务执行结束。这是当前实现的真实行为，不是文档笔误。要么前端按同步处理（加大超时、做 loading 态），要么等后端改成异步提交。不要自己发明一个 `202 Accepted + 轮询` 的流程然后声称"文档里写的"。

---

## E. 如实列出的缺口与未验证项（不要当成已完成）

| 项 | 真实状态 |
|---|---|
| 80 项单测 | **已跑通**：`80 passed; 0 failed`，构建于 ASCII 副本（2026-11-09 第四轮）。命令与原始输出见 `10-verification-report.md` §11.2 |
| 123 项端到端冒烟 | **已入库并跑绿**：`scripts/smoke.py`，`合计 123 项 \| 通过 123 \| 失败 0`（json/memory 双后端各一遍），原始输出留档 `docs/evidence/regress-all.raw.txt`。一条命令即可复跑，不需要我参与 |
| `utoipa` / `openapi.json` | **`openapi.json` 已产出**（`docs/openapi.json`，OpenAPI 3.1，24 路径 / 28 操作 / 50 schema），但**不是 utoipa 生成**，而是 `scripts/gen-openapi.py` 静态解析源码（零侵入、0 新依赖）。**仍未有的**：Swagger UI、运行时请求校验。若审阅方坚持要 utoipa，需给 28 个操作补约 400 行注解 |
| 任务执行 | 仍是**模拟执行**（`SimRunner` 按规则合成结果），没有真实调用任何模型 |
| SSE 流式 | 未实现 |
| 越权返回码 | **已分开**：越权回 403、校验失败回 422（冒烟 S76 实测 `403` + `code=forbidden`）。仍不统一的是"他人资源"在不同端点回 403 或 404，见 §10.5 缺口 7 |
| 并发/持久化 | `Store::in_memory()` 是进程内内存存储 + JSON 快照落盘，不是数据库 |

### E1. 为什么用生成器替代 utoipa（供审阅方判断该不该推翻）

| 维度 | utoipa | 本项目的 `gen-ts-types.py` |
|---|---|---|
| 侵入性 | 每个 handler 加 `#[utoipa::path]`、每个结构体加 `ToSchema`，约 375 行注解 | 后端代码**零改动** |
| 会不会漂移 | 忘了写注解 → 契约不更新，且**看不出**它过期了 | 每次重新生成，自动跟随源码 |
| 额外产出 | Swagger UI、运行时请求校验 | 只有 TS 类型 |
| 新增依赖 | 3～4 个 crate | 0 |
| 适合阶段 | 端点稳定、需要对外交付 API 文档 | 端点仍在变动、只有自己一个前端 |

**结论不是"utoipa 不好"，而是"现在上它，维护成本高于收益"**。若后端端点冻结（比如准备开放给别人调用），再上 utoipa，两者可并存：生成器出 TS，utoipa 出 openapi.json。

> **更新（2026-11-09 本轮）**：为消除"前端类型对了、但接口路径/状态码/请求体仍靠人写"的漂移，本轮新增 `gen-openapi.py`，
> 用同一套静态解析思路把**路由表 + handler 签名 + 数据结构**一并导出为 `docs/openapi.json`。
> 因此现状是：**TS 类型（types.generated.ts）+ 接口契约（openapi.json）双机器生成**，人工文档 `40-api-contract-ts.md` 降级为"导读 + 设计意图说明"，
> 其正确性判据改为"与生成物一致"。这就是"报假"在机制上被堵住的地方——**两份生成物都由代码算出来，我改不了口。**

---

## F. 一句话交给对方 AI 的指令模板

```
仓库路径：<项目根目录>
任务：只读源码，按 docs/60-review-checklist.md 逐条核对，输出一张表：
      断言 | 一致/不一致/无法确认 | 证据（文件:行号 或 命令输出）。
      若发现文档与源码冲突，以源码为准并列出冲突项。
      不要修改任何文件，不要运行 cargo test（除非我允许）。
      最后用一句话回答：这份交付有没有"报假"。
```
