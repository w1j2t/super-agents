//! 认证接口：注册、登录、刷新、登出、当前用户、改密。

use crate::api::jsons::{ApiJson};
use crate::api::extract::{AuthUser, RequestMeta};
use crate::error::{validation_error, ApiError};
use crate::services::auth::{AuthResult, LoginInput, RegisterInput};
use crate::state::SharedState;
use axum::extract::State;
use axum::http::StatusCode;
use axum::Json;
use serde::{Deserialize, Serialize};

#[derive(Debug, Deserialize)]
pub struct RefreshInput {
    pub refresh_token: String,
}

#[derive(Debug, Deserialize)]
pub struct ChangePasswordInput {
    pub current_password: String,
    pub new_password: String,
}

#[derive(Debug, Serialize)]
pub struct RevokedView {
    pub revoked_sessions: usize,
}

/// `POST /api/v1/auth/register` — 注册并直接签发令牌（首个注册用户为 owner）。
pub async fn register(
    State(state): State<SharedState>,
    meta: RequestMeta,
    ApiJson(body): ApiJson<RegisterInput>,
) -> Result<(StatusCode, Json<AuthResult>), ApiError> {
    let result = state.auth().register(body, &meta.ip, &meta.user_agent)?;
    Ok((StatusCode::CREATED, Json(result)))
}

/// `POST /api/v1/auth/login` — 用户名或邮箱登录。
pub async fn login(
    State(state): State<SharedState>,
    meta: RequestMeta,
    ApiJson(body): ApiJson<LoginInput>,
) -> Result<Json<AuthResult>, ApiError> {
    let result = state.auth().login(body, &meta.ip, &meta.user_agent)?;
    Ok(Json(result))
}

/// `POST /api/v1/auth/refresh` — 刷新令牌轮换，旧令牌立即作废。
pub async fn refresh(
    State(state): State<SharedState>,
    ApiJson(body): ApiJson<RefreshInput>,
) -> Result<Json<AuthResult>, ApiError> {
    if body.refresh_token.trim().is_empty() {
        return Err(validation_error(vec![("refresh_token", "不能为空".into())]));
    }
    Ok(Json(state.auth().refresh(&body.refresh_token)?))
}

/// `POST /api/v1/auth/logout` — 撤销当前刷新令牌对应的会话（幂等）。
pub async fn logout(
    State(state): State<SharedState>,
    ApiJson(body): ApiJson<RefreshInput>,
) -> Result<Json<RevokedView>, ApiError> {
    let revoked = state.auth().logout(&body.refresh_token)?;
    Ok(Json(RevokedView {
        revoked_sessions: revoked,
    }))
}

/// `GET /api/v1/auth/me` — 当前登录用户。
pub async fn me(
    State(state): State<SharedState>,
    user: AuthUser,
) -> Result<Json<crate::domain::user::UserView>, ApiError> {
    Ok(Json(state.auth().me(&user.id)?))
}

/// `POST /api/v1/auth/password` — 改密成功后所有会话失效，需重新登录。
pub async fn change_password(
    State(state): State<SharedState>,
    user: AuthUser,
    ApiJson(body): ApiJson<ChangePasswordInput>,
) -> Result<Json<RevokedView>, ApiError> {
    let revoked = state
        .auth()
        .change_password(&user.id, &body.current_password, &body.new_password)?;
    Ok(Json(RevokedView {
        revoked_sessions: revoked,
    }))
}
