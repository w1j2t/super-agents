# 04 交互规范

> 六部分之一（**交互规范**）。约束"外部怎么跟这个后端说话"：地址、鉴权、错误、任务、事件流、取消。
> 所有字段名与错误码都以 `docs/openapi.json`（源码生成）为唯一真源；本文解释**为什么这样定**，以及**哪些地方最容易踩**。

## 0. 原文三项要求 → 本文落点

首个需求对「交互规范」点名三件事：**统一交互风格、对话共享记忆、上下文策略**。此前这三项散落在多处，现逐条给落点。

| 原文要求 | 本文落点 | 一句话口径 |
| --- | --- | --- |
| 统一交互风格 | §1 通用约定 + §3 错误模型 | 所有接口同构：统一前缀、snake_case 枚举、不透明 id、RFC 3339 UTC、错误统一为 Problem JSON 且 `details` 指到字段 |
| 对话共享记忆 | §9.2 共享记忆策略 | 三作用域 `private`/`shared`/`system`，读可见性与写权限分离，任务创建时读、成功后写回 |
| 上下文策略 | §9.1 上下文装配策略 | 懒加载只装命中项、6000 字符预算、超预算摘要优先全文裁剪、截断会在收敛时扣分 |

## 1. 通用约定

| 项 | 约定 |
| --- | --- |
| 地址 | `http://127.0.0.1:8088`（`SA_BIND` 可改） |
| 版本 | 业务接口统一前缀 `/api/v1`；`/healthz` `/readyz` `/metrics` 不带前缀 |
| 编码 | 请求与响应均为 UTF-8 JSON |
| 时间 | RFC 3339 UTC，例：`2026-11-11T08:30:00Z` |
| 标识 | 所有 id 视为**不透明字符串**，前端不得解析其内部结构 |
| 鉴权 | `Authorization: Bearer <access_token>` |
| 枚举 | 状态/类型一律 **snake_case 字符串**（`aborted_budget`、`partially_succeeded`），前端不得本地拼写，用生成的 TS 联合类型 |
| 请求体上限 | 256 KB（`SA_MAX_BODY_BYTES`），超限由请求体限流层直接 `413` 拒绝，不进入业务层 |

## 2. 鉴权流程

```text
POST /api/v1/auth/register   {username, email, password[, display_name, locale, timezone]}
POST /api/v1/auth/login      {account, password}        → {tokens:{access,refresh}, user{...}}
POST /api/v1/auth/refresh    {refresh}                  → 新 tokens
POST /api/v1/auth/logout     {refresh}                  → 204
GET  /api/v1/auth/me                                    → 当前用户
PUT  /api/v1/auth/password   {current, next}            → 204
```

**必知的一个坑（实测踩过）**：注册用 `username`，登录用 **`account`**。
两者不是同一个字段名——`account` 设计上允许"用户名或邮箱"任填一种，所以不能写成 `username`。
写错时后端返回 422 且 `details` 会明确指出字段缺失，不会静默失败。

| 令牌 | 有效期 | 配置项 | 说明 |
| --- | --- | --- | --- |
| access | 30 分钟 | `SA_ACCESS_TTL_SECS`（1800） | 每次请求都要带 |
| refresh | 7 天 | `SA_REFRESH_TTL_SECS`（604800） | 只在 `/auth/refresh`、`/auth/logout` 用 |

前端约定：access 过期（401 + `code=unauthorized`）先静默 refresh 重试一次，仍失败才跳登录页；
**禁止**在 401 上做无限重试。

## 3. 错误模型（Problem JSON）

```json
{
  "code": "unprocessable_entity",
  "message": "参数校验未通过",
  "details": { "goal": "不能为空" }
}
```

- `code` 是**稳定枚举**，前端按它分支；`message` 只用于展示，不得作为判断依据。
- `details` 只在需要字段级归因时出现（422 等）。
- 未认证 / 已认证但越权，是两件事：`401` 是"你是谁不知道"，`403` 是"知道你是谁，但你不能"。

### 错误码按操作的真实分布

早期版本的 openapi 把七个错误码糊在全部 31 个操作上，那是填空，不是契约。
现已按**代码里真实会发生的路径**挂载，前端照此写兜底即可：

| 操作类别 | 400 | 401 | 403 | 404 | 409 | 422 | 429 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 公开只读：`GET /agents*`、`GET /capabilities*` | ✓ | — | — | 带 `{id}` 时 | — | ✓ | — |
| 注册 `POST /auth/register` | ✓ | — | — | — | ✓（用户名/邮箱占用） | ✓ | ✓（10 次/小时/IP） |
| 登录 / 刷新 | ✓ | ✓（凭据错误或刷新令牌失效） | — | — | — | ✓ | ✓（10 次/分钟、30 次/分钟） |
| 提交任务 `POST /tasks` | ✓ | ✓ | ✓ | — | — | ✓ | ✓（60 次/小时/用户） |
| 其余受保护接口 | ✓ | ✓ | ✓ | 带 `{id}` 时 | — | ✓ | — |
| `/healthz`、`/readyz` | — | — | — | — | — | — | — |

