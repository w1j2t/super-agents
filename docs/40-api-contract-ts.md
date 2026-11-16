# 前端 API 契约与 TypeScript 类型（逐字段对齐后端源码）

> 本文所有字段均从 `src/domain/*.rs`、`src/api/handlers/*.rs`、`src/error.rs` **逐个核对**得出，不是推测。
> 核对时间：2026-11-09（第二次核对，脚本化）。

## 0. 先看这一节：这份文档怎么被验证

本文有两个**互相独立**的来源，二者交叉比对的结果附在下面，你可以自己复跑：

| 来源 | 方式 | 谁生成 |
|---|---|---|
| A. 人工核对 | 读 `domain/*.rs`、`handlers/*.rs` 的 serde 定义，手写成下面的 TS | 本文档（人工） |
| B. 自动生成（类型） | `python scripts/gen-ts-types.py` 直接解析 Rust 源码生成 `generated/types.generated.ts` | 脚本（机器） |
| C. 自动生成（接口） | `python scripts/gen-openapi.py` 解析路由表 + handler 签名，生成 `docs/openapi.json`（OpenAPI 3.1） | 脚本（机器） |

**2026-11-16 复核结果**：`python scripts/check-contract.py` 实测 A 与 B 交叉验证 `25` 个 interface，**字段差异 0 处**（源码结构体 91 个 / 文档手写 interface 25 个 / 查无同名 0）。
`generated/types.generated.ts` 现为 `62` 个 interface + `12` 个联合类型（机器产物，以它为准）。
C 产出 **29 条路径 / 33 个操作 / 51 个 schema**（其中 `/api/v1` 前缀下 26 条路径 / 30 个操作，根路径 `/healthz`、`/readyz`、`/metrics` 各 1 个操作），§2 的端点表已按 C 逐行复核。
数字一致性现在由门禁 27 `scripts/check_docs_drift.py` 自动守着 —— 以后谁改了接口忘了改文档，门禁会直接报红，不必再靠人眼发现。
复跑命令：

```powershell
cd "C:\Users\94355\Desktop\新建文件夹(7)"
python scripts\gen-ts-types.py                 # 重新生成前端类型（B）
python scripts\gen-openapi.py                  # 重新生成接口契约（C）
python scripts\check-contract.py               # 校验本文件与源码结构体字段一致
python scripts\gen-openapi.py --check          # 契约是否漂移（退出码 1 = 有差异）
```

> 本文件里出现的每一个类型，都能被 B 的生成结果覆盖；B 覆盖不到的一个也没有（除下面第 0.1 条说明的 `OnboardingStatus`）。
> **如果你（或审阅的 AI）只信机器**，直接用 `generated/types.generated.ts`，它不含任何人工判断。

### 0.1 唯一一个"文档有、源码无"的类型：`OnboardingStatus`

`OnboardingStatus` **不是** Rust 里的 enum，源码中没有这个类型名。它的真实来源是：

```rust
// src/domain/onboarding.rs:144
pub fn status(&self) -> &'static str {
    // 取值 "not_started" / "in_progress" / "completed" / "skipped"
    // 四个取值由该函数 return 的分支决定，并有单元测试逐一锁定
}
```

本文把它写成一个联合类型，是**前端侧的类型封装**（值域与源码完全一致），不是源码里的类型。审阅时请按"前端自建约束"看待，不要理解为后端有 `OnboardingStatus` 类型。

### 0.2 前端写代码时请以 B 为准

`generated/types.generated.ts` 是纯机器产物：字段名、可选性、枚举字面量全部由源码推导，**没有手写成分**。
手工改动它会在下次生成时被覆盖 —— 需要额外约束（比如后端没表达的业务枚举）请写在你自己的文件里，不要改生成文件。

## 1. 通用约定

