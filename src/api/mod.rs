//! HTTP 层装配：路由、CORS、请求体上限、请求号、统一兜底。
//!
//! 装配顺序（由外到内）决定了三件事的可靠性，改动前请先读注释：
//! 1. `SetRequestIdLayer` 最外层：**每个**请求都会拿到 request id（含 404 / 500）；
//! 2. `PropagateRequestIdLayer` 在它内侧：把同一个 id 写回响应头，前端报错能带着它来对账；
//! 3. `TraceLayer` 在两者内侧：日志里的 request_id 与响应头、审计记录三者一致。

pub mod extract;
pub mod handlers;
pub mod jsons;

use crate::config::Config;
use crate::obs::metrics::MetricsExporter;
use crate::state::SharedState;
use axum::extract::{Request, State};
use axum::http::{HeaderName, HeaderValue, StatusCode};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post, put};
use axum::{Json, Router};
use tower::ServiceBuilder;
use tower_http::cors::{Any, CorsLayer};
use tower_http::limit::RequestBodyLimitLayer;
use tower_http::request_id::{MakeRequestUuid, PropagateRequestIdLayer, SetRequestIdLayer};
use tower_http::trace::TraceLayer;

pub const REQUEST_ID_HEADER: &str = "x-request-id";

/// 把真实路径折算成低基数标签：`/api/v1/tasks/6f1c...` → `/api/v1/tasks/:id`。
///
/// 为什么必须折叠：Prometheus 每条标签组合都是独立时间序列，若直接把 uuid 放进
/// `route` 标签，一次压测就能造出上万条序列，把监控打爆。折叠规则刻意保守——
/// 只把"像 id 的段"替换掉，宁可多几条序列也不要误伤真实路径。
pub(crate) fn route_label(path: &str) -> String {
    let mut out = String::with_capacity(path.len());
    for (i, seg) in path.split('/').enumerate() {
        if i > 0 {
            out.push('/');
        }
        out.push_str(if looks_like_id(seg) { ":id" } else { seg });
    }
    out
}

fn looks_like_id(seg: &str) -> bool {
    if seg.is_empty() {
        return false;
    }
    // 纯数字（自增主键）或 uuid（8-4-4-4-12）
    if seg.chars().all(|c| c.is_ascii_digit()) {
        return true;
    }
    let parts: Vec<&str> = seg.split('-').collect();
    if parts.len() == 5
        && parts.iter().map(|p| p.len()).collect::<Vec<_>>() == vec![8, 4, 4, 4, 12]
        && seg.chars().all(|c| c.is_ascii_hexdigit() || c == '-')
    {
        return true;
    }
    false
}

/// 全站 HTTP 计数中间件：让 `sa_http_requests_total` 从"定义了没人调"变成真实流量口径。
///
/// 放在最外层，404 / 405 / 401 也会被计数——排查"前端把接口写错了"时这些最有用。
async fn count_http(State(state): State<SharedState>, req: Request, next: Next) -> Response {
    let method = req.method().as_str().to_owned();
    let route = route_label(req.uri().path());
    let resp = next.run(req).await;
    let code = resp.status().as_u16();
    state.metrics.inc_http(&route, &method, code);
    note_status(state.metrics.as_ref(), code);
    resp
}

/// 按响应码补两类"承诺过但曾零落点"的计数。
///
/// 为什么单独抽成一个纯函数：中间件本身要 `Next`、起真连接才能跑，
/// 单测里造不出来——于是这段最容易悄悄失真的逻辑反而不受任何测试保护。
/// 抽成"输入响应码、输出打点"的纯函数后，单测可以直接喂 403 / 500 / 200 / 499
/// 断言计数器，中间件那边只剩一行调用。
///
/// - 403 → `sa_authz_denied_total`：越权拒绝的唯一按码统计口。
///   不放 `AuthUser::require` 里，是因为 require 只有 `&self` 拿不到 state，
///   而 403 的来源不止它（抓取鉴权、任务归属校验、集群管理面都会拒）。
/// - ≥500 → `sa_http_5xx_total`：不复用 `sa_http_requests_total{status="500"}` 去算，
///   是因为那要用 PromQL 做除法，而除法一旦分子分母标签集不匹配（route 是否折叠、
///   method 大小写）会静默变 0 或 NaN——"错误率永远显示 0"比没有错误率更危险。
fn note_status(metrics: &dyn MetricsExporter, code: u16) {
    if code == 403 {
        metrics.inc("sa_authz_denied_total");
    }
    if code >= 500 {
        metrics.inc("sa_http_5xx_total");
    }
}

