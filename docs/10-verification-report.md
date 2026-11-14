# 后端验收报告 — Personal Super Agents（Rust v1.0.0）

> 验证时间：2026-11-08
> 工具链：`stable-x86_64-pc-windows-gnu`（MSVC 侧缺 `link.exe`，GNU 侧可用）
> 构建方式：源码留在本仓库，复制到纯 ASCII 目录 + 独立 `CARGO_TARGET_DIR` 编译（MinGW `ld` 对中文路径报 `Invalid argument`）

## 1. 结论速览

> ⏱ **时效提示（2026-11-11 更新）**：本文是**历史快照**，按轮次记录当时的验证结论，数字会随迭代变化。
> 本节及 §2–§8 写于 2026-11-08（第一轮），其中的 **52 单测 / 48 项冒烟** 是当时的基线；§11 写于第二轮，记的是 **80 单测 / 123 项冒烟 / 7 道门禁**。
> **要看当前事实，请读桌面《Marvis-后端交接文档.md》与 `docs\evidence\regress-all.txt`**：截至 2026-11-11 为 **131 单测 / 156 项冒烟（双后端各一遍）/ 14 道门禁全绿**。
> 历史数字一律保留不删——审阅方需要看见演进痕迹，而不是一份被反复涂改的"永远正确"的报告。

| 项 | 结果 |
|---|---|
| `cargo build --bins` | 通过（增量 6.5–7.7s） |
| `cargo test` | **52 passed / 0 failed** |
| 端到端冒烟 | **48 项断言全绿（FAILS: none）** |
| 服务启动 | `readyz` 200；日志 `backend ready addr=127.0.0.1:18088 env=smoke` |
| 全链路 | 注册 → 登录 → 教程 6 步 → 跳过/重置 → 记忆读写改 → 任务创建/查轨迹 → 目录 → 可观测 → 令牌轮换 → 改密 → 登出，全程无 5xx |
| 遗留缺口 | 见 §6（3 项功能缺口 + 3 项契约/运维待补） |

## 2. 单元与集成测试

```
running 52 tests
test result: ok. 52 passed; 0 failed; 0 ignored
```

覆盖点（节选）：PBKDF2 哈希与恒定时间比较、JWT 签发/过期/类型错配、RBAC 权限矩阵、令牌桶限流、快照原子落盘与重启恢复、注册/登录/刷新/登出、教程状态机（幂等完成、必填收敛、跳过、重置）、任务分类与越界判定、预算降档与耗尽中止、重试上限与熔断、合规红线一票否决、只读 agent 不可写记忆、成员不可写 system 记忆、错误码映射与内部信息不泄漏。

## 3. 端到端冒烟（48 项）

方式：启动真实二进制 → HTTP 打满全部路由 → 断言状态码与关键字段。

| # | 用例 | 期望 | 实测 |
|---|---|---|---|
| 1–2 | `GET /healthz`、`GET /readyz` | 200 | 200 / 200 |
| 3 | 首用户注册 | 201 + `role=owner` | 201，`onboarding=not_started`，`next=welcome` |
| 4 | 重复注册 | 409 | 409 |
| 5 | 字段级校验（用户名/邮箱/密码均非法） | 422 + `details` | 422 |
| 6 | 错误密码登录 | 401 | 401 |
| 7 | 邮箱登录 | 200 | 200 |
| 8–9 | `GET /auth/me` 有/无令牌 | 200 / 401 | 200 / 401 |
| 10 | 教程总览 | 200，6 步、必填 5 | `required 0/5`，步骤顺序正确 |
| 11–12 | 完成 `welcome` + 重复完成 | 200 / `changed=false` | 幂等成立 |
| 13 | 非法步骤 `nope` | 422 | 422 |
| 14–18 | 完成 `profile/agents/memory/first_task/boundaries` | 200 ×5 | 全 200 |
| 19 | 全部完成后的总览 | `status=completed`，`next_step=null` | 一致 |
| 20–21 | 跳过 / 重置 | 200 / 200 | 200 / 200 |
| 22 | 写 shared 记忆 | 201 | 201 |
| 23 | 同 key 覆盖 | 200 + `version=2` + `created=false` | 一致 |
| 24 | owner 写 system 作用域 | 201 | 201 |
| 25 | 非法 scope | 422 | 422 |
| 26–27 | 记忆列表 / 更新 | 200 / 200 | total=2，更新成功 |
| 28 | 创建任务（显式 `coding` + `deep`） | 201 | `succeeded`，plan 4 步，trace 5 条，tokens=12804，memory_refs=1 |
| 29–31 | 任务详情 / 轨迹 / 列表 | 200 ×3 | 200，total=1 |
| 32 | 隐式路由任务 | 201 | 路由到 `planning`，`model_profile=fast` |
| 33 | 越界任务（股票预测/买入建议） | 422 | 422 `unprocessable_entity` |
| 34 | 预算过低（50 < 200） | 422 | 422 |
| 35 | 目标过短（`ab`） | 422 | 422 |
| 36–39 | agents 列表 / 详情 / capabilities / 未知能力 | 200/200/200/404 | 14 个 agent、26 项能力、404 命中 |
| 40–41 | 可观测 overview / audit | 200 ×2 | 200 |
| 42–44 | `/metrics` 无令牌 / 静态抓取令牌 / 管理员令牌 | 401 / 200 / 200 | 全符合 |
| 45–46 | 刷新令牌 / 旧令牌重放 | 200 / 401 | 401（轮换即作废） |
| 47 | 修改密码 | 200 | 200 |
| 48 | 登出（吊销会话） | 200 | 200 |