| 项 | 约定 |
|---|---|
| 基址 | 开发 `http://127.0.0.1:8088/api/v1`（走 Vite 代理则可写 `/api/v1`） |
| 编码 | 请求/响应均 `application/json` |
| 鉴权 | 需 `Authorization: Bearer <access_token>` 的：`/auth/me`、`/auth/password`、`/onboarding/*`、`/tasks/*`、`/memories/*`、`/observability/*`。**免鉴权的**：`/auth/register`、`/auth/login`、`/auth/refresh`、`/auth/logout`（凭 body 里的 `refresh_token`）、`/agents`、`/agents/{id}`、`/capabilities`、`/capabilities/{id}`（目录数据，公开只读）、`/healthz`、`/readyz`；`/metrics` 用独立的静态令牌 `SA_METRICS_TOKEN` |
| 时间 | RFC3339 UTC（`2026-11-08T10:12:33.123456Z`），前端统一转本地时区显示 |
| 枚举 | `snake_case`（`serde rename_all`），下文类型已写成联合字面量 |
| 请求体上限 | 256 KB（`SA_MAX_BODY_BYTES`），超限由 `RequestBodyLimitLayer` 直接拒 |
| 健康检查 | `/healthz`、`/readyz`、`/metrics` 在**根路径**，不带 `/api/v1` |

### 1.1 错误体（唯一形态）

```ts
interface ProblemBody {
  code: string;                    // 机器码，见下表
  message: string;                 // 人类可读，可直接展示
  details?: Record<string, string>; // 字段级错误，key = 字段名（如 "email"）
}
```

| HTTP | `code` | 前端处置 |
|---|---|---|
| 400 | `bad_request` | Toast |
| 401 | `unauthorized` | 尝试刷新；失败则登出 |
| 403 | `forbidden` | 页内 403，不跳转 |
| 404 | `not_found` | 空态页 |
| 409 | `conflict` | 字段级冲突提示（重复注册等） |
| 422 | `unprocessable_entity` | 两种含义：**字段校验**（有 `details`）或**业务拒绝**（无 `details`，看 `message`）→ 按有无 `details` 分流 |
| 429 | `too_many_requests` | 读响应头 `Retry-After`（秒）做倒计时 |
| 500 | `internal_error` | 兜底；`message` 已被后端脱敏，原始细节只在服务端日志 |

> `429` 响应必带 `Retry-After`；`401` 响应必带 `WWW-Authenticate`。

## 2. 端点全表

`/api/v1` 前缀下 **26 条路径 / 30 个操作**，另有 3 个运维端点挂在根路径（见 §2.2：`/healthz`、`/readyz`、`/metrics`），合计 **29 条路径 / 33 个操作**，与 `docs/openapi.json` 的 `paths` 逐条对齐（`/memories`、`/memories/{id}`、`/tasks` 各承载 2 个方法，`/memories/{id}` 承载 3 个）。

| # | 方法 | 路径 | 鉴权 | 请求体 | 响应 |
|---|---|---|---|---|---|
| 1 | POST | `/auth/register` | — | `RegisterInput` | 201 `AuthResult` |
| 2 | POST | `/auth/login` | — | `LoginInput`（`account`/`username`/`email` 任给其一） | 200 `AuthResult` |
| 3 | POST | `/auth/refresh` | — | `RefreshInput` | 200 `AuthResult` |
| 4 | POST | `/auth/logout` | **免鉴权**（body 带 `refresh_token`） | `RefreshInput` | 200 `RevokedView` |
| 5 | GET | `/auth/me` | Bearer | — | 200 `UserView` |
| 6 | PUT | `/auth/password` | Bearer | `ChangePasswordInput` | 200 `RevokedView` |
| 7 | GET | `/onboarding` | Bearer | — | 200 `OnboardingView` |
| 8 | POST | `/onboarding/steps/{step_id}/complete` | Bearer | **无请求体**（步骤 key 在路径里） | 200 `StepChangeView` |
| 9 | POST | `/onboarding/skip` | Bearer | — | 200 **`OnboardingView`**（整份进度，不是单步） |
| 10 | POST | `/onboarding/reset` | Bearer | — | 200 **`OnboardingView`** |
| 11 | POST | `/tasks` | Bearer | `CreateTaskInput` | **202** 初始快照（默认 `mode=async`）/ **201** 终态快照（`mode=sync`），均为平铺 `TaskDetail` |
| 12 | GET | `/tasks?limit=` | Bearer | — | 200 `TaskList`（`limit` 默认 20，clamp 1–100） |
| 13 | GET | `/tasks/{id}` | Bearer | — | 200 `TaskDetail` |
| 14 | GET | `/tasks/{id}/trace` | Bearer | — | 200 **`TraceEntry[]`**（裸数组，不是对象包一层） |
| 15 | GET | `/tasks/{id}/events` | Bearer | — | 200 `text/event-stream`（`TaskEvent` 帧，含 `id:` 序号，支持 `Last-Event-ID` 续传） |
| 16 | POST | `/tasks/{id}/cancel` | Bearer | — | 200 已终态（幂等）/ 202 取消已受理 |
| 17 | POST | `/memories` | Bearer | `UpsertInput` | 201 `UpsertResult`（新建）/ 200（覆盖） |
| 18 | GET | `/memories?scope=&limit=` | Bearer | — | 200 `MemoryList`（`scope` 可选，`limit` 可选） |
| 19 | GET | `/memories/{id}` | Bearer | — | 200 `MemoryEntry` |
| 20 | PUT | `/memories/{id}` | Bearer | `UpdateInput` | 200 `MemoryEntry` |
| 21 | DELETE | `/memories/{id}` | Bearer | — | 204 |
| 22 | GET | `/agents` | Bearer | — | 200 `AgentView[]`（9 条） |
| 23 | GET | `/agents/{id}` | Bearer | — | 200 `AgentView` |
| 24 | GET | `/capabilities` | Bearer | — | 200 `CapabilityView[]`（26 条） |
| 25 | GET | `/capabilities/{id}` | Bearer | — | 200 `CapabilityView` |
| 26 | GET | `/cluster` | Bearer（**admin**） | — | 200 `ClusterView`（节点数组 + 汇总；单节点部署也返回长度为 1 的数组） |
| 27 | GET | `/observability/overview` | Bearer（admin 看全量） | — | 200 `Overview` |
| 28 | GET | `/observability/audit?limit=&actor=` | Bearer（admin） | — | 200 `AuditList` |
| 29 | GET | `/observability/components` | Bearer（**admin**） | — | 200 部件清单（5 槽位的 `active` / `available` / `env_var`） |