fn request_id_header() -> HeaderName {
    HeaderName::from_static(REQUEST_ID_HEADER)
}

fn cors_layer(cfg: &Config) -> CorsLayer {
    let base = CorsLayer::new()
        .allow_methods(Any)
        .allow_headers(Any)
        // 前端要能在 JS 里读到请求号，否则报错时无法带上它
        .expose_headers([request_id_header()])
        // 预检结果缓存 10 分钟：前端每次带 Authorization 的请求都会触发 OPTIONS，
        // 不缓存等于把请求量翻倍（早前实测每个写接口都多一次 204 往返）
        .max_age(std::time::Duration::from_secs(600));
    let origins: Vec<HeaderValue> = cfg
        .cors_allowed_origins
        .iter()
        .filter_map(|o| o.parse::<HeaderValue>().ok())
        .collect();
    if origins.is_empty() {
        // 空配置 = **不开跨源**，只发同源响应（不挂 `Access-Control-Allow-Origin`）。
        //
        // 旧实现在这里是 `allow_origin(Any)`：任何站点都能在用户浏览器里对本服务发请求
        // 并读到响应体。个人服务的两种真实用法都不需要它——前端同源托管，或 dev 期走
        // Vite 的 `server.proxy`——所以"默认全放开"换来的只有风险，没有便利。
        // 真要跨源联调，就在 `SA_CORS_ORIGINS` 里显式写白名单；
        // 写 `*` 表示"我清楚后果，就这么开"，此时才退回 Any。
        base
    } else if origins.iter().any(|o| o == "*") {
        base.allow_origin(Any)
    } else {
        base.allow_origin(origins)
    }
}

/// 未命中任何路由时的兜底：返回 JSON 而不是空体，前端不必为 404 单独写解析分支。
async fn not_found_fallback() -> Response {
    (
        StatusCode::NOT_FOUND,
        Json(serde_json::json!({
            "code": "not_found",
            "message": "接口不存在，请检查请求路径与方法",
        })),
    )
        .into_response()
}

/// 路径存在但方法不允许（例如对 /auth/login 发 GET）。
async fn method_not_allowed_fallback() -> Response {
    (
        StatusCode::METHOD_NOT_ALLOWED,
        Json(serde_json::json!({
            "code": "method_not_allowed",
            "message": "该路径不支持此请求方法",
        })),
    )
        .into_response()
}

