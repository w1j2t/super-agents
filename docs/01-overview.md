# 01 总体定位

> 六部分之一（对应首个需求：**总体定位**）。本文件回答三件事：这个系统为什么存在、它管到哪、一次请求到底走了哪几步。
> 事实部分均可追溯到源码；出现数字的地方都标了来源文件。

## 1. 一句话定位

**个人超级 agents 后端**：一个只能用 HTTP 调用的 Rust 服务。你给它一句目标（`POST /api/v1/tasks`），它自己判定该由谁做、拆成几步、逐步执行、逐步校验，最后交回带置信度的结果与完整轨迹。

它不是"聊天接口 + 提示词模板"，而是把**编排、治理、审计、可观测**四件事做成代码里的硬约束：
能干什么是能力矩阵写死的（26 项），谁负责是花名册写死的（14 个 agent），
越权是做不了的（RBAC 七项原子权限），超预算是要失败的（`aborted_budget`）。

## 2. 六条设计公理

这六条是首轮需求里的原话要求，也是后续所有取舍的最高裁判标准。任何改动如果破坏其中一条，即使"功能更多"也不能合入。

| # | 公理 | 代码落点 | 现场证据 |
| --- | --- | --- | --- |
| A1 | 全自动化：用户只给目标，不问中间细节 | `super-agent` 唯一入口 + `routing.rs` 自动选人 + `executor.rs` 自动拆步执行 | `POST /api/v1/tasks` 一次调用拿到终态任务与 trace |
| A2 | 懒加载：不命中的能力不装上下文 | `AssembledContext` 只装配命中 agent / skill / 文档 / 记忆 | 能力 `lazy_loading`（见 `docs/02`）与 `sa_context_tokens_saved` 类信号 |
| A3 | 模块化：换一件不殃及全部 | 五槽位（`src/ports.rs`）+ 依赖注入（`src/state.rs`） | `scripts/matrix.py` 逐案换件、`scripts/swap_drill.py` 换件演练 |
| A4 | 可观测：每一步都能被外部看见 | `obs/` 指标 + `audit` 审计 + `EventBus` SSE + `trace` | `/metrics`、`/api/v1/tasks/{id}/trace`、`/api/v1/tasks/{id}/events` |
| A5 | 安全边界：越权与红线不做"下不为例" | `security/rbac.rs` + `executor.rs` 的 `REDLINES` | 命中红线即阻断，不降级放行（`docs/05`） |
| A6 | 亲自实测：不接受"应该能跑" | `scripts/regress_all.py` 24 道门禁 | 一条命令出结论表，退出码即结论 |

## 2.1 八个"化"的落点（原文第五段逐条对应）

首个需求原话要求整体满足"**全自动化、安全化、正确化、全可视化、可控化、全面化、自进化与规划**"。
这一句不是形容词，八项各有可指认的机制与可执行的证据；缺哪一项都能被 `scripts/check-prompt-coverage.py` 当场点名。

| # | 要求 | 机制落点 | 证据（怎么验） |
| --- | --- | --- | --- |
| 1 | 全自动化 | 只给目标：`POST /api/v1/tasks` 一次调用走完选人→拆步→执行→收敛（公理 A1） | 冒烟 S 系列：同步返回终态 + 完整 `trace` |
| 2 | 安全化 | 七项原子权限 + 三档权限组（`security/rbac.rs`）、7 条红线（`executor.rs`）、限流/体积/配额三闸门 | 单测：越权 3 次终止、红线命中即阻断；`docs/05` |
| 3 | 正确化 | 参数级校验（`api/jsons.rs`）+ 错误码按操作真实挂载 + `agreement_score` 结算 + `guard-agent` 复核步 | 冒烟 S95（字段级 422）、门禁 4/6 schema 与源码逐个对齐 |
| 4 | 全可视化 | 指标 `/metrics`、审计 `/observability/audit`、轨迹 `/tasks/{id}/trace`、进度 `/tasks/{id}/events`、部件 `/observability/components`、概览 `/observability/overview` | 门禁 11 审计自检（真起进程写一条再读回） |
| 5 | 可控化 | 预算 60,000 tokens、降档上限 2 次、熔断 `3/3`、`cancel` 接口、提交配额 60 次/h、"不可配置清单"5 项不给开关 | 单测：预算耗尽 → `aborted_budget`；取消 → `cancelled` 且不计失败 |
| 6 | 全面化 | 26 项能力 / 8 分类覆盖原文 25 条（评估驱动与 RAG 拆为 C17/C18），14 个 agent 覆盖 6 类任务（含 7 个垂直域） | `docs/02` 五要素由源码生成；门禁 12 逐条对表 |
| 7 | 自进化 | 数据飞轮 `record_flywheel`（成功任务经验写回共享记忆）、自净化 C07、评估驱动 C17 | 单测覆盖飞轮写入；`docs/02` C07/C16/C17 |
| 8 | 规划 | `planner-agent` + `routing.rs` 打分（门槛 0.45）+ 计划步骤自带 `est_tokens` 预算 | `docs/03` §3 表（打分公式与源码一致）；冒烟校验计划步骤 |