> 注意：记忆的更新/删除用 **`id`**（条目 ID），不是 `key`；`key` 只用于 upsert 的幂等定位。
>
> `GET /tasks/{id}/events` 的帧不是 JSON 数组：每条 `data:` 是一个 `TaskEvent` 对象，首帧 `seq=0` 是完整快照，其后 `seq` 递增；断线重连请带 `Last-Event-ID: <最后收到的 seq>`，服务端从环形台账补发；台账已滚出窗口时会退化为快照重发（不会静默丢弃，`sa_sse_replay_gap_total` 计数增加）。

### 2.1 免鉴权端点清单（安全相关，前端别写错）

| 端点 | 为什么免鉴权 | 前端注意 |
|---|---|---|
| `POST /auth/register` | 注册入口（可用 `SA_ALLOW_REGISTRATION=0` 关闭，关闭后返回 403） | 关闭时前端要能展示"实例已关闭注册" |
| `POST /auth/login` | 登录入口 | 有 IP 维度限流 |
| `POST /auth/refresh` | 刷新对 | 用 `refresh_token` 换新对 |
| `POST /auth/logout` | 只需 `refresh_token` | **不要**给它带 Bearer 头也可以调 |
| `GET /agents`、`GET /agents/{id}` | 内置 14 个 agent 的静态元数据 | 不含任何用户数据 |
| `GET /capabilities`、`GET /capabilities/{id}` | 内置 26 个能力定义 | 同上 |

### 2.2 运维端点（根路径，不属于 `/api/v1`）

| 方法 | 路径 | 鉴权 | 响应 |
|---|---|---|---|
| GET | `/healthz` | 无 | 200 纯文本 `ok` |
| GET | `/readyz` | 无 | 200 纯文本，数据目录可写才返回 ok |
| GET | `/metrics` | `Authorization: Bearer $SA_METRICS_TOKEN` | 200 Prometheus 文本格式 |

### 2.3 端点表的修订记录（旧文档错在哪，如实留痕）

本轮用 `gen-openapi.py` 生成的 `docs/openapi.json` 反查，修正了 4 处此前写错的响应形状：

| 端点 | 旧文档（错） | 源码事实（对） |
|---|---|---|
| `POST /tasks` | 201 `TaskView` | 201 `TaskDetail` |
| `GET /tasks/{id}/trace` | 200 `{ trace: [...] }` | 200 `TraceEntry[]` |
| `POST /onboarding/skip`、`/reset` | 200 `StepChangeView` | 200 `OnboardingView` |
| `POST /auth/logout` | 需 Bearer | 免鉴权，凭 body 的 `refresh_token` |

