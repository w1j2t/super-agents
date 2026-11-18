//! 请求上下文提取器：Bearer 鉴权、角色判定、请求元信息。

use crate::domain::user::UserStatus;
use crate::error::ApiError;
use crate::security::jwt::{self, Claims, JwtError};
use crate::security::rbac::{self, Permission, Role};
use crate::state::SharedState;
use axum::extract::{ConnectInfo, FromRequestParts};
use axum::http::header::{AUTHORIZATION, USER_AGENT};
use axum::http::request::Parts;
use tower_http::request_id::RequestId;
use chrono::Utc;
use std::net::SocketAddr;
use std::sync::Arc;

/// 已鉴权用户。任何需要登录的接口在参数里写 `user: AuthUser` 即完成鉴权。
#[derive(Debug, Clone)]
pub struct AuthUser {
    pub id: String,
    pub role: Role,
    pub claims: Claims,
}

impl AuthUser {
    /// 权限校验：不通过返回 403，并在错误码中带上所需权限，便于前端与审计对齐。
    pub fn require(&self, perm: Permission) -> Result<(), ApiError> {
        if rbac::allows(self.role, perm) {
            Ok(())
        } else {
            Err(ApiError::Forbidden(format!(
                "角色 {} 缺少权限 {}",
                self.role.as_str(),
                perm.as_str()
            )))
        }
    }
}

/// `/metrics` 抓取鉴权：优先静态抓取令牌（SA_METRICS_TOKEN），否则回退管理员 access token。
/// 这样 Prometheus 可用长期令牌抓取，无需持有会过期的用户令牌。
pub struct MetricsAuth;

fn ct_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

impl FromRequestParts<SharedState> for MetricsAuth {
    type Rejection = ApiError;

    async fn from_request_parts(
        parts: &mut Parts,
        state: &SharedState,
    ) -> Result<Self, Self::Rejection> {
        let token = bearer(&parts.headers)?;
        if let Some(expected) = state.config.metrics_token.as_deref() {
            if ct_eq(token.as_bytes(), expected.as_bytes()) {
                return Ok(MetricsAuth);
            }
        }
        let user = AuthUser::from_request_parts(parts, state).await?;
        user.require(Permission::Admin)?;
        Ok(MetricsAuth)
    }
}

/// 请求元信息（IP / UA / 请求号），用于审计与限流。永不失败。
#[derive(Debug, Clone)]
pub struct RequestMeta {
    pub ip: String,
    pub user_agent: String,
    pub request_id: String,
}