pub fn router(state: SharedState) -> Router {
    let cfg = state.config.clone();

    let v1 = Router::new()
        // ---- 认证 ----
        .route("/auth/register", post(handlers::auth::register))
        .route("/auth/login", post(handlers::auth::login))
        .route("/auth/refresh", post(handlers::auth::refresh))
        .route("/auth/logout", post(handlers::auth::logout))
        .route("/auth/me", get(handlers::auth::me))
        .route("/auth/password", put(handlers::auth::change_password))
        // ---- 新手教程 ----
        .route("/onboarding", get(handlers::onboarding::overview))
        .route(
            "/onboarding/steps/{step_id}/complete",
            post(handlers::onboarding::complete_step),
        )
        .route("/onboarding/skip", post(handlers::onboarding::skip))
        .route("/onboarding/reset", post(handlers::onboarding::reset))
        // ---- 任务 ----
        .route(
            "/tasks",
            post(handlers::task::create).get(handlers::task::list),
        )
        .route("/tasks/{id}", get(handlers::task::detail))
        .route("/tasks/{id}/trace", get(handlers::task::trace))
        // 进度直播流（SSE）：连接即得当前快照，之后随状态变化推送，终态后自行 EOF
        .route("/tasks/{id}/events", get(handlers::task_events::stream))
        // 取消：异步任务立旗子（202），遗留任务就地收敛（200），已结束 409
        .route("/tasks/{id}/cancel", post(handlers::task::cancel))
        // ---- 共享记忆 ----
        .route(
            "/memories",
            post(handlers::memory::upsert).get(handlers::memory::list),
        )
        .route(
            "/memories/{id}",
            get(handlers::memory::detail)
                .put(handlers::memory::update)
                .delete(handlers::memory::remove),
        )
        // ---- 能力目录 ----
        .route("/agents", get(handlers::catalog::agents))
        .route("/agents/{id}", get(handlers::catalog::agent_detail))
        .route("/capabilities", get(handlers::catalog::capabilities))
        .route(
            "/capabilities/{id}",
            get(handlers::catalog::capability_detail),
        )
        // ---- 集群（多 agent 节点视图）----
        // 单机部署时 nodes 恒为 1 条，形状为多节点预留：将来加节点不改协议
        .route("/cluster", get(handlers::cluster::overview))
        // ---- 可观测 ----
        .route(
            "/observability/overview",
            get(handlers::observability::overview),
        )
        .route("/observability/audit", get(handlers::observability::audit))
        .route(
            "/observability/components",
            get(handlers::observability::components),
        )
        // 告警：抓取即评估（见 `handlers::observability::alerts` 的注释说明为什么不挂后台循环）
        .route("/observability/alerts", get(handlers::observability::alerts));

    Router::new()
        .route("/healthz", get(handlers::observability::health))
        .route("/readyz", get(handlers::observability::ready))
        .route("/metrics", get(handlers::observability::metrics))
        .nest("/api/v1", v1)
        .fallback(not_found_fallback)
        .method_not_allowed_fallback(method_not_allowed_fallback)
        // 先把 state 交给路由（中间件自带一份 state，故此处 clone）
        .with_state(state.clone())
        .layer(
            ServiceBuilder::new()
                // 请求方向：先注入 id；响应方向：最后仍保留同一个 id
                .layer(SetRequestIdLayer::new(
                    request_id_header(),
                    MakeRequestUuid,
                ))
                // 计数放在 RequestBodyLimit 之外：body 还是原始 Body，且 404/405 一并计数
                .layer(axum::middleware::from_fn_with_state(state, count_http))
                .layer(PropagateRequestIdLayer::new(request_id_header()))
                .layer(TraceLayer::new_for_http())
                .layer(RequestBodyLimitLayer::new(cfg.max_body_bytes))
                .layer(cors_layer(&cfg)),
        )
}

#[cfg(test)]
mod tests {
    use super::{note_status, route_label};
    use crate::obs::metrics::{Metrics, MetricsExporter};

    fn counter(m: &Metrics, name: &str) -> u64 {
        m.snapshot().get(name)
    }

    /// 403 计越权、5xx 计错误、其余一律不计 —— 三类边界都要有断言，
    /// 否则"多计"（把 404 也算成越权）和"少计"一样是错的口径。
    #[test]
    fn note_status_counts_only_403_and_5xx() {
        let m = Metrics::new();
        for code in [200u16, 201, 202, 404, 422, 429] {
            note_status(&m, code);
        }
        assert_eq!(counter(&m, "sa_authz_denied_total"), 0, "非 403 不得计越权");
        assert_eq!(counter(&m, "sa_http_5xx_total"), 0, "非 5xx 不得计错误");

        note_status(&m, 403);
        note_status(&m, 403);
        assert_eq!(counter(&m, "sa_authz_denied_total"), 2);
        assert_eq!(counter(&m, "sa_http_5xx_total"), 0, "403 不算 5xx");

        note_status(&m, 500);
        note_status(&m, 503);
        assert_eq!(counter(&m, "sa_http_5xx_total"), 2);
        assert_eq!(
            counter(&m, "sa_authz_denied_total"),
            2,
            "5xx 不得回头改动越权数"
        );
    }

    #[test]
    fn route_label_folds_ids_but_keeps_real_paths() {
        assert_eq!(route_label("/api/v1/tasks"), "/api/v1/tasks");
        assert_eq!(
            route_label("/api/v1/tasks/6f1c2b8e-3d4a-4c9b-8f10-2a7e5d9c1b34"),
            "/api/v1/tasks/:id"
        );
        assert_eq!(route_label("/metrics"), "/metrics");
        assert_eq!(route_label("/api/v1/memories/12345"), "/api/v1/memories/:id");
        // 别把普通路径误折叠
        assert_eq!(
            route_label("/api/v1/observability/overview"),
            "/api/v1/observability/overview"
        );
        assert_eq!(route_label("/api/v1/onboarding"), "/api/v1/onboarding");
    }
}