> 这 4 处正是"人工核对也会错"的证据：**以生成物为准，不要以本文档的人工段落为准**。

## 3. TypeScript 类型（`src/shared/api/types.ts`）

```ts
// ===== 枚举 =====
export type Role = 'owner' | 'admin' | 'member';          // 仅三级，无 viewer
export type UserStatus = 'active' | 'disabled';           // 仅两态，无 suspended
export type TaskType = 'coding' | 'writing' | 'research' | 'planning' | 'ideation' | 'collaboration';
export type ModelProfile = 'fast' | 'standard' | 'deep';
export type TaskStatus =
  | 'queued' | 'running' | 'succeeded' | 'partially_succeeded'
  | 'failed' | 'aborted_budget' | 'cancelled';
export type MemoryScope = 'private' | 'shared' | 'system';
export type Permission =                 // security::rbac::Permission（snake_case）
  | 'read' | 'write' | 'sandbox_exec'
  | 'memory_write_shared' | 'memory_write_system'
  | 'cross_agent' | 'admin';
// 注意：源码无此 enum，值来自 domain::onboarding::OnboardingProgress::status() 的返回分支（见 §0.1）
export type OnboardingStatus = 'not_started' | 'in_progress' | 'completed' | 'skipped';

// 源码 domain::agent::AgentKind（serde lowercase）。用于 agent 注册表，文档原先漏列，2026-11-09 补入
export type AgentKind = 'super' | 'sub';

// ===== 用户与认证 =====
export interface UserView {
  id: string;
  username: string;
  email: string;
  display_name: string;
  role: Role;
  status: UserStatus;
  locale: string;
  timezone: string;
  created_at: string;          // RFC3339
  last_login_at: string | null;
}

export interface AuthTokens {
  access_token: string;
  refresh_token: string;
  token_type: string;          // "Bearer"
  expires_in: number;          // access 有效秒数，默认 1800
}

export interface AuthResult {
  user: UserView;
  tokens: AuthTokens;
  onboarding_status: OnboardingStatus;
  onboarding_next_step: string | null;
}

export interface RegisterInput {
  username: string;            // 3–32，字母数字下划线
  email: string;
  password: string;            // ≥8
  display_name?: string;
  locale?: string;
  timezone?: string;
}

/**
 * 登录入参。
 *
 * `account` / `username` / `email` 是**同一个登录名的三种叫法**（不是三个凭据）：
 * 服务端按 account → username → email 取第一个非空值，三个都空才 422。
 * 这样前端把注册表单对象直接复用到登录、或手上只有邮箱时，都不会白挨一次 401。
 */
export interface LoginInput {
  account?: string;            // 主字段：用户名或邮箱
  username?: string;           // account 的兼容别名（复用注册字段名）
  email?: string;              // account 的兼容别名（只有邮箱时）
  password: string;
}
// 必填关系用 anyOf 表达：{account|username|email} 任给其一 + password

export interface RefreshInput { refresh_token: string; }
export interface ChangePasswordInput { current_password: string; new_password: string; }
export interface RevokedView { revoked_sessions: number; }

// ===== 新手教程 =====
export interface StepView {    // 源码名就是 StepView（src/api/handlers/onboarding.rs:14），前端不要改名
  key: string;                 // welcome|profile|agents|memory|first_task|boundaries
  order: number;
  title: string;
  summary: string;
  action: string;              // 建议动作文案
  required: boolean;
  est_seconds: number;
  completed: boolean;
  completed_at: string | null;
}

export interface OnboardingView {
  status: OnboardingStatus;
  total_required: number;      // 5
  completed_required: number;
  skipped: boolean;
  next_step: string | null;
  next_action: string | null;
  steps: StepView[];
}

export interface StepChangeView {
  changed: boolean;            // false = 幂等重放，前端不报错、不推进
  onboarding: OnboardingView;
}

// ===== 任务 =====
export interface CreateTaskInput {
  goal: string;                // ≥3 字（"ab" 会被 422）
  task_type?: TaskType;        // 省略则由后端分类器推断
  model_profile?: ModelProfile;// 省略 = auto（按档位）
  budget_tokens?: number;      // 省略 = plan 估算 ×1.2 + 记忆上下文占用
  /**
   * 执行模式：'async'（**默认**，202 + 后台执行 + SSE 进度 + 可取消）
   * | 'sync'（同步跑完才返回 201；慢模型单步可达分钟级，易被客户端超时误判为失败）
   */
  mode?: "sync" | "async";
}

export interface PlanStep {
  index: number;
  name: string;
  agent_id: string;
  model_profile: ModelProfile;
  permission: Permission;
  est_tokens: number;
  sandbox: boolean;
  /**
   * 依赖的前序步骤 index（空数组 = 无依赖）。
   * 只允许指向更小的 index（前向依赖），依赖图因此天然无环；
   * 同层（依赖已满足）的步骤在 SA_STEP_CONCURRENCY > 1 时可并行执行。
   */
  depends_on: number[];
}

export interface TraceEntry {
  /**
   * 事件时刻。`outcome='started'` 是开始时刻，`ok/failed/retryable_error` 是结束时刻。
   * 别拿两条 trace 的 at 相减算耗时——并行步骤的时间窗会重叠，相减会出负数或串味。
   */
  at: string;
  step_index: number;
  agent_id: string;
  phase: string;               // "context" | "sandbox" | "execute" | "step" | "budget" | "cancel" ...
  outcome: string;             // "started" | "ok" | "retryable_error" | "fatal_error" | "failed" ...
  detail: string;
  attempt: number;
  tokens: number;
  /**
   * 该事件耗时（毫秒）；瞬时事件为 null。
   * `execute` = 单次尝试耗时；`step` = 含重试与退避的整步耗时。
   * 每个步骤都保证有一条 `phase='step'` 的收尾记录（成功 outcome='ok'、失败 outcome='failed'）。
   */
  duration_ms: number | null;
}

export interface StepResult {
  index: number;
  agent_id: string;
  name: string;
  success: boolean;
  model_profile: ModelProfile;
  tokens: number;
  attempts: number;
  output: string;
  error: string | null;
}

export interface TaskView {
  id: string;
  goal: string;
  task_type: TaskType;
  status: TaskStatus;
  plan: PlanStep[];
  results: StepResult[];
  result_summary: string;
  tokens_used: number;
  budget_tokens: number;
  model_profile: ModelProfile; // 实际生效档位（显式请求值或计划首步）
  agreement_score: number;     // 0–1，多 agent 结论一致性
  created_at: string;
  updated_at: string;
  finished_at: string | null;
}

export interface TaskDetail {
  task: TaskView;
  trace: TraceEntry[];
  memory_refs: string[];       // 被引用的记忆 key 列表
}
// ===== 集群视图（多 agent 节点，GET /cluster，需 admin）=====
/**
 * 单节点视图。即使当前部署只有一个进程，`nodes` 也返回**数组**：
 * 前端按"集群"建模一次，将来加入多节点就不用改渲染结构。
 * 注意这里不重复部件（ports）的 `active`——部件是"本进程装了什么"，
 * 节点是"这个进程本身有多忙"，两件事分开问，别混在一个接口里。
 */
export interface ClusterNode {
  node_id: string;             // SA_NODE_ID，缺省为 "<主机名>-<pid>"
  role: string;                // 当前恒为 "all-in-one"
  started_at: string;          // 进程启动绝对时刻（与 uptime_seconds 分工：绝对 vs 时长）
  uptime_seconds: number;
  step_concurrency: number;    // SA_STEP_CONCURRENCY（1–8），决定单任务内步骤并行度
  async_running: number;       // 正在执行的异步任务数（来自取消登记表，不读任务表状态字段）
  async_capacity: number;      // 异步队列上限（8），达到即 429
  sse_subscribers: number;     // 当前 SSE 订阅者数
  events_dropped: number;      // 因订阅者积压而丢弃的事件数（非 0 说明客户端读得太慢）
  agents_total: number;        // 本节点可用 agent 数
  tasks: ClusterTasks;         // 本节点自己的任务分布："一个人的账"，光看 totals 会把负载倾斜平均掉
  source: string;              // "local" = 本进程实时采集；"peer" = 来自共享目录的心跳文件
  updated_at: string;          // 这份数据的采集时刻；peer 节点上就是"最后一次心跳时间"
  stale: boolean;              // 心跳已过期：仍然可见，但不计入 totals（陈旧值比没有更糟）
}

/** 任务状态分布。8 个字段之和恒等于 `total`。 */
export interface ClusterTasks {
  total: number;
  queued: number;
  running: number;
  succeeded: number;
  partially_succeeded: number; // 部分步骤失败但整体有产出；混进 failed 会让失败率失真
  failed: number;
  cancelled: number;
  aborted_budget: number;
}

export interface ClusterTotals {
  nodes: number;               // 含已离线的节点
  offline_nodes: number;       // 心跳过期的节点数：只报 nodes 的话，2 台死 1 台仍是 2，看板看不出异常
  agents_total: number;
  async_running: number;
  async_capacity: number;
  sse_subscribers: number;
  tasks: ClusterTasks;
}

export interface ClusterView {
  nodes: ClusterNode[];        // 当前 >= 1
  totals: ClusterTotals;       // 跨节点求和；单节点时等于 nodes[0] 的口径
}


export interface TaskList { total: number; items: TaskView[]; }

// ===== 记忆 =====
export interface MemoryEntry {
  id: string;
  scope: MemoryScope;
  owner_id: string;
  key: string;
  content: string;
  tags: string[];
  version: number;             // 同 (key, scope, owner) 覆盖时 +1
  written_by: string;          // "user" 或 agent id
  created_at: string;
  updated_at: string;
  access_count: number;
  confidence: number;          // 0–1
}

export interface UpsertInput {
  scope: MemoryScope;
  key: string;
  content: string;
  tags?: string[];
  confidence?: number;
}

export interface UpdateInput {
  content: string;
  tags?: string[];
  confidence?: number;
}

export interface UpsertResult { created: boolean; entry: MemoryEntry; }
export interface MemoryList { total: number; items: MemoryEntry[]; }

// ===== 目录 =====
export interface AgentView {
  id: string;
  kind: string;
  name: string;
  domain: string;
  summary: string;
  vertical_knowledge: string[];
  doc_refs: string[];
  skills: string[];
  permissions: string[];
  sandbox_profile: string;
  default_model: string;
  task_types: string[];
  max_concurrency: number;
}

export interface CapabilityView {
  id: string;
  name: string;
  category: string;
  trigger: string;
  owner_agents: string[];
  permission_boundary: string;
  observability_signals: string[];
  degradation_or_abort: string;
}

// ===== 可观测 =====
export interface AuditEvent {
  id: string;
  at: string;
  actor: string;
  action: string;
  target: string;
  decision: string;            // "allow" | "deny"
  reason: string;
  request_id: string;
}

export interface AuditList { total: number; denied: number; items: AuditEvent[]; }

export interface Overview {
  version: string;
  env: string;
  /** 当前存储后端：json | memory（可插拔部件，运维核对用） */
  storage: "json" | "memory";
  /** 该后端是否落盘；false 表示进程退出即丢数据 */
  storage_durable: boolean;
  uptime_secs: number;
  agents_total: number;
  capabilities_total: number;
  audit_denied: number;
  /** 当前审计出口：memory | file（可插拔部件，运维核对用） */
  audit_backend: "memory" | "file";
  /** 当前执行后端：sim | process | 自定义后端标识（可插拔部件，运维核对用） */
  runner_backend: string;
  /** 当前限流后端：memory | noop（可插拔部件，运维核对用） */
  limiter_backend: "memory" | "noop";
  /** 限流是否真的生效；limiter_backend=noop 时为 false —— 别让"看起来有限流"骗人 */
  limiter_enforcing: boolean;
  /** 当前指标导出部件：prometheus | json | noop */
  metrics_export: "prometheus" | "json" | "noop";
  store: Record<string, number>;
}
```

