//! 统一错误类型与 HTTP 响应映射（RFC7807 风格 Problem JSON）。

use axum::http::{header, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde::Serialize;
use std::collections::BTreeMap;

pub type ApiResult<T> = Result<T, ApiError>;

#[derive(Debug, thiserror::Error)]
pub enum ApiError {
    #[error("{0}")]
    BadRequest(String),
    #[error("{0}")]
    Unauthorized(String),
    /// 退避期内的 401：状态码、code、文案与普通失败完全一致（不制造账号存在性信号），
    /// 仅额外带标准 Retry-After 头，让客户端按真实剩余时间退避而不是靠猜。
    #[error("{message}")]
    UnauthorizedWithRetry { message: String, retry_after: u64 },
    #[error("{0}")]
    Forbidden(String),
    #[error("{0}")]
    NotFound(String),
    #[error("{0}")]
    Conflict(String),
    /// 参数校验失败，携带字段级明细
    #[error("请求参数校验未通过")]
    Validation(Vec<(String, String)>),
    #[error("{0}")]
    Unprocessable(String),
    #[error("{message}")]
    TooManyRequests { message: String, retry_after: u64 },
    #[error("{0}")]
    PayloadTooLarge(String),
    #[error("{0}")]
    Unavailable(String),
    #[error("{0}")]
    Internal(String),
}

#[derive(Debug, Serialize)]
pub struct ProblemBody {
    pub code: &'static str,
    pub message: String,
    #[serde(skip_serializing_if = "BTreeMap::is_empty")]
    pub details: BTreeMap<String, String>,
}

impl ApiError {
    pub fn code(&self) -> &'static str {
        match self {
            ApiError::BadRequest(_) => "bad_request",
            ApiError::Unauthorized(_) | ApiError::UnauthorizedWithRetry { .. } => "unauthorized",
            ApiError::Forbidden(_) => "forbidden",
            ApiError::NotFound(_) => "not_found",
            ApiError::Conflict(_) => "conflict",
            ApiError::Validation(_) => "unprocessable_entity",
            ApiError::Unprocessable(_) => "unprocessable_entity",
            ApiError::TooManyRequests { .. } => "too_many_requests",
            ApiError::PayloadTooLarge(_) => "payload_too_large",
            ApiError::Unavailable(_) => "service_unavailable",
            ApiError::Internal(_) => "internal_error",
        }
    }

    pub fn status(&self) -> StatusCode {
        match self {
            ApiError::BadRequest(_) => StatusCode::BAD_REQUEST,
            ApiError::Unauthorized(_) | ApiError::UnauthorizedWithRetry { .. } => {
                StatusCode::UNAUTHORIZED
            }
            ApiError::Forbidden(_) => StatusCode::FORBIDDEN,
            ApiError::NotFound(_) => StatusCode::NOT_FOUND,
            ApiError::Conflict(_) => StatusCode::CONFLICT,
            ApiError::Validation(_) | ApiError::Unprocessable(_) => StatusCode::UNPROCESSABLE_ENTITY,
            ApiError::TooManyRequests { .. } => StatusCode::TOO_MANY_REQUESTS,
            ApiError::PayloadTooLarge(_) => StatusCode::PAYLOAD_TOO_LARGE,
            ApiError::Unavailable(_) => StatusCode::SERVICE_UNAVAILABLE,
            ApiError::Internal(_) => StatusCode::INTERNAL_SERVER_ERROR,
        }
    }

    /// 内部错误对外只暴露稳定文案，细节仅写日志，避免信息泄漏。
    pub fn to_problem(&self) -> ProblemBody {
        let mut details = BTreeMap::new();
        let message = match self {
            ApiError::Internal(detail) => {
                tracing::error!(detail = %detail, "internal error");
                "内部错误，已记录到服务端日志".to_string()
            }
            ApiError::Validation(fields) => {
                for (k, v) in fields {
                    details.insert(k.clone(), v.clone());
                }
                "请求参数校验未通过".to_string()
            }
            other => other.to_string(),
        };
        ProblemBody {
            code: self.code(),
            message,
            details,
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let status = self.status();
        let mut resp = (status, Json(self.to_problem())).into_response();
        // RFC 9110 §10.2.3：429 必须给 Retry-After；退避期内的 401 也带上，
        // 客户端才能按真实剩余秒数退避（状态码仍为 401，不制造账号存在性差异）。
        let wait_secs = match &self {
            ApiError::TooManyRequests { retry_after, .. } => Some(*retry_after),
            ApiError::UnauthorizedWithRetry { retry_after, .. } => Some(*retry_after),
            _ => None,
        };
        if let Some(secs) = wait_secs {
            if let Ok(v) = axum::http::HeaderValue::from_str(&secs.to_string()) {
                resp.headers_mut().insert(header::RETRY_AFTER, v);
            }
        }
        if status == StatusCode::UNAUTHORIZED {
            resp.headers_mut().insert(
                header::WWW_AUTHENTICATE,
                axum::http::HeaderValue::from_static("Bearer"),
            );
        }
        resp
    }
}

impl From<serde_json::Error> for ApiError {
    fn from(e: serde_json::Error) -> Self {
        ApiError::Internal(format!("json error: {e}"))
    }
}

/// 统一产出 422，带字段级 details。
pub fn validation_error(fields: Vec<(&str, String)>) -> ApiError {
    ApiError::Validation(
        fields
            .into_iter()
            .map(|(k, v)| (k.to_string(), v))
            .collect(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn maps_status_and_codes() {
        assert_eq!(
            ApiError::NotFound("x".into()).status(),
            StatusCode::NOT_FOUND
        );
        assert_eq!(
            ApiError::Unauthorized("x".into()).status(),
            StatusCode::UNAUTHORIZED
        );
        assert_eq!(
            ApiError::TooManyRequests {
                message: "慢点".into(),
                retry_after: 3
            }
            .status(),
            StatusCode::TOO_MANY_REQUESTS
        );
        assert_eq!(
            validation_error(vec![("username", "必填".into())]).status(),
            StatusCode::UNPROCESSABLE_ENTITY
        );
    }

    #[test]
    fn backoff_401_keeps_status_and_adds_retry_after() {
        let err = ApiError::UnauthorizedWithRetry {
            message: "账号或密码不正确，请稍后再试".into(),
            retry_after: 42,
        };
        // 状态码与 code 必须与普通 401 完全一致，否则就是账号存在性信号
        assert_eq!(err.status(), StatusCode::UNAUTHORIZED);
        assert_eq!(err.code(), "unauthorized");
        assert_eq!(err.to_problem().message, "账号或密码不正确，请稍后再试");
        let resp = err.into_response();
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
        assert_eq!(
            resp.headers().get(header::RETRY_AFTER).map(|v| v.to_str().unwrap()),
            Some("42")
        );
    }

    #[test]
    fn plain_401_has_no_retry_after() {
        let resp = ApiError::Unauthorized("账号或密码不正确".into()).into_response();
        assert!(resp.headers().get(header::RETRY_AFTER).is_none());
    }

    #[test]
    fn internal_detail_is_not_leaked() {
        let problem = ApiError::Internal("db 连接串泄露".into()).to_problem();
        assert!(!problem.message.contains("连接串"));
    }

    #[test]
    fn validation_details_are_exposed() {
        let problem = validation_error(vec![("email", "格式不正确".into())]).to_problem();
        assert_eq!(problem.details.get("email").unwrap(), "格式不正确");
        assert_eq!(problem.code, "unprocessable_entity");
    }
}