## 4. 本轮验证中发现并修复的两个真实缺陷

### 4.1 假性预算超支（估算即上限）

- **现象**：同一任务（`deep`，plan est 合计 13000）前次 `succeeded`（11884 tokens），后次 `aborted_budget`（13132 tokens）。
- **根因**：执行侧 token 抖动区间为 `0.85–1.15`，预算却直接取 `sum(est_tokens)`（系数 1.0）；叠加记忆注入后触到上沿即被误判超支。
- **修复**：
  1. `routing::build_plan` 预算改为 `ceil(sum(est) × 1.2)`，覆盖抖动上沿；
  2. 任务创建改为**先装配上下文、后算预算**：默认预算 = `bundle.budget_tokens + ctx.used_tokens`（记忆注入亦是真实成本）；显式 `budget_tokens` 仍是硬上限，超限 422，不静默截断。
- **回归**：修复后同路径稳定 `succeeded`；单测 `budget_exhaustion_aborts` 仍绿（真实耗尽照样中止）。

### 4.2 `/metrics` 无法被 Prometheus 长期抓取

- **现象**：仅接受管理员 access token，而 access token 30 分钟过期。
- **修复**：新增 `SA_METRICS_TOKEN`（≥24 字符，留空不启用）+ `MetricsAuth` 提取器：静态令牌常量时间比对优先，未命中回退「管理员 access token」，双通道皆不通才 401。三态均已验证。

### 4.3 顺带补齐：任务视图缺「实际生效模型档位」

- **问题**：前端无法展示「按任务模型调度」结果，也无法核对路由是否正确。
- **修复**：`TaskView.model_profile` = 显式请求值，否则取计划首步档位（兜底 `standard`）。实测隐式任务返回 `fast`。

## 5. 环境变量清单（无任何硬编码密钥）

| 变量 | 默认 | 说明 |
|---|---|---|
| `SA_BIND` | `127.0.0.1:8088` | 监听地址 |
| `SA_DATA_DIR` | `data` | 数据目录（JSON 快照落盘位置） |
| `SA_JWT_SECRET` | 自动生成到 `data/secret.key` | ≥32 字符；未设置则生成并告警 |
| `SA_ACCESS_TTL_SECS` | `1800` | access token 有效期（30 min） |
| `SA_REFRESH_TTL_SECS` | `604800` | refresh token 有效期（7 d） |
| `SA_PBKDF2_ITERATIONS` | `120000` | 密码哈希迭代次数（测试用 1000 加速） |
| `SA_SNAPSHOT_INTERVAL_SECS` | `15` | 全量快照兜底间隔（**真正的落盘靠 `SA_FLUSH_INTERVAL_MS` 写穿，快照只兜底**） |
| `SA_FLUSH_INTERVAL_MS` | `500` | 脏数据写穿窗口（50~60000 ms）。**进程被硬杀时最多丢这个窗口内的写入** |
| `SA_MAX_BODY_BYTES` | `262144` | 请求体上限（256 KB） |
| `SA_CORS_ORIGINS` | 空（`*`） | 逗号分隔白名单；生产需收紧 |
| `SA_ALLOW_REGISTRATION` | `true` | 是否开放注册 |
| `SA_TASK_RETRY_LIMIT` | `2` | 单步重试上限 |
| `SA_TASK_BUDGET_TOKENS` | `60000` | 单任务预算基准（硬上限为其 4 倍） |
| `SA_METRICS_TOKEN` | 空（不启用） | Prometheus 抓取静态令牌（≥24 字符） |
| `SA_ENV` | `dev` | 环境标识（出现在 overview 与日志） |
| `SA_STORAGE` | `json` | **存储部件**：`json`/`file`/`snapshot` 或 `memory`/`mem`/`ephemeral`；非法值拒启 |
| `SA_RUNNER` | `sim` | **执行部件**：`sim` 或 `process`；`process` 必填 `SA_RUNNER_DIR`，否则拒启 |
| `SA_RUNNER_DIR` | 空 | `process` 后端的脚本目录（每 agent 一个 `<agent-id>.cmd`） |
| `SA_AUDIT` | `memory` | **审计部件**：`memory`/`mem` 或 `file`/`jsonl`/`disk`/`durable`；非法值拒启 |
| `SA_AUDIT_FILE` | 空（`file` 模式必填） | 审计 JSONL 路径；指向目录会拒启（不假装成功） |