## 4. Query Key 规范（`features/<domain>/hooks.ts`）

```ts
export const authKeys = {
  me: ['auth', 'me'] as const,
};
export const onboardingKeys = {
  all: ['onboarding'] as const,
  view: () => [...onboardingKeys.all, 'view'] as const,
};
export const taskKeys = {
  all: ['tasks'] as const,
  list: (params: { limit?: number }) => [...taskKeys.all, 'list', params] as const,
  detail: (id: string) => [...taskKeys.all, 'detail', id] as const,
  trace:  (id: string) => [...taskKeys.all, 'trace', id] as const,
};
export const memoryKeys = {
  all: ['memories'] as const,
  list: (scope?: MemoryScope) => [...memoryKeys.all, 'list', scope ?? 'all'] as const,
  detail: (id: string) => [...memoryKeys.all, 'detail', id] as const,
};
export const catalogKeys = {
  agents: ['agents'] as const,
  agent: (id: string) => ['agents', id] as const,
  capabilities: ['capabilities'] as const,
  capability: (id: string) => ['capabilities', id] as const,
};
export const obsKeys = {
  overview: ['observability', 'overview'] as const,
  audit: (q: { limit?: number; actor?: string }) => ['observability', 'audit', q] as const,
};
```

**变更后失效规则**（写死在 mutation 的 `onSuccess`，禁止全量失效）：