## 3. 边界：管什么，不管什么

**管（In scope）**

- 账号与鉴权：注册 / 登录 / 刷新 / 改密 / 登出（`/api/v1/auth/*`）
- 首次使用引导：6 步教程与跳过、重置（`/api/v1/onboarding*`）
- 任务全链路：提交 → 编排 → 执行 → 收敛 → 轨迹 → SSE 进度 → 取消
- 记忆：`private` / `shared` / `system` 三作用域读写与权限校验
- 能力与花名册的自描述接口：`/api/v1/capabilities*`、`/api/v1/agents*`（前端据此渲染，不必二份抄写）
- 治理与运维：限流、体积上限、审计、指标、健康检查（`/healthz` `/readyz` `/metrics`）

**不管（Out of scope，主动排除，避免做歪）**

- **不写前端页面**。前端由你提供 TS；本仓库只出 openapi、TS 类型与契约文档（`docs/30`、`docs/40`）。
- 不做多租户 SaaS、不做计费、不做模型训练/微调。
- 不托管具体业务数据（文件、表格、知识库内容），只做调度与治理层。
- 不承诺 OS 级沙箱隔离（现状是进程级 + 画像约束，缺口如实记在 `docs/06`）。

## 4. 运行时拓扑

单进程 axum 服务，内部按"端口-实现"分层，业务代码只依赖 trait，不依赖具体实现：

| 槽位 | 端口（trait） | 现有实现 | 切换方式 | 说明 |
| --- | --- | --- | --- | --- |
| 存储 | `Store` | `json`（快照）/ `memory` | `SA_STORAGE` | 每 15s 原子快照（`SA_SNAPSHOT_INTERVAL_SECS`） |
| 执行 | `StepRunner` | `sim` / `process` | `SA_RUNNER` | `sim` 为默认；`process` 真起外部脚本，超时 `SA_RUNNER_TIMEOUT_SECS`（默认 20s） |
| 审计 | `AuditSink` | `memory` / `file`（内存 + JSONL） | `SA_AUDIT` | 容量 `SA_AUDIT_CAPACITY`（默认 2000），细则见 `docs/70` |
| 限流 | `RateLimiter` | `memory` | `SA_LIMITER` | 每用户每小时提交配额 `SA_TASK_SUBMIT_QUOTA_PER_HOUR`（默认 60） |
| 指标 | `MetricsExporter` | `prometheus` | `SA_METRICS_EXPORT` | `/metrics` 可用 `SA_METRICS_TOKEN` 免登录抓取 |

另有两条不占槽位但同样隔离的通道：**事件总线**（`orchestration/events.rs`，SSE 数据源，可整体换 WebSocket/消息队列）与**审计**（写盘与业务解耦，失败不拖住任务）。

## 5. 一次请求的完整生命周期