fn bearer(headers: &axum::http::HeaderMap) -> Result<&str, ApiError> {
    let raw = headers
        .get(AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .ok_or_else(|| ApiError::Unauthorized("缺少 Authorization 请求头".into()))?;
    let mut parts = raw.splitn(2, ' ');
    let (scheme, token) = match (parts.next(), parts.next()) {
        (Some(s), Some(t)) => (s, t),
        _ => return Err(ApiError::Unauthorized("Authorization 格式应为 Bearer <token>".into())),
    };
    if !scheme.eq_ignore_ascii_case("bearer") || token.trim().is_empty() {
        return Err(ApiError::Unauthorized("Authorization 格式应为 Bearer <token>".into()));
    }
    Ok(token.trim())
}

impl FromRequestParts<SharedState> for AuthUser {
    type Rejection = ApiError;

    async fn from_request_parts(
        parts: &mut Parts,
        state: &SharedState,
    ) -> Result<Self, Self::Rejection> {
        let token = bearer(&parts.headers)?;
        let claims = jwt::verify(
            &state.config.jwt_secret,
            token,
            jwt::TOKEN_TYPE_ACCESS,
            Utc::now().timestamp(),
        )
        .map_err(|e| match e {
            JwtError::Expired => ApiError::Unauthorized("访问令牌已过期".into()),
            JwtError::WrongType => ApiError::Unauthorized("请使用 access token 访问".into()),
            _ => ApiError::Unauthorized("访问令牌无效".into()),
        })?;

        // 令牌有效不等于账号仍有效：禁用/删除后立即失效
        // 令牌有效不等于这次登录还有效：登出、改密、管理员踢人撤销的都是**会话**，
        // 而 access 令牌是自包含的，不看 `sid` 的话它能一路用到过期（默认 30 分钟）。
        // 那段时间里"已登出"只存在于前端的想象中，谁捡到令牌谁还用得上。
        //
        // 旧令牌没有 `sid`（空串），跳过这一步——升级不该把在线用户全部踢下线。
        let (status, role, session_alive) = state
            .store
            .read(|db| {
                let u = db.users.get(&claims.sub)?;
                let alive = claims.sid.is_empty()
                    || db
                        .sessions
                        .get(&claims.sid)
                        // 顺着轮换链走到最新一代再判定：access 里带的是签发时那一代的 sid，
                        // 轮换之后撤销状态记在新一代身上，只看自己被撤会漏掉登出/改密
                        // （见 Session::successor_id 的注释）。
                        .map(|s| s.lineage_alive(Utc::now(), |nid| db.sessions.get(nid)))
                        .unwrap_or(false);
                Some((u.status, u.role, alive))
            })
            .ok_or_else(|| ApiError::Unauthorized("账号不存在或已被删除".into()))?;
        if status != UserStatus::Active {
            return Err(ApiError::Forbidden("账号已被禁用".into()));
        }
        if !session_alive {
            return Err(ApiError::Unauthorized("会话已失效，请重新登录".into()));
        }

        Ok(AuthUser {
            id: claims.sub.clone(),
            role,
            claims,
        })
    }
}

impl FromRequestParts<SharedState> for RequestMeta {
    type Rejection = ApiError;

    async fn from_request_parts(
        parts: &mut Parts,
        state: &SharedState,
    ) -> Result<Self, Self::Rejection> {
        let headers = &parts.headers;

        // 顺序是"先要可信来源，再考虑头"：`ConnectInfo` 由内核给出的对端地址填充，
        // 客户端改不了；`X-Forwarded-For` / `X-Real-IP` 只是一段客户端自己写的字符串。
        //
        // 旧实现反过来，无条件取 XFF 首值：等于把限流分桶的键交给调用方自选（换个头换个桶，
        // 配额形同虚设），还能伪造别人的 IP 把锁定记到受害者头上。现在默认只认对端地址，
        // 只有显式 `SA_TRUST_PROXY=1`（本进程确实挂在可信反代后面）才采信代理头。
        let peer_ip = parts
            .extensions
            .get::<ConnectInfo<SocketAddr>>()
            .map(|ci| ci.0.ip().to_string());

        let forwarded = if state.config.trust_proxy {
            headers
                .get("x-forwarded-for")
                .and_then(|v| v.to_str().ok())
                .and_then(|v| v.split(',').next())
                .map(|v| v.trim().to_string())
                .or_else(|| {
                    headers
                        .get("x-real-ip")
                        .and_then(|v| v.to_str().ok())
                        .map(|v| v.trim().to_string())
                })
        } else {
            None
        };

        // 请求号由 SetRequestIdLayer 注入到 extensions，与响应头 x-request-id 完全一致；
        // 直接读 header 只能拿到"客户端自己传的"，客户端没传就会是空串，审计便无法对账。
        let request_id = parts
            .extensions
            .get::<RequestId>()
            .and_then(|id| id.header_value().to_str().ok())
            .map(str::to_string)
            .or_else(|| {
                headers
                    .get(crate::api::REQUEST_ID_HEADER)
                    .and_then(|v| v.to_str().ok())
                    .map(str::to_string)
            })
            .unwrap_or_default();

        Ok(RequestMeta {
            ip: forwarded
                .or(peer_ip)
                .filter(|v| !v.is_empty())
                .unwrap_or_else(|| "unknown".into()),
            user_agent: headers
                .get(USER_AGENT)
                .and_then(|v| v.to_str().ok())
                .unwrap_or("unknown")
                .to_string(),
            request_id,
        })
    }
}

/// 供 handler 内部复用：从 Arc 状态取引用，避免每处写 `as_ref()`。
pub fn cfg(state: &Arc<crate::state::AppState>) -> &crate::config::Config {
    state.config.as_ref()
}
