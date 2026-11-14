//! 可观测接口：探活、就绪、Prometheus 指标、审计流水、运行概览。

use crate::api::jsons::{ApiQuery, window_checked};
use crate::api::extract::{AuthUser, MetricsAuth};
use crate::domain::agent::AGENTS;
use crate::domain::capability::CAPABILITIES;
use crate::error::{validation_error, ApiError};
use crate::obs::alert::{Alert, AlertStats, AutoAction};
use crate::obs::audit::AuditEvent;
use crate::security::rbac::Permission;
use crate::state::SharedState;
use axum::extract::{State};
use axum::http::{header, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::collections::BTreeMap;

#[derive(Debug, Deserialize)]
pub struct AuditQuery {
    #[serde(default)]
    pub actor: Option<String>,
    /// 只允许 allow / deny：安全排查时一眼看出谁被拦了
    #[serde(default)]
    pub decision: Option<String>,
    #[serde(default)]
    pub limit: Option<usize>,
    #[serde(default)]
    pub offset: Option<usize>,
}

#[derive(Debug, Serialize)]
pub struct AuditList {
    /// 过滤后的全量条数
    pub total: usize,
    /// 本页实际条数
    pub count: usize,
    pub limit: usize,
    pub offset: usize,
    pub has_more: bool,
    /// 未过滤的拒绝总数（安全看板用）
    pub denied: u64,
    pub items: Vec<AuditEvent>,
}

#[derive(Debug, Serialize)]
pub struct Overview {
    pub version: &'static str,
    pub env: String,
    /// 当前存储后端（可插拔部件，运维核对用）
    pub storage: &'static str,
    /// 该后端是否落盘（false 表示进程退出即丢数据）
    pub storage_durable: bool,
    pub uptime_secs: u64,
    pub agents_total: usize,
    pub capabilities_total: usize,
    pub audit_denied: u64,
    /// 当前审计出口（可插拔部件，运维核对用）：memory / file
    pub audit_backend: &'static str,
    /// 当前执行后端（可插拔部件，运维核对用）：sim / process / 其它自定义后端
    pub runner_backend: &'static str,
    /// 当前限流后端（可插拔部件）：memory / noop
    pub limiter_backend: &'static str,
    /// 限流是否真的生效（noop 时为 false，别让"看起来有限流"骗人）
    pub limiter_enforcing: bool,
    /// 当前指标导出部件：prometheus / json / noop
    pub metrics_export: &'static str,
    pub store: BTreeMap<String, u64>,
}

/// `GET /healthz` — 仅表示进程存活，不触达存储。
pub async fn health() -> impl IntoResponse {
    Json(json!({
        "status": "ok",
        "version": env!("CARGO_PKG_VERSION"),
        "service": "personal-super-agents",
    }))
}

/// `GET /readyz` — 存储可读且数据目录可访问才返回 200。
///
/// 同时如实体报告审计的真实状态（`durable` / `degraded`）：
/// "存储就绪、审计却写不进去"的实例，运维必须能在这一个端点上同时看到，
/// 而不是查完 readyz 再去翻日志。
///
/// **审计降级不改变 HTTP 状态码**，这是刻意的：审计失败不得阻断业务（端口契约如此），
/// 把它变成 503 会让负载均衡摘掉一台其实还能正常干活的机器。
/// 告警走日志与看板，不该由就绪探针代替。
pub async fn ready(State(state): State<SharedState>) -> Response {
    let storage_ok = state
        .store
        .read(|db| db.schema_version);
    let dir_ok = state.config.data_dir.exists();
    let audit_degraded = state.audit.degraded();
    let body = json!({
        "ready": dir_ok,
        "schema_version": storage_ok,
        "storage": state.store.kind().as_str(),
        "storage_durable": state.store.kind().is_durable(),
        "data_dir": state.config.data_dir.display().to_string(),
        "audit": {
            "backend": state.audit.kind(),
            "durable": state.audit.durable(),
            "degraded": audit_degraded.is_some(),
            "degraded_reason": audit_degraded,
        },
    });
    let code = if dir_ok {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    };
    (code, Json(body)).into_response()
}

/// `GET /metrics` — Prometheus 文本格式。
/// 鉴权：静态抓取令牌（SA_METRICS_TOKEN，推荐给 Prometheus）或管理员 access token。
pub async fn metrics(
    State(state): State<SharedState>,
    _auth: MetricsAuth,
) -> Result<Response, ApiError> {
    // 事件总线的两个数字，真值在总线自己的原子变量里，不在导出器里。
    // 抓取前对齐一次绝对值（而不是累加），否则每抓一次就重复计一遍。
    //
    // 为什么要报这个：广播是"满即丢、绝不阻塞发布者"的，所以 SSE 慢消费者丢事件是
    // **设计内的正常行为**——但看不见就等于没有。这里把它摆在 /metrics 上，
    // 前端"偶发跳帧"时能一眼分清是网络断，还是服务端已经丢帧了。
    state.metrics.counter_set(
        "sa_sse_events_dropped_total",
        "",
        state.events.dropped(),
    );
    state
        .metrics
        .gauge_set("sa_sse_subscribers", "", state.events.subscribers() as f64);
    // 环形台账的当前长度：它同时是"断线重连最多能补回多少帧"的上限。
    // 台账满（=512）而 sa_sse_replay_gap_total 在涨，说明有人断得太久，补不齐了。
    state
        .metrics
        .gauge_set("sa_sse_history", "", state.events.history_len() as f64);

    // 审计被容量挤掉的累计条数：真值在审计出口自己的原子变量里，
    // 与上面那条 SSE 丢帧同理——抓取时对齐绝对值，绝不能累加（会按抓取次数虚涨）。
    //
    // 为什么要报：审计是有界环形 + 溢出去最旧，容量满了以后历史被静默覆盖。
    // 对合规场景这是实质风险（要查的那条可能刚好被挤掉）。这个数一涨，
    // 结论很明确：调大 SA_AUDIT_CAPACITY，或者该接外部归档了。
    state
        .metrics
        .counter_set("sa_audit_dropped_total", "", state.audit.dropped());

    // 集群：节点数 + 任务分布。抓取时按**绝对值**对齐（counter_set 是覆盖不是累加）——
    // 任务状态是"此刻快照"，累加会把每个任务数成抓取次数那么多遍。
    //
    // 为什么把任务分布也放这里：/cluster 要 admin 权限才能读，
    // 而 Prometheus 抓的是 /metrics 的 Bearer token，没有用户身份。
    // 不放这一组，告警规则就没法对"排队堆积""失败率抬头"直接设阈值。
    let tasks = state
        .store
        .read(|db| crate::domain::cluster::ClusterTasks::count(db.tasks.values()));
    state.metrics.counter_set("sa_cluster_nodes", "", 1);
    for (status, value) in [
        ("total", tasks.total),
        ("queued", tasks.queued),
        ("running", tasks.running),
        ("succeeded", tasks.succeeded),
        ("partially_succeeded", tasks.partially_succeeded),
        ("failed", tasks.failed),
        ("cancelled", tasks.cancelled),
        ("aborted_budget", tasks.aborted_budget),
    ] {
        state.metrics.counter_set(
            "sa_cluster_tasks",
            &format!("status=\"{status}\""),
            value,
        );
    }

    // Content-Type 由导出器自报：装 json 导出器时这里自动变成 application/json，
    // handler 不需要认识具体实现（换部件零改动）。
    Ok((
        [(header::CONTENT_TYPE, state.metrics.content_type())],
        state.metrics.render(),
    )
        .into_response())
}

/// `GET /api/v1/observability/audit?limit=50&actor=<id>`
pub async fn audit(
    State(state): State<SharedState>,
    user: AuthUser,
    ApiQuery(q): ApiQuery<AuditQuery>,
) -> Result<Json<AuditList>, ApiError> {
    user.require(Permission::Admin)?;
    let (limit, offset) = window_checked(q.limit, q.offset, 50, 500)?;
    // 与任务/记忆列表口径统一：非法枚举值必须 422 显式报错，不能静默返回空列表骗人
    let decision = match q.decision.as_deref().map(str::trim).filter(|s| !s.is_empty()) {
        None => None,
        Some(raw) => match raw {
            "allow" | "deny" => Some(raw),
            _ => {
                return Err(validation_error(vec![("decision", "可选 allow / deny".to_string())]));
            }
        },
    };
    let (items, total) = state
        .audit
        .page(limit, offset, q.actor.as_deref(), decision);
    let count = items.len();
    Ok(Json(AuditList {
        total,
        count,
        limit,
        offset,
        has_more: offset + count < total,
        denied: state.audit.denied_count(),
        items,
    }))
}

/// 部件清单响应。
#[derive(Debug, Serialize)]
pub struct ComponentList {
    pub count: usize,
    /// 提示：换部件后需重启进程生效（当前不支持热插拔，如实告知）
    pub restart_required_for_change: bool,
    pub items: Vec<crate::ports::ComponentInfo>,
}

/// `GET /api/v1/observability/components` — 当前装了哪些可插拔部件。
///
/// 回答三个运维最常问的问题：跑的是哪个实现、还能换什么、换完要不要重启。
pub async fn components(
    State(state): State<SharedState>,
    user: AuthUser,
) -> Result<Json<ComponentList>, ApiError> {
    user.require(Permission::Admin)?;
    let items = state.components();
    Ok(Json(ComponentList {
        count: items.len(),
        restart_required_for_change: true,
        items,
    }))
}

/// `GET /api/v1/observability/overview`
pub async fn overview(
    State(state): State<SharedState>,
    user: AuthUser,
) -> Result<Json<Overview>, ApiError> {
    user.require(Permission::Admin)?;
    let store = state.store.read(|db| db.stats());
    Ok(Json(Overview {
        version: env!("CARGO_PKG_VERSION"),
        env: state.config.env.clone(),
        storage: state.store.kind().as_str(),
        storage_durable: state.store.kind().is_durable(),
        uptime_secs: state.metrics.uptime_secs(),
        agents_total: AGENTS.len(),
        capabilities_total: CAPABILITIES.len(),
        audit_denied: state.audit.denied_count(),
        audit_backend: state.audit.kind(),
        runner_backend: state
            .runner
            .as_deref()
            .map(|r| r.backend())
            .unwrap_or("sim"),
        limiter_backend: state.limiter.kind(),
        limiter_enforcing: state.limiter.enforces(),
        metrics_export: state.metrics.kind(),
        store,
    }))
}

/// 告警响应。
///
/// 字段全部必现（无 `Option`、无 `skip_serializing_if`）：门禁 14 的形状断言
/// 与前端类型都靠"字段恒定"才敢硬读，少一个字段就是一次线上白屏。
#[derive(Debug, Serialize)]
pub struct AlertList {
    /// 当前活跃告警条数（= `active.len()`，冗余但省去前端再数一遍）
    pub count: usize,
    /// 活跃告警（按 severity 倒序、同级按 rule 升序，输出稳定）
    pub active: Vec<Alert>,
    /// 本次抓取触发了什么：新发出 / 被去重压下 / 被风暴折叠
    pub evaluated: EvaluatedNow,
    /// 累计口径
    pub stats: AlertStats,
    /// 此刻由哪个既有机制承担自动处置（告警本身不处置，见 docs/85）
    pub auto_action: AutoAction,
    /// 生效阈值（运维核对"为什么没报"的第一现场）
    pub policy: AlertPolicyView,
    /// 规则身份证：id + 级别 + 观测来源，方便前端做图例、值班做交接
    pub rules: Vec<RuleView>,
}

#[derive(Debug, Serialize)]
pub struct EvaluatedNow {
    pub fired: usize,
    /// 本次被去重压下的规则 id
    pub deduped: Vec<&'static str>,
    /// 本次被风暴折叠掉的条数
    pub aggregated: u64,
}

#[derive(Debug, Serialize)]
pub struct AlertPolicyView {
    pub dedup_window_secs: u64,
    pub storm_threshold: usize,
    pub http_5xx_ratio: f64,
    pub min_http_requests: u64,
    pub task_failure_ratio: f64,
    pub min_tasks: u64,
    pub rate_limited_hits: u64,
    pub authz_denied_hits: u64,
    pub circuit_open_hits: u64,
}

#[derive(Debug, Serialize)]
pub struct RuleView {
    pub id: &'static str,
    pub title: &'static str,
    pub severity: &'static str,
    pub observed_from: &'static str,
}

/// `GET /api/v1/observability/alerts` — 阈值告警。
///
/// # 为什么是"抓取即评估"，而不是后台每秒扫一遍
///
/// 后台循环需要一个"多久扫一次"的旋钮，而这个旋钮没有正确答案：扫太密是白烧 CPU，
/// 扫太疏则告警延迟不可控。反过来，只有人会来读告警（看板抓取 / 值班点开），
/// 让**读**这个动作本身就是评估触发点，旋钮自然消失。
///
/// 代价要说清楚：没有人抓取的时候不会告警。这在本产品的形态下不构成风险——
/// 告警的目的是"人在看的时候别把真正重要的那条淹没"，而不是做实时告警平台。
/// 真正需要"无人值守也要报"的场景，应当由外部 Prometheus 对 `/metrics` 设规则
/// （`sa_alerts_total` 的原始计数器一直在，抓不抓都不丢）。
///
/// # 幂等性
///
/// 同一进程内，5 分钟窗口内重复抓取**只发一条**；重复抓取本身是安全的，
/// 它只会让 `evaluated.deduped` 变长、`stats.suppressed_total` 增长。
pub async fn alerts(
    State(state): State<SharedState>,
    user: AuthUser,
) -> Result<Json<AlertList>, ApiError> {
    // 告警里含规则阈值与内部信号，属运维信息，按 Admin 收敛（与 overview 同权）
    user.require(Permission::Admin)?;

    // 评估前先取一次快照：判定与打点必须基于同一份读数，
    // 否则"比值算出来 21%，报的明细却是 19%"这种自相矛盾会在页面上出现。
    let snap = state.metrics.snapshot();
    let evaluated = state.alerts.evaluate(&snap, state.metrics.as_ref(), std::time::Instant::now());
    let active = state.alerts.active();
    let p = state.alerts.policy();
    Ok(Json(AlertList {
        count: active.len(),
        active,
        evaluated: EvaluatedNow {
            fired: evaluated.alerts.len(),
            deduped: evaluated.deduped,
            aggregated: evaluated.aggregated,
        },
        stats: state.alerts.stats(),
        auto_action: state.alerts.auto_action(&snap),
        policy: AlertPolicyView {
            dedup_window_secs: p.dedup_secs,
            storm_threshold: p.storm_threshold,
            http_5xx_ratio: p.http_5xx_ratio,
            min_http_requests: p.min_http_requests,
            task_failure_ratio: p.task_failure_ratio,
            min_tasks: p.min_tasks,
            rate_limited_hits: p.rate_limited_hits,
            authz_denied_hits: p.authz_denied_hits,
            circuit_open_hits: p.circuit_open_hits,
        },
        rules: crate::obs::alert::RULES
            .iter()
            .map(|r| RuleView {
                id: r.id,
                title: r.title,
                severity: r.severity.as_str(),
                observed_from: r.observed_from,
            })
            .collect(),
    }))
}