## 6. 已知缺口（如实记录，不掩盖）

| 类型 | 缺口 | 影响 | 建议 |
|---|---|---|---|
| 功能 | 任务提交未做限流（计划中的 60 次/h 未落地），仅注册 10/h、登录 10/min、刷新 30/min | 单用户可高频打满执行器 | Phase-2 加 `Quota::new(60.0, 60.0/3600.0)` 按 user 建桶 |
| 功能 | 执行器为**确定性模拟**（不接真实 LLM/MCP），token 与延迟为估算模型 | 真实成本/时延尚不可测 | Phase-2 接 Rust 热路径 + Python 编排（见 `20-rust-python-architecture.md`） |
| 功能 | 无 SSE/WebSocket 流式通道，任务创建是同步返回 | 前端只能轮询 trace | 后端新增 `GET /api/v1/tasks/{id}/events`（契约草案见 `40-ts-frontend-plan.md` §6） |
| 契约 | 越界拒绝与字段校验共用 `unprocessable_entity` 码 | 前端需按 `message` 区分 | Phase-2 细分 `out_of_scope` / `validation_failed` |
| 运维 | 存储为内存主存 + JSON 快照，单实例 | 无多副本一致性；重启仅恢复快照点 | 量产换 SQLite/Postgres，接口层已抽象为 `Store` trait |
| 契约 | 未提供 OpenAPI 文件 | 前端类型需手写或脚本生成 | Phase-2 引入 `utoipa` 自动导出 `openapi.json` |

## 7. 复现命令

```powershell
# 1) 同步源码到 ASCII 构建目录并编译
$src  = "C:\...\新建文件夹(7)"
$bld  = "C:\Users\94355\AppData\Roaming\Tencent\Marvis\User\oAN1i2ZQWL4Jhqy6CFXV3VsjUFjY\build_sa1"
robocopy $src\src $bld\src /MIR
$env:CARGO_TARGET_DIR = "$bld\target"
cargo +stable-x86_64-pc-windows-gnu build --bins

# 2) 跑单测
cargo +stable-x86_64-pc-windows-gnu test

# 3) 起服务（最小环境）
$env:SA_DATA_DIR="$bld\data"; $env:SA_BIND="127.0.0.1:8088"
$env:SA_JWT_SECRET="dev-secret-with-enough-entropy-0123456789"
$env:SA_METRICS_TOKEN="dev-metrics-token-0123456789"
& "$bld\target\debug\super-agents.exe"

# 4) 冒烟
python temp\smoke_runtime.py
```

## 8. 验收口径对照

| 验收标准（docs/00） | 现状 |
|---|---|
| 可直接运行 | ✅ 单二进制，环境变量注入即启动 |
| 可观测 | ✅ metrics（三态鉴权）/ audit / overview / 结构化日志，请求 ID 贯穿 |
| 可控 | ✅ 逐步鉴权、预算降档与硬上限、重试与熔断、合规红线、限流 |
| 不越权 | ✅ 跨用户 403 + 审计；`system` 记忆仅管理员可写；只读 agent 不可写 |
| 注册/登录/新手教程 | ✅ 全链路含幂等、跳过、重置 |
| 26 项能力五要素 | ✅ `GET /capabilities` 全量可查 |

---

## 9. 2026-11-09 变更与二次核查（本节晚于上方所有结论，冲突时以本节为准）

### 9.1 代码变更