限流只存在于上述四处（`src/security/ratelimit.rs` 令牌桶），**不是全站中间件**。
`/healthz` 永远不会有 429——前端不要对它写退避逻辑。

## 4. 首次使用（新手教程）

`GET /api/v1/onboarding` 返回进度，6 步固定顺序，`step_id` 与标题一一对应：

| # | `step_id` | 标题 | 目的 |
| --- | --- | --- | --- |
| 1 | `welcome` | 认识你的超级 agent | 建立"一句话目标 → 自动执行"的预期 |
| 2 | `profile` | 设定个人偏好 | 语言/时区等，影响后续输出 |
| 3 | `agents` | 熟悉子 agents 分工 | 让用户知道"这事归谁" |
| 4 | `memory` | 建立共享记忆 | 让系统跨任务记住偏好 |
| 5 | `first_task` | 发起第一个任务 | 第一次真实跑通全链路 |
| 6 | `boundaries` | 了解能力边界与安全 | 先讲清不可逾越的红线 |

| 操作 | 接口 |
| --- | --- |
| 完成某步 | `POST /api/v1/onboarding/steps/{step_id}/complete` |
| 跳过整个教程 | `POST /api/v1/onboarding/skip` |
| 重置教程 | `POST /api/v1/onboarding/reset` |

规范：**教程可跳过但不可绕过**。跳过只影响引导展示，不影响鉴权与边界校验——
边界永远是代码硬约束，不靠用户"看过教程"。

## 5. 任务生命周期

### 5.1 提交

```http
POST /api/v1/tasks
{ "goal": "把上周的会议纪要整理成周报", "task_type": "writing", "budget_tokens": 60000 }
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `goal` | ✓ | 自然语言目标，是唯一必需输入 |
| `task_type` | — | 缺省时由路由打分识别；**打不到信号不会猜**，直接 422（见下方三种来源），不要"随便猜一个"填上 |
| `model_profile` | — | `fast` / `standard` / `deep` |
| `budget_tokens` | — | 缺省 60000（`SA_TASK_BUDGET_TOKENS`），超预算会降档或终止 |
| `mode` | — | `async`（缺省）/ `sync`，见下方两种模式 |

**两种模式** —— 响应体结构**完全一致**（都是平铺 `TaskDetail`），差异只在 `status` 是否已终态，
前端一套渲染逻辑吃两种模式：

| 模式 | 返回 | 什么时候用 |
| --- | --- | --- |
| `sync` | **201** + 终态快照 | 短任务（无模型或快模型，秒级返回）。脚本 / curl 且能确定秒级完成时才显式传它 |
| `async`（缺省） | **202** + `status=queued` 的初始快照 | 缺省即此模式。接上真实模型后**单步可达两分钟以上**，`sync` 会直接顶穿客户端 30s 默认超时 |

`async` 的三个坑（都是实测踩出来的）：

- **别把 202 的 `status` 当"已在跑"**：那一刻是 `queued`，随后后台线程先落一次 `running` 再执行——
  这一步是刻意的，不落库就没有事件，前端只能看到 `queued` 直接跳终态，直播流等于白做。
- **并发上限满了是拒绝，不是排队**：`SA_ASYNC_MAX_RUNNING`（缺省 8）。超出时返回 **429 + `Retry-After: 30`**，
  并把刚落库的那条 `queued` 就地收敛为 `cancelled`（`result_summary` 写明"未被受理、未消耗预算"）——
  绝不留下一条永远不动的僵尸任务。
- **进程重启**：内存里的执行体不会自己继续，`GET` 会看到它停在 `running`；`cancel` 能识别"没有执行体"并就地收敛（见 §5.4）。

**422 的三种来源** —— 同一个状态码三种含义，后两种 `details` 为空、原因在 `message` 里：

| 来源 | 特征 |
| --- | --- |
| 字段校验失败 | `details` 带字段级原因（缺 `goal`、`mode` 写成 `sync2`、`model_profile` 非法……） |
| **未识别出任务类型** | 没传 `task_type` 且 `goal` 未命中任何类型关键词。后端**不猜**——猜错就是把任务交给不合适的 agent。`message` 给两条出路：显式指定 `task_type`，或把 `goal` 写成「产出物＋领域」 |
| 超出能力边界 | 命中越界信号，或该任务类型下没有 agent 能承接；`message` 说明缺什么能力 |

### 5.2 查询

| 接口 | 用途 | 注意 |
| --- | --- | --- |
| `GET /api/v1/tasks` | 列表 | 支持分页/过滤，参数见 openapi |
| `GET /api/v1/tasks/{id}` | 详情 | **平铺结构**（`id`/`goal`/`status`/`trace`… 直接在最外层），与 openapi 的 `TaskDetail` 逐键一致；这条由门禁 14 起真 exe 发真请求校验，不是靠人眼 |
| `GET /api/v1/tasks/{id}/trace` | 逐步轨迹 | 前端时间轴回放的数据源，设计见 `docs/80` |

### 5.3 进度流（SSE）

```http
GET /api/v1/tasks/{id}/events      Accept: text/event-stream
```

```text
event: status
data: {"seq":0,"task_id":"t_...","status":"running","at":"...","finished_at":null,"summary":"..."}

