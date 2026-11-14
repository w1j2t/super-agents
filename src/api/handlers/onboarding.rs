//! 新手教程接口：教程总览、步骤推进、跳过、重置。
//! 后端只提供状态机与动作标识（action），不渲染任何界面。

use crate::api::jsons::{ApiPath};
use crate::api::extract::AuthUser;
use crate::domain::onboarding::{OnboardingProgress, STEPS};
use crate::error::ApiError;
use crate::state::SharedState;
use axum::extract::{State};
use axum::Json;
use chrono::{DateTime, Utc};
use serde::Serialize;

#[derive(Debug, Serialize)]
pub struct StepView {
    pub key: &'static str,
    pub order: u8,
    pub title: &'static str,
    pub summary: &'static str,
    pub action: &'static str,
    pub required: bool,
    pub est_seconds: u32,
    pub completed: bool,
    pub completed_at: Option<DateTime<Utc>>,
}

#[derive(Debug, Serialize)]
pub struct OnboardingView {
    pub status: &'static str,
    pub total_required: usize,
    pub completed_required: usize,
    pub skipped: bool,
    pub next_step: Option<&'static str>,
    pub next_action: Option<&'static str>,
    pub steps: Vec<StepView>,
}

#[derive(Debug, Serialize)]
pub struct StepChangeView {
    /// false 表示该步骤此前已完成（幂等，不重复计数）
    pub changed: bool,
    pub onboarding: OnboardingView,
}

pub fn render(p: &OnboardingProgress) -> OnboardingView {
    let steps = STEPS
        .iter()
        .map(|s| StepView {
            key: s.key,
            order: s.order,
            title: s.title,
            summary: s.summary,
            action: s.action,
            required: s.required,
            est_seconds: s.est_seconds,
            completed: p.completed.iter().any(|k| k == s.key),
            completed_at: p
                .step_completed_at
                .iter()
                .find(|(k, _)| k == s.key)
                .map(|(_, t)| *t),
        })
        .collect();
    OnboardingView {
        status: p.status(),
        total_required: p.required_total(),
        completed_required: p.required_done(),
        skipped: p.skipped,
        next_step: p.next_step().map(|s| s.key),
        next_action: p.next_step().map(|s| s.action),
        steps,
    }
}

/// 取用户进度；历史数据缺失时就地初始化（不改变已完成状态）。
fn ensure_progress(state: &SharedState, user_id: &str) -> OnboardingProgress {
    if let Some(p) = state.store.read(|db| db.progress.get(user_id).cloned()) {
        return p;
    }
    let fresh = OnboardingProgress::new(user_id, Utc::now());
    state
        .store
        .write(|db| db.progress.insert(user_id.to_string(), fresh.clone()));
    fresh
}

/// `GET /api/v1/onboarding`
pub async fn overview(
    State(state): State<SharedState>,
    user: AuthUser,
) -> Result<Json<OnboardingView>, ApiError> {
    let p = ensure_progress(&state, &user.id);
    Ok(Json(render(&p)))
}

/// `POST /api/v1/onboarding/steps/{key}/complete`
pub async fn complete_step(
    State(state): State<SharedState>,
    user: AuthUser,
    ApiPath(key): ApiPath<String>,
) -> Result<Json<StepChangeView>, ApiError> {
    let now = Utc::now();
    let mut p = ensure_progress(&state, &user.id);
    let changed = p
        .complete(&key, now)
        .map_err(|msg| ApiError::Validation(vec![("key".into(), msg)]))?;
    state
        .store
        .write(|db| db.progress.insert(user.id.clone(), p.clone()));
    if changed {
        state.metrics.inc("sa_onboarding_step_completed_total");
        state.audit.record(
            &user.id,
            "onboarding.complete_step",
            &key,
            true,
            p.status(),
            "",
        );
    }
    if p.status() == "completed" {
        state.metrics.inc("sa_onboarding_completed_total");
    }
    Ok(Json(StepChangeView {
        changed,
        onboarding: render(&p),
    }))
}

/// `POST /api/v1/onboarding/skip`
pub async fn skip(
    State(state): State<SharedState>,
    user: AuthUser,
) -> Result<Json<OnboardingView>, ApiError> {
    let now = Utc::now();
    let mut p = ensure_progress(&state, &user.id);
    p.mark_skipped(now);
    state
        .store
        .write(|db| db.progress.insert(user.id.clone(), p.clone()));
    state.metrics.inc("sa_onboarding_skipped_total");
    state
        .audit
        .record(&user.id, "onboarding.skip", &user.id, true, "", "");
    Ok(Json(render(&p)))
}

/// `POST /api/v1/onboarding/reset` — 重置进度本身不破坏数据，仅影响教程状态。
pub async fn reset(
    State(state): State<SharedState>,
    user: AuthUser,
) -> Result<Json<OnboardingView>, ApiError> {
    let now = Utc::now();
    let mut p = ensure_progress(&state, &user.id);
    p.reset(now);
    state
        .store
        .write(|db| db.progress.insert(user.id.clone(), p.clone()));
    state
        .audit
        .record(&user.id, "onboarding.reset", &user.id, true, "", "");
    Ok(Json(render(&p)))
}