| # | 变更 | 落点 | 新增测试 |
|---|---|---|---|
| 1 | 抽出单步执行后端 `trait StepRunner`（`StepRequest` / `StepOutcome` / `SimRunner`），`Executor` 通过 `runner: Option<&dyn StepRunner>` 注入，收益是"换执行后端不必改治理层" | `src/orchestration/executor.rs:39/60/70/119/128/561` | `custom_step_runner_replaces_backend_and_keeps_governance`（:1006）、`authz_denial_precedes_step_runner`（:1046） |
| 2 | 补任务提交限流：每用户每小时 60 次，超限 429 + `Retry-After` | `src/api/handlers/task.rs:21`（常量）、`:83`（`create()` 内第 0 步） | `task_submit_quota_is_60_per_hour_per_user`（`src/security/ratelimit.rs:97`） |

因此第 6 节缺口清单中的"任务提交无限流"一条，状态改为：**已补，且已通过单测验证（见 §9.2）**。其余缺口未动。

### 9.2 验证状态：已跑通（2026-11-09 第二次执行，覆盖下方的"受阻"记录）

上一版本记录"被智能应用控制拦截、未能运行"。**该阻断现已解除**（`VerifiedAndReputablePolicyState` 由 1 变为 0），并按 `docs/50-build-and-verify.md` 的流程完整跑通：

- 构建位置：`%LOCALAPPDATA%\sa-build`（纯 ASCII 副本，规避中文路径 `ld` 失败）
- 编译耗时：33.71s（`cargo +stable-x86_64-pc-windows-gnu test --lib`）
- **实际结果：55 passed; 0 failed; 0 ignored**（与期望值完全一致，52 + 新增 3）

新增 3 项测试确认真实执行（原始输出行）：

```
test orchestration::executor::tests::authz_denial_precedes_step_runner ... ok
test orchestration::executor::tests::custom_step_runner_replaces_backend_and_keeps_governance ... ok
test security::ratelimit::tests::task_submit_quota_is_60_per_hour_per_user ... ok
test result: ok. 55 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.10s
```

说明：本次只跑了**单元测试**这一层。改造前那轮做过 48 项端到端冒烟（拉起 exe 打真实 HTTP），本次**没有重跑冒烟**，属未覆盖项，已在 §9.4 标注。

### 9.2.1 前端类型改为机器生成（替代 utoipa 的方案）

原计划引入 `utoipa` 导出 `openapi.json`。评估后**本轮没有引入 utoipa**，原因是：给 25 个端点逐条写 `#[utoipa::path]` 注解约 375 行，且每轮改注解都要重新编译，在端点仍会变动的阶段维护成本高于收益；一旦漏写注解，产生的是"看起来有、实际过期"的契约，比没有更危险。

改为做**零侵入的源码 → TS 类型生成器** `scripts/gen-ts-types.py`：

- 输入：`src/domain`、`src/api/handlers`、`src/security`、`src/services`、`src/obs` 的 `pub struct` / `pub enum`
- 输出：`generated/types.generated.ts`（最终 40 interface + 9 联合类型，404 行）
- 特性：解析 `serde rename_all` 生成枚举字面量、`Option<T>` → `T | null`、`skip_serializing_if` → 可选字段；无法识别的类型退化为 `unknown` 并打 `WARN`，**不猜**
- 交叉验证：与人工核对的 40 号文档相比，**21 个 interface 字段差异 0，7 个共有枚举取值差异 0**

两者取舍写在 `docs/60-review-checklist.md` E 节。若后续要 Swagger UI 或运行时请求校验，再上 utoipa，生成器可与之并存。

### 9.3 文档二次核查（机器化）

新增 `scripts/check-contract.py`，把 40 号契约文档的每个 interface 与源码 `pub struct` 字段集合比对：

```
源码结构体: 56 个 | 文档 interface: 21 个
结果: 一致 21 | 有差异 0 | 查无同名 0
```

**核对中发现并修正 2 处**：
1. `OnboardingStepView` → 源码真名 `StepView`（`onboarding.rs:14`）。字段本身正确，仅命名不符，已改并复跑至 0 差异。
2. `OnboardingStatus` → 源码中**不存在**该 enum，其值实际由 `onboarding.rs:144` 的 `status() -> &'static str` 决定。已在 40 号文档 §0.1 显式标注为"前端自建类型"，避免审阅方误以为后端有此类型。

另补入文档原先漏列的 `AgentKind = 'super' | 'sub'`（`domain/agent.rs`，serde lowercase）。