event: status
data: {"seq":7,"task_id":"t_...","status":"succeeded","at":"...","finished_at":"...","summary":"..."}
```

| 契约点 | 说明 |
| --- | --- |
| 事件名 | 永远 `status`；序列化失败等异常帧为 `error` |
| 首帧 | `seq = 0` 的快照帧，连上就代表"当前状态"，前端不必先拉 detail 再订阅 |
| `seq` | 进程内单调递增，用于判断是否漏帧；**不连续就回查 detail**，这是设计的兜底 |
| 终态 | `succeeded` / `failed` / `partially_succeeded` / `aborted_budget` / `cancelled` 之一，服务端随即 EOF |
| 心跳 | 空闲 15s 一条，防止中间代理掐连接 |
| 背压 | 订阅者缓冲满时**丢该订阅者的帧**而非拖住任务；宁可少一帧中间态，也不能卡住执行 |
| 断点续传 | 每帧带 `id: <seq>`。断线重连带 `Last-Event-ID: <最后收到的 seq>`，服务端只补发 `seq > 锚点` 的帧，**不重推 `seq=0` 快照**；锚点非法（非数字/负数）按首次连接处理；补发为空且任务已终态时立即 EOF，不挂连接 |
| 续传退化 | 唯一退化场景是中间帧已滚出服务端环形台账（256 条）：此时退化为重发快照并累加 `sa_sse_replay_gap_total`。前端策略：`seq` 不连续就回查 `GET /tasks/{id}`，与上一条兜底同一套逻辑 |
| 契约守护 | 上述行为由门禁 17 的 A7 系列实测守护（只补后续帧 / 不重推快照 / 非法锚点退回 / 空补发 EOF / 锚点超前不挂），契约字段见 `openapi.json` 的 `x-stream.reconnectHeaderLastEventId` |

### 5.4 取消

```http
POST /api/v1/tasks/{id}/cancel
```

- **协作式**：在步骤边界与重试退避之前生效，正在执行的单步不会被打断（避免留下半写文件）。
- 取消不是故障：`AbortKind::Cancel` 不计入失败指标，终态 `cancelled`，
  否则"用户主动取消"会污染可用性口径。

三种结果必须分清，混成一个"200 成功"就是对调用方撒谎：

| 响应 | 含义 | 前端该怎么显示 |
| --- | --- | --- |
| **202** | 有执行体在跑，取消旗子已立起，**下一个步骤边界生效** | "已请求取消，将在当前步骤结束后停止" |
| **200** | 没有执行体：进程重启前遗留的 `queued`/`running`，它永远不会自己结束 | "任务已失效，已标记取消" |
| **409** | 任务已是终态，取消没有语义 | "任务已结束，无需取消"（别当失败重试） |

## 6. 可观测接口

| 接口 | 鉴权 | 用途 |
| --- | --- | --- |
| `GET /healthz` | 无需 | 存活性，永远轻量 |
| `GET /readyz` | 无需 | 就绪性（依赖是否可用） |
| `GET /metrics` | 需管理员令牌，或 `SA_METRICS_TOKEN` 静态令牌 | Prometheus 抓取 |
| `GET /api/v1/observability/overview` | 需 **Admin** 权限 | 任务/agent 概览数字 |
| `GET /api/v1/observability/components` | 需 **Admin** 权限 | 五槽位当前实现与健康度——**换件后第一眼就该看这里** |
| `GET /api/v1/observability/audit` | 需 **Admin** 权限 | 审计查询 |

## 7. 前端对接三条铁律

1. **类型不手写**：`docs/openapi.json` 是唯一真源，TS 类型由 `scripts/gen-ts-types.py` 生成。
   手抄一个 `interface` 就多一处漂移源，`check-contract.py` 会当场打红。
2. **枚举不拼字符串**：状态、任务类型、`code` 都用生成的联合类型；
   传了不存在的枚举值，后端 422，`details` 会告诉你是哪个字段。
3. **重试要有边界**：401 静默 refresh 一次；429 看 `Retry-After` 退避；5xx 指数退避并设上限。
   无上限重试会把限流配额烧光，反而把可用性做没了。

## 8. 最小可用调用序列

```bash
# 1. 注册 → 登录
curl -X POST http://127.0.0.1:8088/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"username":"boss","email":"boss@local","password":"Passw0rd!23"}'