```text
POST /api/v1/tasks
  │
  ├─(1) 入口治理：CORS → 体积上限(SA_MAX_BODY_BYTES, 默认 256KB) → Bearer 鉴权 → 提交配额
  │        失败即 Problem JSON：401 unauthorized / 413 体积 / 429 配额
  ├─(2) 入参校验（api/jsons.rs, api/extract.rs）  失败 422 + details 字段级明细
  ├─(3) 装配上下文（懒加载）只装命中 agent 的 skill / 文档 / 可见记忆
  ├─(4) 路由打分（orchestration/routing.rs）
  │        task_type 识别 → worker 打分 → 门槛 0.45 未达标则 super-agent 自行承接
  ├─(5) 生成计划步骤（每步带 est_tokens）
  ├─(6) 执行（orchestration/executor.rs）
  │        每步：RBAC 权限检查 → 红线检查 → 预算检查（超则降档，降无可降 aborted_budget）
  │             → StepRunner 执行 → 失败重试（SA_TASK_RETRY_LIMIT，默认 2）
  │             → 失败步数达 3 熔断（Circuit）/ 连续拒绝达 3（Permission）/ 用户取消（Cancel）
  ├─(7) 收敛：agreement_score 结算（初始 0.7，失败步扣减，clamp [0, 0.99]）、result_summary 合成
  └─(8) 出口统一：Store 落库 → EventBus 广播终态 → AuditSink 记账 → MetricsExporter 打点
```

这一步序有个刻意的设计：**出口只有一处**（(8)）。所以"落库了但没广播"或"广播了但没审计"这类状态不一致，
在结构上就不该出现；出现即 bug，而不是"配置问题"。

## 6. 模块地图

```text
src/
  main.rs / lib.rs        进程装配（读配置 → 建槽位实现 → 注入 AppState → 起 axum）
  config.rs               全部 SA_* 环境变量解析与默认值（25 个）
  state.rs                AppState：各端口实现的持有者
  ports.rs                五槽位的 trait 定义（换件的唯一契约面）
  error.rs                统一错误 → Problem JSON 映射
  domain/                 纯领域：agent、capability、task、event、onboarding、memory
  orchestration/          编排：routing（选人）、executor（治理链）、events（总线）、runners/（sim、process）
  security/rbac.rs        七项原子权限与三档权限组
  store/                  存储实现与快照
  obs/                    指标与审计
  services/               面向用例的服务层（鉴权、任务、记忆、教程）
  api/                    mod.rs 路由表、handlers/、extract/、jsons/
```

## 7. 六部分文档导航

| 首个需求的部分 | 文档 | 性质 | 防漂移手段 |
| --- | --- | --- | --- |
| 总体定位 | 本文 `01` | 手写设计说明 | 公理表 ↔ `regress_all.py` 门禁 |
| 能力清单 | `02` | **由源码生成** | `gen-spec-docs.py --check` |
| 分工协作 | `03` | **由源码生成** | `gen-spec-docs.py --check` |
| 交互规范 | `04` + `07` | 手写规范 + **索引由 openapi 生成** | `check-openapi.py`、`check-contract.py`、`gen-ts-types.py --check` |
| 边界处理 | `05` | 手写规范 | 治理单测 + `matrix.py` + `audit-probe-test.py` |
| 验收标准 | `06` | 手写标准 | `regress_all.py`（24 道门禁） |

结论先行：**"文档说能"不算数，"脚本跑绿"才算数。** 02/03/07 三份干脆不给人手写的机会——
它们是脚本从源码生成的，源码一变、文档没重生成，门禁直接红。

## 8. 已知缺口（如实登记，细则见 `docs/06`）

| 缺口 | 现状 | 影响 | 计划 |
| --- | --- | --- | --- |
| 存储仅 JSON 快照 + 内存 | 每次快照全量写盘 | 任务量大时写放大 | 预留 `Store` 端口，换 SQLite 不动业务层 |
| ~~SSE 无断点续传~~ **已具备** | 带 `Last-Event-ID` 重连只补后续帧、不重推快照；补发为空且任务终态则主动 EOF | 断线重连不丢中间态 | 无（门禁 17 的 A7 系列守此契约；跨机续传随集群一并排） |
| 无 OS 级沙箱配额 | 进程级画像约束 | 恶意脚本可吃满 CPU/内存（本机个人场景可接受） | 接 cgroup/Job Object 时只换 runner |
| 前端零代码 | 只有 openapi / TS 类型 / 契约文档 | 需要你那边落地页面 | 已在 `docs/30`、`docs/40`、`docs/80` 给出方案 |