### 9.4 本节不改变的事实

- 任务执行仍是模拟（`SimRunner` 合成结果），未接入任何真实模型；
- **`utoipa` / `openapi.json` 仍未做**（改用源码生成器，见 §9.2.1）；
- SSE 未实现，前端拿不到实时进度推送；
- 存储仍是内存 + JSON 快照；
- **端到端冒烟（48 项）本轮未重跑**，仅单测层面验证；
- 限流只覆盖注册/登录/刷新 + 任务提交（60 次/小时/用户），其余端点无限流。


---

## 10. 2026-11-09 第三轮：三个待办项结项 + 98 项冒烟全绿（**与上文冲突时，以本节为准**）

### 10.1 三个待办项：全部结项（含"没照原计划做"的那一项）

| # | 原待办 | 状态 | 落点（可点开核对） | 机器判据 |
|---|---|---|---|---|
| 1 | 抽 `trait Executor` 抽象执行后端 | **已完成**（名字改为 `trait StepRunner`，理由见 README 末节） | `src/orchestration/executor.rs`：`StepRequest` / `StepOutcome` / `trait StepRunner` / `SimRunner`；`Executor.runner: Option<&dyn StepRunner>` | 单测 `custom_step_runner_replaces_backend_and_keeps_governance`、`authz_denial_precedes_step_runner` |
| 2 | 任务提交限流 60 次/小时/用户 | **已完成并端到端实测** | `src/api/handlers/task.rs`：`TASK_SUBMIT_QUOTA` + `create()` 第 0 步调 `limiter.check`；`src/security/ratelimit.rs` | 单测 `task_submit_quota_is_60_per_hour_per_user` + 冒烟 S87–S92（第 56 次后命中 429，`Retry-After=59`） |
| 3 | 引入 `utoipa` 导出 `openapi.json` | **目标已达成，未用 utoipa**（见 10.2 与 `60-review-checklist.md` §E1） | `scripts/gen-openapi.py` → `docs/openapi.json`（OpenAPI **3.1**，24 路径 / 28 操作 / 50 schema） | `python scripts/gen-openapi.py --check` → `CHECK: 一致` |

> 第 3 项必须说清楚：**`docs/openapi.json` 里没有一行是手写的**，全部由脚本解析 `src/api/mod.rs` 的路由 + `src/api/handlers/*.rs` 的签名 + `src/**/*.rs` 的结构体生成。
> 它不是 utoipa 产物，也不假装是；文件头 `info.description` 里就写明了这一点，审阅方一眼可见。

### 10.2 本轮全部实跑记录（原始输出全部留档）

| 命令 | 结果 | 耗时 | 证据文件 |
|---|---|---|---|
| `cargo build --bin super-agents` | 通过（增量） | 5.2 s | — |
| `cargo test --all` | **55 passed / 0 failed** | 4.5 s | — |
| `python scripts/smoke.py` | **98 项 / 通过 98 / 失败 0** | ~2 min | `docs/evidence/smoke-latest.txt` |
| `python scripts/gen-openapi.py` | 24 路径 / 28 操作 / 50 schema，**WARN 0** | <1 s | `docs/evidence/gen-openapi-check.txt` |
| `python scripts/gen-ts-types.py` | 40 interface + 9 联合类型（404 行） | <1 s | `docs/evidence/gen-ts-latest.txt` |
| `python scripts/check-contract.py` | 源码结构体 56 个 / 文档 interface 21 个 / **差异 0** | <1 s | `docs/evidence/check-contract-latest.txt` |

构建环境与第一轮一致：`stable-x86_64-pc-windows-gnu`，ASCII 副本 `%LOCALAPPDATA%\sa-build`，`CARGO_TARGET_DIR` 独立。

### 10.3 自检抓到的 7 个真实缺陷（"全绿"之前先出错，这才是可信的样子）

本轮不是"一次就对"。机器化自检抓出 7 处问题，全部已修并留痕（与 `70-audit-handoff.md` §6 同一份清单）：

