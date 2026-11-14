//! 任务接口：提交 → 路由 → 编排 → 执行 → 返回可观测结果。
//!
//! # 两种执行模式
//!
//! - `mode=async`（默认）：立即返回 202 + 任务快照，后台线程池执行，可随时 `POST /tasks/{id}/cancel`。
//! - `mode=sync`（显式请求）：提交即跑完再返回 201，脚本/curl 友好，但调用方要自己扛超时。
//!
//! 为什么缺省必须是 async：接上真实执行后端（`SA_RUNNER=process`）后单步可能跑十几秒，
//! 同步模式会让 HTTP 连接先超时断开，前端拿不到结果、用户也无法叫停。
//! 两种模式共用同一条治理链路（鉴权 / 预算 / 重试 / 熔断 / 审计 / 指标），
//! 差别只有两点：async 会在每个步骤边界读一次取消旗子、并在真实执行前先落库一条 `queued`。

use crate::api::jsons::{normalize_keyword, ApiJson, ApiPath, ApiQuery, window_checked};
use crate::api::extract::{AuthUser, RequestMeta};
use crate::domain::task::{ModelProfile, TaskRecord, TaskStatus, TaskType, TaskView, TraceEntry};
use crate::domain::user::User;
use crate::orchestration::cancel::Cancellations;
use crate::orchestration::context::AssembledContext;
use crate::error::{validation_error, ApiError};
use crate::orchestration::{context, routing};
use crate::security::ratelimit::Quota;
use crate::security::rbac::{Permission, Role};
use crate::state::SharedState;
use axum::extract::{State};
use axum::http::header::{HeaderValue, LOCATION};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use chrono::Utc;
use serde::{Deserialize, Serialize};
use std::sync::Arc;

/// 任务提交限流：每用户每小时 60 次（令牌桶，capacity=60，按 60/h 匀速回填）。
///
/// 选择"按用户"而非"按 IP"：任务提交是已认证写操作，成本与账号绑定；
/// 且 `POST /tasks` 会真实消耗预算与模型配额（异步后台执行同样消耗，只是发生在受理之后），
/// 必须防单账号刷爆。
pub const TASK_SUBMIT_QUOTA: Quota = Quota::new(60.0, 60.0 / 3600.0);

