//! 请求上下文提取器：Bearer 鉴权、角色判定、请求元信息。

use crate::domain::user::UserStatus;
use crate::error::ApiError;
use crate::security::jwt::{self, Claims, JwtError};
use crate::security::rbac::{self, Permission, Role};
use crate::state::SharedState;
use axum::extract::FromRequestParts;
use axum::http::header::{AUTHORIZATION, USER_AGENT};
use axum::http::request::Parts;
use tower_http::request_id::RequestId;
use chrono::Utc;
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
        let user = state
            .store
            .read(|db| db.users.get(&claims.sub).map(|u| (u.status, u.role)))
            .ok_or_else(|| ApiError::Unauthorized("账号不存在或已被删除".into()))?;
        if user.0 != UserStatus::Active {
            return Err(ApiError::Forbidden("账号已被禁用".into()));
        }

        Ok(AuthUser {
            id: claims.sub.clone(),
            role: user.1,
            claims,
        })
    }
}

impl FromRequestParts<SharedState> for RequestMeta {
    type Rejection = ApiError;

    async fn from_request_parts(
        parts: &mut Parts,
        _state: &SharedState,
    ) -> Result<Self, Self::Rejection> {
        let headers = &parts.headers;
        let forwarded = headers
            .get("x-forwarded-for")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.split(',').next())
            .map(|v| v.trim().to_string());
        let real_ip = headers
            .get("x-real-ip")
            .and_then(|v| v.to_str().ok())
            .map(|v| v.trim().to_string());

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
                .or(real_ip)
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