| # | 缺陷 | 后果（如果没抓住） | 修复 |
|---|---|---|---|
| 1 | `gen-openapi.py` 用**函数名**索引 handler，`memory::list` 被 `task::list` 覆盖 | `GET /memories` 的响应会被写成 `TaskList`，**前端照此建模必崩** | 索引键改为 `(模块名, 函数名)` |
| 2 | 40 号文档 `POST /tasks` 响应写成 `TaskView` | 实际是 `TaskDetail`，前端拿不到 trace / memory_refs | 40 号 §2.3 修订表 |
| 3 | 40 号文档 `/tasks/{id}/trace` 写成 `{ trace: [...] }` | 实际是**裸数组** `TraceEntry[]`，前端解包得 `undefined` | 同上 |
| 4 | 40 号文档 `onboarding/skip|reset` 写成 `StepChangeView`、`/auth/logout` 写成需 Bearer | 实际都是 `OnboardingView`；logout 免鉴权（凭 body 的 `refresh_token`） | 同上 |
| 5 | `openapi.json` 的 `servers.url` 写成 `:8080`，源码默认是 `127.0.0.1:8088`（`src/config.rs:50`） | 别人按文档联调连不上 | 已改为 8088 并在 description 注明 `SA_BIND` 可覆盖 |
| 6 | `60-review-checklist.md` 把 `Role` 标成 `domain/user.rs` | 真在 `security/rbac.rs:7`，审阅者找不到会误判"文档造假" | C 节已纠正并留痕 |
| 7 | `generated/types.generated.ts` 混入内部装配结构 `AuthService`（含 `&'a Config`） | 前端类型里出现不可序列化的结构，误导 | 生成器 `SKIP_STRUCTS` 增补 `AuthService` / `Config`，输出 41 → 40 interface |

> 一个诚实信号：**"一次全过"往往是没测**。上面 7 条都能在文档里找到"错 → 对"的前后对照。

### 10.4 对 §9.4 的覆盖（这几条已经变了）

| §9.4 旧表述 | 现状态 |
|---|---|
| `utoipa` / `openapi.json` 仍未做 | **`docs/openapi.json` 已有**（源码解析生成，非 utoipa），`--check` 可防漂移 |
| 端到端冒烟（48 项）本轮未重跑 | **已重跑并扩展到 98 项，全绿**，脚本入库（`scripts/smoke.py`），输出留档 |
| 限流只覆盖注册/登录/刷新 + 任务提交 | 未变（仍只有这 4 类），目录与 observability 端点无限流 |
| 任务执行仍是模拟 | 未变（`SimRunner`），`trait StepRunner` 已就位，接真执行只替实现不动治理层 |
| SSE 未实现 | 未变，前端实时进度仍无推送通道 |
| 存储仍是内存 + JSON 快照 | 未变 |

### 10.5 当前真实缺口（刷新后的完整清单，不掩盖）

| # | 缺口 | 影响 | 建议优先级 |
|---|---|---|---|
| 1 | 任务执行是确定性模拟（合成 tokens/结果），未接真实模型 | 功能真实但产出是假的 | P0（接 Python 编排时一并解决） |
| 2 | 无 SSE / WebSocket，任务进度只能靠轮询 `GET /tasks/{id}` | 前端体验，不阻塞 | P1 |
| 3 | `sa_http_requests_total` 在 `obs/metrics.rs::inc_http` 里定义但**无生产调用点**，`/metrics` 中不出现 | 观测缺口，冒烟只打 INFO 不打 PASS | P1 |
| 4 | 无 Swagger UI / 无运行时请求校验（有 `openapi.json` 静态文件） | 对外交付 API 文档时需补 | P2 |
| 5 | 存储为内存 + JSON 快照，非事务性数据库 | 多实例 / 崩溃恢复不可靠 | P2 |
| 6 | `GET /agents`、`GET /capabilities` **完全免鉴权**（静态目录，不含用户数据） | 若将来目录里加入用户私有数据，会变成泄露 | 设计确认项 |
| 7 | 越权返回 403、校验失败返回 422，两者错误码已分开；但"资源不存在"与"无权访问他人资源"在不同端点返回 404 / 403 **不统一** | 前端错误分支要按端点区分 | P2 |
| 8 | 限流只覆盖 4 类端点，其余可被刷 | 滥用防护不完整 | P2 |

---

## 11. 2026-11-09 第四轮：模块化改造 + 逐模块打磨（**与上文冲突时，以本节为准**）

本节回答老板的四个问题，逐条给证据；不含"我觉得"。

### 11.1 功能是否全部已做：按清单核，不按印象核

