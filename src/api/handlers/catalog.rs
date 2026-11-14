//! 能力目录接口：agent 名册与能力清单（前端据此渲染，不硬编码在页面里）。

use crate::api::jsons::{ApiPath};
use crate::domain::agent::{AgentKind, AGENTS};
use crate::domain::capability::{CapabilitySpec, CAPABILITIES};
use crate::error::ApiError;
use axum::Json;
use serde::Serialize;

#[derive(Debug, Serialize)]
pub struct AgentView {
    pub id: &'static str,
    pub kind: &'static str,
    pub name: &'static str,
    pub domain: &'static str,
    pub summary: &'static str,
    pub vertical_knowledge: Vec<&'static str>,
    pub doc_refs: Vec<&'static str>,
    pub skills: Vec<&'static str>,
    pub permissions: Vec<&'static str>,
    pub sandbox_profile: &'static str,
    pub default_model: &'static str,
    pub task_types: Vec<&'static str>,
    pub max_concurrency: u8,
}

#[derive(Debug, Serialize)]
pub struct CapabilityView {
    pub id: &'static str,
    pub name: &'static str,
    pub category: &'static str,
    pub trigger: &'static str,
    pub owner_agents: Vec<&'static str>,
    pub permission_boundary: &'static str,
    pub observability_signals: Vec<&'static str>,
    pub degradation_or_abort: &'static str,
}

fn kind_of(kind: &AgentKind) -> &'static str {
    match kind {
        AgentKind::Super => "super_agent",
        AgentKind::Sub => "sub_agent",
    }
}

fn agent_view(a: &'static crate::domain::agent::AgentSpec) -> AgentView {
    AgentView {
        id: a.id,
        kind: kind_of(&a.kind),
        name: a.name,
        domain: a.domain,
        summary: a.summary,
        vertical_knowledge: a.vertical_knowledge.to_vec(),
        doc_refs: a.doc_refs.to_vec(),
        skills: a.skills.to_vec(),
        permissions: a.permissions.iter().map(|p| p.as_str()).collect(),
        sandbox_profile: a.sandbox_profile,
        default_model: a.default_model.as_str(),
        task_types: a.task_types.iter().map(|t| t.as_str()).collect(),
        max_concurrency: a.max_concurrency,
    }
}

fn capability_view(c: &'static CapabilitySpec) -> CapabilityView {
    CapabilityView {
        id: c.id,
        name: c.name,
        category: c.category,
        trigger: c.trigger,
        owner_agents: c.owner_agents.to_vec(),
        permission_boundary: c.permission_boundary,
        observability_signals: c.observability_signals.to_vec(),
        degradation_or_abort: c.degradation_or_abort,
    }
}

/// `GET /api/v1/agents`
pub async fn agents() -> Json<Vec<AgentView>> {
    Json(AGENTS.iter().map(|a| agent_view(a)).collect())
}

/// `GET /api/v1/agents/{id}`
pub async fn agent_detail(ApiPath(id): ApiPath<String>) -> Result<Json<AgentView>, ApiError> {
    AGENTS
        .iter()
        .find(|a| a.id == id)
        .map(|a| Json(agent_view(a)))
        .ok_or_else(|| ApiError::NotFound(format!("未找到 agent：{id}")))
}

/// `GET /api/v1/capabilities`
pub async fn capabilities() -> Json<Vec<CapabilityView>> {
    Json(CAPABILITIES.iter().map(|c| capability_view(c)).collect())
}

/// `GET /api/v1/capabilities/{id}`
pub async fn capability_detail(ApiPath(id): ApiPath<String>) -> Result<Json<CapabilityView>, ApiError> {
    CAPABILITIES
        .iter()
        .find(|c| c.id == id)
        .map(|c| Json(capability_view(c)))
        .ok_or_else(|| ApiError::NotFound(format!("未找到能力项：{id}")))
}