#[derive(Debug, Deserialize)]
pub struct CreateTaskInput {
    pub goal: String,
    #[serde(default)]
    pub task_type: Option<String>,
    #[serde(default)]
    pub model_profile: Option<String>,
    #[serde(default)]
    pub budget_tokens: Option<u64>,
    /// 执行模式：`sync`（默认，同步返回结果）或 `async`（202 + 后台执行 + 可取消）
    #[serde(default)]
    pub mode: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct TaskListQuery {
    /// 按状态过滤
    #[serde(default)]
    pub status: Option<String>,
    /// 按任务类型过滤
    #[serde(default)]
    pub task_type: Option<String>,
    /// 关键词，匹配目标描述
    #[serde(default)]
    pub q: Option<String>,

    #[serde(default)]
    pub limit: Option<usize>,
    #[serde(default)]
    pub offset: Option<usize>,
}
/// 关键词长度上限（字符数）：够用且挡住超长关键词放大。
pub const MAX_KEYWORD_CHARS: usize = 200;

#[derive(Debug, Serialize)]
pub struct TaskDetail {
    #[serde(flatten)]
    pub task: TaskView,
    pub trace: Vec<TraceEntry>,
    pub memory_refs: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct TaskList {
    /// 过滤后的全量条数（不受分页影响）
    pub total: usize,
    /// 本页实际条数
    pub count: usize,
    pub limit: usize,
    pub offset: usize,
    pub has_more: bool,
    pub items: Vec<TaskView>,
}

impl TaskList {
    fn new(items: Vec<TaskView>, total: usize, limit: usize, offset: usize) -> Self {
        let count = items.len();
        Self {
            total,
            count,
            limit,
            offset,
            has_more: offset + count < total,
            items,
        }
    }
}

fn parse_model(raw: &str) -> Option<ModelProfile> {
    match raw.to_ascii_lowercase().as_str() {
        "fast" => Some(ModelProfile::Fast),
        "standard" => Some(ModelProfile::Standard),
        "deep" => Some(ModelProfile::Deep),
        _ => None,
    }
}

fn detail_of(task: &TaskRecord) -> TaskDetail {
    TaskDetail {
        task: TaskView::from(task),
        trace: task.trace.clone(),
        memory_refs: task.memory_refs.clone(),
    }
}

/// 执行模式。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Mode {
    /// 同步：提交即跑完再返回 201 + 终态快照。**需要显式 `mode=sync`**。
    ///
    /// 为什么不把同步当缺省：同步的语义是"这次请求必须等到终态"，而慢模型
    /// 单步可达分钟级，常规客户端 30s 就超时——任务在服务端跑得好好的，
    /// 客户端却把超时当成失败。把"等结果"的代价让所有调用方默认承担，
    /// 是把一个本可避免的误判做成常态。因此同步降级为显式请求，
    /// 谁要"必须拿到终态"谁写 `mode=sync`，由调用方自己承担等价的等待。
    Sync,
    /// 异步：立即返回 202 + 初始快照，后台执行，进度走 SSE，可取消。**缺省模式**。
    ///
    /// 为什么缺省是它：契约（`openapi.json` 的 202、`types.generated.ts`、
    /// 文档 04/40）一直声明"缺省 async"。缺省异步后，任何调用方都不会
    /// 因为默认选择而超时误判；轻量场景想要同步终态，显式写 `mode=sync` 即可。
    /// 这一反转连带要求全部既有门禁脚本显式声明 `mode=sync`，
    /// 避免它们按"缺省=同步"的旧假设静默拿到 202 + `queued`。
    Async,
}

/// 单进程内同时执行的异步任务上限。
///
/// 超过即 429 而不是无限排队：个人部署算力有限，堆积只会让每条任务都变慢，
/// 而且用户看不到"排到第几位"，不如直接说"队列满了，稍后再来"。
pub const ASYNC_MAX_RUNNING: usize = 8;

impl Mode {
    /// 解析执行模式。**缺省是 `async`**：契约这么写，也是唯一不会让客户端
    /// 因默认选择而超时的取值。同步必须显式请求。
    fn parse(raw: Option<&str>) -> Result<Mode, ApiError> {
        match raw.map(str::trim).filter(|s| !s.is_empty()) {
            // 缺省异步：与 `openapi.json`、`types.generated.ts`、文档 04/40 一致。
            // 想"提交即拿终态"的调用方显式写 `mode=sync`（并自行设足客户端超时）。
            None => Ok(Mode::Async),
            Some(s) => match s.to_ascii_lowercase().as_str() {
                "sync" => Ok(Mode::Sync),
                "async" => Ok(Mode::Async),
                _ => Err(validation_error(vec![("mode", "可选 sync / async".into())])),
            },
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Mode::Sync => "sync",
            Mode::Async => "async",
        }
    }
}

/// 取消旗子的清理卫兵。
///
/// 后台执行体无论是正常结束还是 panic，都必须把旗子摘掉：否则
/// `cancellations.active()` 会永久偏高，后续请求会被误判成"队列已满"，
/// 取消接口也会误以为"还有东西在跑"。
struct CancelGuard {
    reg: Arc<Cancellations>,
    id: String,
}

impl Drop for CancelGuard {
    fn drop(&mut self) {
        self.reg.finish(&self.id);
    }
}

/// 校验 / 路由 / 规划 / 上下文 / 预算 都已通过、且已落库为 `queued` 的任务。
struct Prepared {
    owner: User,
    task: TaskRecord,
    ctx: AssembledContext,
}

/// 受理请求的共同前置：任何一条校验失败都必须在落库之前返回，不留半成品。
fn prepare(
    state: &SharedState,
    user: &AuthUser,
    meta: &RequestMeta,
    body: &CreateTaskInput,
) -> Result<Prepared, ApiError> {
    let goal = body.goal.trim().to_string();
    let mut errs: Vec<(&'static str, String)> = Vec::new();
    let goal_len = goal.chars().count();
    if goal_len < 4 {
        errs.push(("goal", "任务目标至少 4 个字符".into()));
    }
    if goal_len > 2000 {
        errs.push(("goal", "任务目标最多 2000 个字符".into()));
    }

    let task_type = match body
        .task_type
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
    {
        None => None,
        Some(raw) => match TaskType::parse(raw) {
            Some(t) => Some(t),
            None => {
                errs.push((
                    "task_type",
                    "可选 coding / writing / research / planning / ideation / collaboration".into(),
                ));
                None
            }
        },
    };

    let model = match body
        .model_profile
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
    {
        None => None,
        Some(raw) => match parse_model(raw) {
            Some(m) => Some(m),
            None => {
                errs.push(("model_profile", "可选 fast / standard / deep".into()));
                None
            }
        },
    };

    if !errs.is_empty() {
        return Err(validation_error(errs));
    }

    // 1) 路由：显式类型优先，缺失时按关键词打分
    // 打分打不到任何信号时**不猜**——猜错的代价比拒一次高得多（会把检索类任务
    // 交给写作 agent，产出一篇正确的废话）。所以这里拒绝，但必须把"为什么拒"
    // 和"怎么办"说清楚，否则用户只会看到一句无从下手的 422。
    let Some(routing_outcome) = routing::classify(task_type, &goal) else {
        return Err(ApiError::Unprocessable(format!(
            "没能从 goal 里识别出任务类型：当前 goal 未命中任何类型的关键词信号，\
             系统不会替你猜（猜错会把任务交给不合适的 agent）。两条出路：\
             ① 在 task_type 中显式指定 coding / writing / research / planning / ideation / collaboration 之一；\
             ② 或在 goal 里写清「产出什么」与「落在哪个领域」，例如「写一份季度复盘报告」「排查这段 Rust 代码的空指针」。\
             本次 goal 长度 {} 字，未命中类型关键词。",
            goal.chars().count()
        )));
    };

    // 2) 规划：超出能力边界直接拒绝，不做静默降级
    let bundle = match routing::build_plan(&goal, &routing_outcome, model) {
        routing::PlanOutcome::Ok(bundle) => bundle,
        routing::PlanOutcome::OutOfScope { reason, suggestion } => {
            state.metrics.inc("sa_tasks_out_of_scope_total");
            state.audit.record(
                &user.id,
                "task.rejected_out_of_scope",
                &user.id,
                false,
                &reason,
                &meta.request_id,
            );
            return Err(ApiError::Unprocessable(format!(
                "超出能力边界：{reason}。建议：{suggestion}"
            )));
        }
    };

    let owner = state
        .store
        .read(|db| db.users.get(&user.id).cloned())
        .ok_or_else(|| ApiError::Unauthorized("账号不存在".into()))?;

    // 3) 上下文装配（共享记忆 + 裁剪）：先于预算计算，注入成本要计入默认预算
    let ctx = context::assemble(state.store.as_ref(), &owner, &goal);

    // 4) 预算：默认 = 规划估算（已含 1.2 余量）+ 上下文注入成本；显式值仍是硬上限
    let cap = state.config.task_budget_tokens.saturating_mul(4).max(1_000);
    let budget = match body.budget_tokens {
        Some(b) if b < 200 => {
            return Err(validation_error(vec![("budget_tokens", "预算过低（最少 200）".into())]))
        }
        Some(b) if b > cap => {
            return Err(validation_error(vec![(
                "budget_tokens",
                format!("超出单任务上限 {cap}"),
            )]))
        }
        Some(b) => b,
        None => bundle
            .budget_tokens
            .saturating_add(ctx.used_tokens)
            .min(cap),
    };

    let now = Utc::now();
    let task = TaskRecord {
        id: uuid::Uuid::new_v4().to_string(),
        owner_id: user.id.clone(),
        goal: goal.clone(),
        task_type: bundle.routing.task_type,
        requested_model: model,
        status: TaskStatus::Queued,
        plan: bundle.steps.clone(),
        results: Vec::new(),
        trace: Vec::new(),
        result_summary: String::new(),
        tokens_used: 0,
        budget_tokens: budget,
        created_at: now,
        updated_at: now,
        finished_at: None,
        // 构造时就带上引用，不再"先落库再回写"：异步模式下后台线程读到的是同一条快照
        memory_refs: ctx.memory_refs.clone(),
        agreement_score: bundle.planned_confidence,
    };
    persist(state, &task);
    // 建单成功：`sa_tasks_created_total` 此前只在能力规格里被引用（automation 能力的
    // observability_signals），实际零落点。放在 `prepare` 的落库之后、而不是 handler 里，
    // 是因为 sync / async 两条分支都要走过这里，而 429 / 422 / 403 的拒绝路径都在
    // 落库**之前**返回——于是这个数字天然等价于"真正建出来的任务数"，
    // 能与 `/tasks` 台账条数对账（探针就是这么验的）。
    state.metrics.inc("sa_tasks_created_total");
    // 计划步数：`sa_task_plan_steps` 同样零落点。用 inc_by 累计而非 gauge，
    // 因为要看的是"平均编排长度"（总计 ÷ 建单数），不是"此刻某个任务多长"。
    // 它在建单时就把**计划**步数记下、不等执行完，编排质量才能在长任务跑完前就被观察。
    state
        .metrics
        .inc_by("sa_task_plan_steps", "", task.plan.len() as u64);

    // 懒加载究竟省了多少上下文：`sa_context_tokens_saved_total` 此前零落点。
    // 取值来自 `context::assemble` 里的"全量可见记忆成本 - 实际装入成本"，
    // 是个能被任何人拿同一份 store 复算出来的差值（不是拍脑袋的估算系数）。
    // 记在建单点而不是装配点，是因为它是"这次任务省下的"，和任务一一对应。
    if ctx.tokens_saved > 0 {
        state
            .metrics
            .inc_by("sa_context_tokens_saved_total", "", ctx.tokens_saved);
    }

    // 实际装配进来的 agent 上下文数：`sa_agents_loaded_total` 也一直是零落点。
    //
    // 口径取"计划里去重后的 agent 个数"，而不是"catalog 里的全部 agent 数"——
    // 后者是个常数，累加起来毫无信息量；前者才反映懒加载的本意：
    // 一个任务真正惊动了几个 agent。`index` 开头那步固定是 super-agent 自己，
    // 也照样计入，因为它的上下文确实被装进来了。
    let mut loaded_agents: Vec<&str> = task.plan.iter().map(|s| s.agent_id.as_str()).collect();
    loaded_agents.sort_unstable();
    loaded_agents.dedup();
    state
        .metrics
        .inc_by("sa_agents_loaded_total", "", loaded_agents.len() as u64);

    // 模型档位分布：`sa_model_profile_used_total` 此前零落点。
    // 带 profile 标签而不是只记总数，因为这张表唯一有用的问法是
    // "deep 档被用了多少次"——它直接对应 token 成本，而无标签的总数做不到这件事。
    // 未显式指定时记 `auto`：这是路由器的实际决策来源，不能混进 standard。
    let profile_label = match task.requested_model {
        Some(m) => format!("profile=\"{}\"", m.as_str()),
        None => "profile=\"auto\"".to_string(),
    };
    state.metrics.inc_by("sa_model_profile_used_total", &profile_label, 1);

    Ok(Prepared { owner, task, ctx })
}

/// 把执行结果写回任务记录（同步 / 异步两条路径共用，保证口径一致）。
fn finish_common(
    state: &SharedState,
    task: &mut TaskRecord,
    outcome: &crate::orchestration::executor::ExecOutcome,
) {
    if outcome.status == TaskStatus::AbortedBudget {
        state.metrics.inc("sa_tasks_aborted_budget_total");
    }
    // 真实 token 消耗累计（`task.tokens_used` 由执行器在 run() 内回写，此处已是终值）。
    //
    // 为什么不在执行器每步里 inc：单步 inc 会把"并行步"的边界打散，且预算按层归并、
    // 中途降档，逐段相加与实际结算值可能差一截。以任务终态的 `tokens_used` 为准，
    // 才能和 `/tasks/{id}` 里那个数字**逐位相等**——对账口径一致比采样及时重要。
    if task.tokens_used > 0 {
        state
            .metrics
            .inc_by("sa_tokens_used_total", "", task.tokens_used);
    }
    if let Some(reason) = &outcome.reason {
        task.result_summary = format!("{}\n[未被采纳的原因] {reason}", task.result_summary);
    }
    task.updated_at = Utc::now();
}

/// 落库最终快照。单独抽出来是因为同步、异步、拒绝、取消四条路径都要写一次。
///
/// 这里是任务状态的**唯一出口**，所以事件广播也挂在这里：
/// 任何一次状态变化，只要能落库，就一定能被 SSE 订阅者看到；
/// 反过来，没走这里的改动对前端而言等于没发生——这条不变量让"漏推"不可能静默发生。
fn persist(state: &SharedState, task: &TaskRecord) {
    let snap = task.clone();
    state
        .store
        .write(|db| db.tasks.insert(snap.id.clone(), snap));
    // 所有任务落库（提交、受理、终态）都走这一个出口，写穿也只需要挂在这里：
    // 用户在界面上已经看到"任务已提交"，就不该因为进程被杀而在重启后变成 404。
    state.persist_now();
    // 事件在落库之后广播：保证订阅者收到事件时回查详情一定读得到同一状态，
    // 不会出现"事件说 succeeded，详情还是 running"的撕裂。
    state.events.publish(task);
}

/// 把任务快照组装成带"分支自证"响应头的响应。
///
/// 两个头都不改 JSON 形状（因此不牵动任何契约门禁），但解决两个真实的排障痛点：
/// - `X-SA-Exec-Mode`：调用方**究竟走了哪条分支**一目了然。缺省值从 sync 反转成 async
///   之后，最怕的就是"以为自己要的是同步，实际拿到 202"——埋点、日志、网关重放
///   都靠这枚头对齐口径，不必再从状态码反推。
/// - `Location`：202 的语义是"已受理，进展在别处看"，按 RFC 9110 就该指向状态资源，
///   调用方无需自己拼 `/api/v1/tasks/{id}`。
fn task_response(status: StatusCode, task: &TaskRecord, mode: Mode) -> Response {
    let mut headers = HeaderMap::new();
    headers.insert("x-sa-exec-mode", HeaderValue::from_static(mode.as_str()));
    if let Ok(v) = HeaderValue::from_str(&format!("/api/v1/tasks/{}", task.id)) {
        headers.insert(LOCATION, v);
    }
    (status, headers, Json(detail_of(task))).into_response()
}

/// `POST /api/v1/tasks` — 提交任务。
///
/// `mode` 缺省为 `async`：立即返回 202 并在后台执行（进度看 `GET /tasks/{id}/events`）；
/// `mode=sync` 则同步跑完返回 201。响应头 `X-SA-Exec-Mode` 指明本次实际走的分支。
pub async fn create(
    State(state): State<SharedState>,
    user: AuthUser,
    meta: RequestMeta,
    ApiJson(body): ApiJson<CreateTaskInput>,
) -> Result<Response, ApiError> {
    user.require(Permission::Write)?;

    // 0) 提交限流：按用户维度 60 次/小时；超限返回 429 + Retry-After，且不计入审计拒绝
    // 配额取配置（默认 60/h）：限额变了，429 的文案也必须跟着变，
    // 否则用户看到的数字与实际拦截规则不符——那是最难排查的一类"假信息"。
    let per_hour = state.config.task_submit_quota_per_hour;
    let quota = Quota::new(per_hour as f64, per_hour as f64 / 3600.0);
    state
        .limiter
        .check(&format!("task_submit:{}", user.id), quota)
        .map_err(|retry_after| {
            // 两个计数，不是重复：
            // `sa_task_submit_rate_limited_total` 是**本入口**的细分口径（只会因建单限流涨），
            // `sa_rate_limited_total` 是**全局限流总量**（能力规格里承诺的名字，未来登录、
            // 抓取、集群注册的限流都该汇到它这里）。只留细分的话，运维想算"这台机器
            // 一共拦了多少次"就得去逐个入口求和——那正是最容易漏算的地方。
            state.metrics.inc("sa_task_submit_rate_limited_total");
            state.metrics.inc("sa_rate_limited_total");
            ApiError::TooManyRequests {
                message: format!(
                    "任务提交过于频繁：限额为每用户每小时 {per_hour} 次，请在 {retry_after} 秒后重试"
                ),
                retry_after,
            }
        })?;

    let mode = Mode::parse(body.mode.as_deref())?;
    let Prepared {
        owner,
        mut task,
        ctx,
    } = prepare(&state, &user, &meta, &body)?;

    match mode {
        Mode::Sync => {
            // 受治理执行：逐步鉴权、预算降档、重试、熔断、合规、复核
            let outcome = state.exec().run(&mut task, &owner, &ctx, &meta.request_id);
            finish_common(&state, &mut task, &outcome);
            persist(&state, &task);
            Ok(task_response(StatusCode::CREATED, &task, Mode::Sync))
        }
        Mode::Async => {
            // 队列已满：不接受就是拒绝，必须把刚落库的那条 queued 收敛掉，
            // 否则前端会看到一条永远不会动的任务。
            let max_running = state.config.async_max_running;
            if state.cancellations.active() >= max_running {
                task.status = TaskStatus::Cancelled;
                task.finished_at = Some(Utc::now());
                task.updated_at = Utc::now();
                task.result_summary =
                    "任务未被受理：后台执行队列已满，本次提交已作废（未消耗预算）".into();
                task.trace.push(TraceEntry {
                    at: Utc::now(),
                    step_index: 0,
                    agent_id: "-".into(),
                    phase: "dispatch".into(),
                    outcome: "rejected".into(),
                    detail: format!("并发上限 {max_running}，当前已满"),
                    attempt: 0,
                    tokens: 0,
                    duration_ms: None,
                });
                persist(&state, &task);
                state.metrics.inc("sa_task_async_rejected_total");
                return Err(ApiError::TooManyRequests {
                    message: format!(
                        "后台执行队列已满（上限 {max_running} 个并发任务），请稍后重试；已提交内容未消耗任何预算"
                    ),
                    retry_after: 30,
                });
            }

            let flag = state.cancellations.register(&task.id);
            let guard = CancelGuard {
                reg: Arc::clone(&state.cancellations),
                id: task.id.clone(),
            };
            task.result_summary =
                "任务已受理，正在后台执行；请通过 GET /api/v1/tasks/{id} 查询进展。".into();
            persist(&state, &task);
            state.audit.record(
                &user.id,
                "task.accepted_async",
                &task.id,
                true,
                "mode=async",
                &meta.request_id,
            );
            state.metrics.inc("sa_task_async_accepted_total");

            let st = Arc::clone(&state);
            let req_id = meta.request_id.clone();
            let mut bg_task = task.clone();
            let bg_owner = owner.clone();
            let bg_ctx = ctx.clone();
            let bg_flag = flag.clone();
            // spawn_blocking 而不是 spawn：执行引擎是同步阻塞的（含进程调用与退避睡眠），
            // 放进 async 线程池会拖住整个 reactor。
            tokio::task::spawn_blocking(move || {
                let _guard = guard;
                // 先把"正在执行"落库并广播：执行器内部虽然会置 Running，但那是内存态，
                // 不落库就不会有事件，前端只能看到 queued 直接跳到终态——
                // 直播流也就退化成"两个端点"，等于白做。
                bg_task.status = TaskStatus::Running;
                bg_task.updated_at = Utc::now();
                persist(&st, &bg_task);
                let outcome = st
                    .exec_with_cancel(Some(bg_flag.as_ref()))
                    .run(&mut bg_task, &bg_owner, &bg_ctx, &req_id);
                finish_common(&st, &mut bg_task, &outcome);
                persist(&st, &bg_task);
                st.audit.record(
                    &bg_owner.id,
                    "task.finished_async",
                    &bg_task.id,
                    matches!(
                        outcome.status,
                        TaskStatus::Succeeded | TaskStatus::PartiallySucceeded
                    ),
                    outcome.status.as_str(),
                    &req_id,
                );
            });

            Ok(task_response(StatusCode::ACCEPTED, &task, Mode::Async))
        }
    }
}

/// `GET /api/v1/tasks?limit=20` — 仅返回本人任务，按创建时间倒序。
pub async fn list(
    State(state): State<SharedState>,
    user: AuthUser,
    ApiQuery(q): ApiQuery<TaskListQuery>,
) -> Result<Json<TaskList>, ApiError> {
    let (limit, offset) = window_checked(q.limit, q.offset, 20, 100)?;
    // 状态过滤：前端"只看失败任务"这类视图必须能在服务端筛，否则要全量拉回本地过滤
    let status_filter = match q.status.as_deref().map(str::trim).filter(|s| !s.is_empty()) {
        None => None,
        Some(raw) => Some(
            TaskStatus::parse(raw)
                .ok_or_else(|| {
                    validation_error(vec![(
                        "status",
                        format!("可选 {}", TaskStatus::ALL.join(" / ")),
                    )])
                })?
                .as_str(),
        ),
    };
    let type_filter = match q.task_type.as_deref().map(str::trim).filter(|s| !s.is_empty()) {
        None => None,
        Some(raw) => Some(
            TaskType::parse(raw)
                .ok_or_else(|| {
                    validation_error(vec![(
                        "task_type",
                        format!("可选 {}", TaskType::ALL.join(" / ")),
                    )])
                })?
                .as_str(),
        ),
    };
    let keyword = normalize_keyword(q.q.as_deref(), "q", MAX_KEYWORD_CHARS)?;

    let mut items: Vec<TaskView> = state.store.read(|db| {
        db.tasks
            .values()
            .filter(|t| t.owner_id == user.id)
            .filter(|t| status_filter.map(|s| t.status.as_str() == s).unwrap_or(true))
            .filter(|t| type_filter.map(|s| t.task_type.as_str() == s).unwrap_or(true))
            .filter(|t| match &keyword {
                None => true,
                Some(k) => t.goal.to_lowercase().contains(k),
            })
            .map(TaskView::from)
            .collect()
    });
    let total = items.len();
    // 最新在前：列表首屏就是刚提交的那个任务，不用往下翻
    items.sort_by(|a, b| b.created_at.cmp(&a.created_at));
    let items: Vec<TaskView> = items.into_iter().skip(offset).take(limit).collect();
    Ok(Json(TaskList::new(items, total, limit, offset)))
}

/// 读取任务并校验归属。`pub(crate)` 是因为 SSE 进度流（`task_events`）必须
/// 复用**同一套**权限判定，避免"详情要鉴权、长连接不要"的越权缝隙。
pub(crate) fn load_owned(state: &SharedState, user: &AuthUser, id: &str) -> Result<TaskRecord, ApiError> {
    let task = state
        .store
        .read(|db| db.tasks.get(id).cloned())
        .ok_or_else(|| ApiError::NotFound("任务不存在".into()))?;
    if task.owner_id != user.id && user.role.rank() < Role::Admin.rank() {
        return Err(ApiError::Forbidden("无权访问他人的任务".into()));
    }
    Ok(task)
}

/// `GET /api/v1/tasks/{id}`
pub async fn detail(
    State(state): State<SharedState>,
    user: AuthUser,
    ApiPath(id): ApiPath<String>,
) -> Result<Json<TaskDetail>, ApiError> {
    let task = load_owned(&state, &user, &id)?;
    Ok(Json(detail_of(&task)))
}

/// `GET /api/v1/tasks/{id}/trace` — 单独拉取全链路轨迹（用于前端时间线）。
pub async fn trace(
    State(state): State<SharedState>,
    user: AuthUser,
    ApiPath(id): ApiPath<String>,
) -> Result<Json<Vec<TraceEntry>>, ApiError> {
    let task = load_owned(&state, &user, &id)?;
    Ok(Json(task.trace))
}

/// `POST /api/v1/tasks/{id}/cancel` — 请求取消一个未结束的任务。
///
/// 两种真实情况分开处理，绝不假装成功：
/// 1. **有执行体在跑**（异步任务）：立起取消旗子，返回 202，并明确告知
///    "将在下一个步骤边界生效"——执行中的单步无法安全打断，这一点必须讲清楚；
/// 2. **没有执行体**（进程重启前遗留的 queued/running）：它永远不会自己结束，
///    服务端就地收敛为 `cancelled` 并说明原因，返回 200。
///
/// 已结束的任务一律 409：取消一个已经完成的任务没有任何语义，不能返回 200 骗调用方。
pub async fn cancel(
    State(state): State<SharedState>,
    user: AuthUser,
    meta: RequestMeta,
    ApiPath(id): ApiPath<String>,
) -> Result<(StatusCode, Json<TaskDetail>), ApiError> {
    user.require(Permission::Write)?;
    let mut task = load_owned(&state, &user, &id)?;

    if task.status.is_terminal() {
        return Err(ApiError::Conflict(format!(
            "任务已结束（{}），无法取消；如需重跑请重新提交",
            task.status.as_str()
        )));
    }

    let now = Utc::now();
    // TraceEntry.step_index 是 u8：直接饱和转换，避免任务步数多时溢出 panic
    let next_step = u8::try_from(task.results.len()).unwrap_or(u8::MAX);

    if state.cancellations.request(&id) {
        task.trace.push(TraceEntry {
            at: now,
            step_index: next_step,
            agent_id: "-".into(),
            phase: "cancel".into(),
            outcome: "requested".into(),
            detail: "收到取消请求，将在下一个步骤边界生效".into(),
            attempt: 0,
            tokens: 0,
            duration_ms: None,
        });
        task.updated_at = now;
        persist(&state, &task);
        state.audit.record(
            &user.id,
            "task.cancel_requested",
            &id,
            true,
            "mode=async",
            &meta.request_id,
        );
        state.metrics.inc("sa_task_cancel_requests_total");
        return Ok((StatusCode::ACCEPTED, Json(detail_of(&task))));
    }

    // 没有执行体：只能是重启前遗留的任务。就地收敛，避免前端永远显示 running。
    task.status = TaskStatus::Cancelled;
    task.finished_at = Some(now);
    task.updated_at = now;
    task.result_summary =
        "任务已被取消：服务端未发现对应的执行体（通常意味着服务重启时该任务被中断）。".into();
    task.trace.push(TraceEntry {
        at: now,
        step_index: next_step,
        agent_id: "-".into(),
        phase: "cancel".into(),
        outcome: "cancelled".into(),
        detail: "无执行体，服务端就地收敛为已取消".into(),
        attempt: 0,
        tokens: 0,
        duration_ms: None,
    });
    persist(&state, &task);
    state.audit.record(
        &user.id,
        "task.cancel_reconciled",
        &id,
        true,
        "no runner attached",
        &meta.request_id,
    );
    state.metrics.inc("sa_task_cancel_reconciled_total");
    Ok((StatusCode::OK, Json(detail_of(&task))))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 缺省值即契约：openapi / TS 类型 / 文档 04/40 都写"缺省 async"。
    /// 谁把它改回 sync，谁就让默认调用方重新背上"30s 超时误判"，此测试锁死。
    #[test]
    fn mode_default_is_async() {
        assert_eq!(Mode::parse(None).unwrap(), Mode::Async);
        assert_eq!(Mode::parse(Some("")).unwrap(), Mode::Async);
        assert_eq!(Mode::parse(Some("   ")).unwrap(), Mode::Async);
    }

    #[test]
    fn mode_explicit_values_are_respected() {
        assert_eq!(Mode::parse(Some("sync")).unwrap(), Mode::Sync);
        assert_eq!(Mode::parse(Some("SYNC")).unwrap(), Mode::Sync);
        assert_eq!(Mode::parse(Some("async")).unwrap(), Mode::Async);
        assert_eq!(Mode::parse(Some("AsYnC")).unwrap(), Mode::Async);
        assert!(Mode::parse(Some("blocking")).is_err(), "非法值必须被拒绝");
    }

    #[test]
    fn mode_as_str_roundtrip() {
        assert_eq!(Mode::Sync.as_str(), "sync");
        assert_eq!(Mode::Async.as_str(), "async");
    }
}