TOKEN=$(curl -s -X POST http://127.0.0.1:8088/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"account":"boss","password":"Passw0rd!23"}' | python -c "import sys,json;print(json.load(sys.stdin)['tokens']['access'])")

# 2. 提交任务（同步返回终态；短任务）
curl -X POST http://127.0.0.1:8088/api/v1/tasks \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"goal":"写一份本周项目周报","task_type":"writing"}'

# 2b. 提交任务（异步，长任务/接真模型时用这条）
TASK=$(curl -s -X POST http://127.0.0.1:8088/api/v1/tasks \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"goal":"写一份本周项目周报","task_type":"writing","mode":"async"}' \
  | python -c "import sys,json;print(json.load(sys.stdin)['id'])")

# 2c. 跟进度（SSE 直播，Ctrl-C 退出）
curl -N -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8088/api/v1/tasks/$TASK/events

# 3. 看轨迹与组件状态
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8088/api/v1/tasks/$TASK/trace
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8088/api/v1/observability/components
```

## 9. 上下文与共享记忆策略（原文第 4 部分）

> 这两项是"看不见的交互"：前端不传，但决定了同一个人问同一句话，为什么第二次结果更好。
> 全部行为在 `src/orchestration/context.rs`，数值以源码为准。

### 9.1 上下文装配策略

| 环节 | 规则 | 源码 |
| --- | --- | --- |
| 预算 | `CONTEXT_BUDGET = 6_000`（近似 token；中文 1 字≈1 token、英文 4 字符≈1 token，按 1.4 折中） | `context.rs` |
| 候选过滤 | 相关度必须 `> 0.05`，最多取 **前 5 条** | `assemble()` |
| 相关度打分 | key 命中 `+0.5` / tag 命中 `+0.3` / 正文命中 `+0.1`，再加访问热度 `access_count×0.002`（上限 50 次）与置信度 `confidence×0.05` | `relevance()` |
| 中文切词 | 按非字母数字边界切分；8 字以内的中文段补**二元组**（"注意写作风格"能召回"写作"），去重后最多 24 个词 | `terms()` |
| 超预算 | **摘要优先、全文裁剪**：保留 120 字头部 + `…（已裁剪）`，置 `truncated = true`，不再追加后续记忆 | `assemble()` |
| 无命中 | 摘要写"（无相关历史记忆，本次按通用策略执行）"，不编造 | 同上 |
| 截断的后果 | 收敛时 `agreement_score -= 0.05`——上下文不全，交付评分就该低一点，不假装没发生 | `executor.rs` 一致性评分段 |

设计取舍：**只装命中的，不装全量的**（原文"懒加载"）。上下文不是越多越好，
装配阶段多塞 4 万 token，代价是每步都更慢更贵，且更容易被无关内容带偏。

### 9.2 对话共享记忆策略

| 作用域 | 读可见性 | 写权限 | 用途 |
| --- | --- | --- | --- |
| `private` | 仅 `owner_id == user.id` | 本人（普通 agent 可代为写入） | 个人偏好、私有事实 |
| `shared` | 全部已认证用户 | 需 `MemoryWriteShared` | 协作约定、跨 agent 共享经验 |
| `system` | 全部已认证用户 | 需 `MemoryWriteSystem`（管理员级） | 全局规则、组织级知识 |

生命周期三处咬合：

1. **任务创建时读**：`assemble()` 按目标相关性取前 5 条，结果作为 `SA_CONTEXT_SUMMARY` 交给执行体。
2. **任务成功后写**：`executor.rs` 的 `record_flywheel()` 把成功任务的可复用经验写回（原文"数据飞轮"）。
3. **被用到就加权**：装配命中即 `access_count += 1`，下一次同主题召回排序更靠前——系统自己越用越顺手。

两条硬约束：

- **可见性差异是权限问题，不是参数问题**：无权限写 `system` 返回 `403`，不会返回"已保存但未生效"。
- **记忆服务异常不拖垮任务**：任务继续执行，但标记 `degraded` 并在结果里声明"未使用历史上下文"——
  宁可少用记忆，也不假装用过（`docs/02` C10 降级口径）。