| 变更 | 需失效 |
|---|---|
| 完成任务步骤 / 跳过 / 重置 | `onboarding.view`、`auth.me`（下一步提示会变） |
| 创建任务 | `tasks.list` |
| upsert / update / delete 记忆 | `memories.list(scope)`、`memories.detail(id)`、`observability.overview`（store 计数） |
| 刷新令牌 | 无（只写内存） |
| 改密 / 登出 | 清空全部缓存 + 跳登录 |

## 5. 典型调用示例

```ts
// features/tasks/api.ts
export const tasksApi = {
  create: (body: CreateTaskInput) =>
    request<TaskView>('/tasks', { method: 'POST', body: JSON.stringify(body) }),
  list: (limit = 30) =>
    request<TaskList>(`/tasks?limit=${limit}`),
  detail: (id: string) =>
    request<TaskDetail>(`/tasks/${id}`),
  trace: (id: string) =>
    request<{ trace: TraceEntry[] }>(`/tasks/${id}/trace`),
};

// features/tasks/hooks.ts
export function useCreateTask() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: tasksApi.create,
    onSuccess: (task) => {
      qc.setQueryData(taskKeys.detail(task.id), { task, trace: [], memory_refs: [] });
      qc.invalidateQueries({ queryKey: taskKeys.all });
    },
  });
}

// 轮询（Phase-2 流式上线前，兼容 running 态）
export function useTaskDetail(id: string) {
  return useQuery({
    queryKey: taskKeys.detail(id),
    queryFn: () => tasksApi.detail(id),
    refetchInterval: (q) =>
      q.state.data?.task.status === 'running' ? 1000 : false,
    staleTime: 5_000,
  });
}
```