| 功能域 | 端点/能力 | 状态 | 本轮有无改动 |
|---|---|---|---|
| 认证 | 注册 / 登录 / 刷新 / 登出 / me / 改密 | 已做且实测 | 无（回归守护） |
| 新手引导 | 状态查询 / 标记完成 / 跳过 | 已做且实测 | 无 |
| 任务 | 提交 / 列表 / 详情 / 取消 / 重试 | 已做且实测 | 无 |
| 记忆 | 创建 / 列表 / 详情 / 更新 / 删除 / 沉淀 | 已做且实测 | 无 |
| Agent 目录 | 列表 / 详情 | 已做且实测 | 无 |
| 可观测 | healthz / readyz / metrics / overview / audit | 已做且实测 | **overview 新增两个部件身份字段**；audit 支持落盘 + 重启回读 |
| 治理链路 | 鉴权 → 预算 → 限流 → 重试 → 熔断 → 审计 → 指标 → 飞轮 | 已做且实测 | 沙箱记账归属修正（见 71 号 §7.1） |
| 契约产物 | openapi.json / TS 类型 / 一致性脚本 | 已做且可复跑 | 随 overview 变更同步重新生成 |

### 11.2 功能是否正确、是否好用、是否真正能用

| 问题 | 判定方式 | 结论 |
|---|---|---|
| 正确吗 | 80 条单测 + 123 条端到端断言（双后端） + 契约逐字段比对 | 是；本轮修的 2 个缺陷都是"正确性在边界条件下不成立"，已修并被断言钉住 |
| 好用吗 | 运维侧：`overview` 直接报当前部件；`/metrics` 可被 Prometheus 长期抓；审计可分页/按决策过滤；错误码统一（400/401/403/404/409/422/429/500） | 是（相对上一版：少猜、少试错） |
| 真能用吗 | 拉起**真 exe**，打**真 HTTP**，走**真文件落盘**，并**硬杀进程**看数据是否还在 | 是；但"零丢失"仍受 Windows 硬杀语义限制，见 71 号 §9 第 3 条 |

### 11.3 模块化：换一个部件不殃及全部

已成立 3 个可插拔部件（存储 / 执行 / 审计），22 项行为断言 + 15 项静态断言守着。详见 `docs/71-modularity-and-upgrade.md`。

### 11.4 本轮全部实跑记录

| 门禁 | 结果 |
|---|---|
| `cargo test` | **80 passed / 0 failed** |
| `smoke.py`（json 后端） | **123 / 123** |
| `smoke.py`（memory 后端） | **123 / 123** |
| `check-openapi.py` / `gen-ts-types.py --check` | 0 处不一致 / 一致 |
| `verify_runner.py` | **15 / 15** |
| `swap_drill.py` | **22 / 22** |
| `check-modularity.py` | **15 / 15** |
| `regress_all.py` | **7 道门禁 7/7 全绿** |

### 11.5 本轮抓到的两个真缺陷（不报"一次全过"）

| # | 缺陷 | 危害 | 修法 |
|---|---|---|---|
| 1 | 数据只靠 15 秒全量快照 + 只监听 Ctrl+C | 容器/关机/硬杀时，最后 ≤15 秒写入全丢（实测：重启后账号 401） | 500 ms 写穿 + 四种退出信号；可调 `SA_FLUSH_INTERVAL_MS` |
| 2 | `SA_AUDIT=file` 只写不回读 | 文件好看，接口重启后查不到任何历史（安全排查恰好在此刻失效） | 启动回读尾部 8 MB、坏行跳过、按容量截断 |

### 11.6 本节不改变的事实

前端代码仍为零（TS 由老板自写）；Python worker 未实现；无数据库；任务仍是同步执行、无 SSE 进度；热插拔未做。

---

## 12. 2026-11-11 第五轮：六部分文档补齐 + 三道新门禁（**与上文冲突时，以本节为准**）

### 12.1 本轮的起点

老板给了两句话：一是「别搞大段过程给我看，只看结果」（这条直接决定了本节写法：只列结论与证据），
二是要求「先做最后一轮优化升级，然后我要重新换个对话」。
第三份输入是**最初那条提示词的截图**——它才是验收的原始依据，一切以它为准，不以我们的印象为准。

### 12.2 做了什么