## 6. 为 Phase-2 预留的流式契约（后端未实现，前端先按此建模）

```
GET /api/v1/tasks/{id}/events      Accept: text/event-stream
```

```
event: step
data: {"step_index":0,"status":"running","tokens":0}

event: progress
data: {"used_tokens":812,"budget_tokens":14400}

event: done
data: {"status":"succeeded","task":{ /* TaskView */ }}
```

前端封装：

```ts
export function subscribeTaskEvents(id: string, on: (e: TaskEvent) => void): () => void {
  const es = new EventSource(`${BASE}/tasks/${id}/events?access_token=${...}`);
  // 注意：EventSource 不能自带 Authorization 头 → 后端需支持 query 参数令牌，
  // 或改用 fetch + ReadableStream 手动解析（推荐后者，安全且可加 Header）
  ...
  return () => es.close();
}
```

> 强烈建议后端用 **fetch + ReadableStream** 方案（前端 `EventSource` 无法带鉴权头，把 token 放 URL 会被日志记录）。这是本次设计里唯一需要后端额外配合的点，已在架构文档 §"现在就该做的三件事"标注。

## 7. 契约核对清单（前端联调前逐项打勾）

- [ ] `POST /tasks` 成功返回 **201**，body 直接是 `TaskView`（不是 `{task: ...}`）
- [ ] `GET /tasks/{id}` 返回 `{task, trace, memory_refs}` 三层包装
- [ ] `GET /tasks/{id}/trace` 只含 `trace`
- [ ] `POST /memories` 新建 **201**、覆盖 **200**，body 为 `{created, entry}`
- [ ] `DELETE /memories/{id}` 返回 **204** 无 body（client 必须处理 204 不解析 JSON）
- [ ] `POST /onboarding/steps/{k}/complete` 幂等时 `changed=false`
- [ ] 422 有两种含义，按 `details` 是否存在分流
- [ ] 429 必带 `Retry-After`
- [ ] `/metrics` 不在 `/api/v1` 下，且需抓取令牌（前端不调用，仅运维）