| 项 | 内容 | 证据 |
|---|---|---|
| 六部分文档补齐 | 新增 `01-overview`（总体定位）、`04-interaction-spec`（交互规范）、`05-boundary-handling`（边界处理）、`06-acceptance-criteria`（验收标准） | `docs/01`~`docs/06` |
| 防文档漂移 | `02`（能力清单）/`03`（分工协作）/`07`（映射）改为**由源码生成**，不再手写 | `scripts/gen-spec-docs.py` |
| OpenAPI 错误码 | 按每个操作**真实会返回**的路径挂 400/401/403/404/409/422/429，健康检查不挂 429（它不鉴权） | `scripts/gen-openapi.py` |
| 需求覆盖可判定 | 把首提示词拆成术语清单，逐条去六部分文档里找落点；找不到就红 | `scripts/check-prompt-coverage.py`（门禁 12） |
| 二进制新鲜度 | 见 12.3 第 1 条 | `scripts/build-exe.py`（门禁 0） |
| 防幻觉打点实测 | 见 12.3 第 2 条 | `scripts/probe_hallucination.py`（门禁 13） |

### 12.3 本轮抓到的三个真缺陷（没有"一次全过"）

**① 假绿：12 道门禁全绿，测的却是上一版程序。**
现象：改完 `src/orchestration/executor.rs`（20:33）跑全量回归，单测、冒烟、矩阵全绿；
但 `super-agents.exe` 的 mtime 停在 18:48。
根因：`cargo test` 产出的是测试可执行文件，**它不更新 `super-agents.exe`**；
而冒烟/矩阵/换件这些门禁都是 `--exe` 起进程的，所以它们跑的是旧程序。
处理：新增门禁 0 `build-exe.py`，真编译 + 校验 exe mtime ≥ 最新 `.rs`，落后即红并**中止后续门禁**
（不中止的话后面的绿毫无意义）。负向验证：`--skip-build` 时确实报
`二进制落后于源码：exe=18:48:46 最新源码=20:33:38`。

**② 假绿：C25 的指标"规格写了、代码没打点"。**
现象：能力规格 `anti_hallucination` 声明了 `sa_hallucination_flags_total`，
`/metrics` 里根本没有这一行（`sa_context_truncations_total` 同样缺失）。
中途还误判了一次：修好门禁 0 后复测仍缺失，一度以为是打点代码没生效；
实际原因是探针只写了 3 条长记忆，而 `assemble` 只 `take(5)`、预算 6000 tokens，
3 条 × ~1800 tokens 根本压不到裁剪线。**注入量不足，不是没实现。**
处理：把注入补到 5 条（填满候选位），复测 `sa_hallucination_flags_total=1.0`、
`sa_context_truncations_total=1.0`，H0~H3 共 13 项全过；并固化为门禁 13。
教训写在脚本注释里：**"没观测到"要先排除"没造出场景"，再怀疑代码。**

**③ 交接文档路径漂移。**
现象：文档写 `%LOCALAPPDATA%\sa-build\target\debug\super-agents.exe`，
实际生效的是 `C:\Users\94355\Desktop\build_sa1\target-gnu\debug\super-agents.exe`；
旧镜像其实还在磁盘上，exe 停在 17:34。
后果：接手人照文档抄命令，会**测到 17:34 的程序却全绿**——和第 ① 条是同一种病。
处理：交接文档全文改为实际路径并标注旧镜像已弃用；
`regress_all.py` 启动打印 `[TARGET] 源码/镜像/被测exe`，若发现另一套镜像存在，
额外打印「另有镜像 … 本次**不用它**，别照旧路径测出假绿」。

### 12.4 本轮实跑记录（原始输出在 `docs/evidence/`）

```
合计 14 道门禁 | 通过 14 | 失败 0
0 编译与二进制新鲜度   exe=20:42:54 ≥ 最新源码 20:33:38
1 单元测试             131 passed / 0 failed
2 冒烟 json            156 项 / 156 通过
3 冒烟 memory          156 项 / 156 通过
4~7 契约与类型         一致 / 未漂移（27 路径 31 操作 39 schema）
8 执行端口实测         15 项 / 15 通过
9 可插拔部件矩阵       86 项 / 86 通过
10 换件演练+静态守护   15 项 + 22 项 / 全通过
11 审计启动自检        15 项 / 15 通过
12 需求覆盖对齐        六部分 9/9~3/3、能力 26/26、公理 6/6
13 防幻觉打点实测      13 项 / 13 通过（flags=1、truncations=1）
```

### 12.5 本节不改变的事实

- worker 仍是**确定性规则实现，不是 LLM**（见交接文档 6.2）；接真模型需要老板提供凭据，不猜不编。
- 存储仍是 JSON 快照，不是数据库（6.3）。
- 前端仍未写，TS 类型与 OpenAPI 已可交付（老板自写）。
- "全绿"只证明**声明的都实测过**，不等于"业务上已经好用到能上线"——那需要老板用真实任务压一遍。
