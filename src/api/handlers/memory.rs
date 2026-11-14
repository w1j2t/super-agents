//! 共享记忆接口：private / shared / system 三级作用域，写入按角色鉴权，读取按可见性过滤。

use crate::api::jsons::{normalize_keyword, ApiJson, ApiPath, ApiQuery, window_checked};
use crate::api::extract::AuthUser;
use crate::domain::memory::{MemoryEntry, MemoryScope, MemoryWrite};
use crate::error::{validation_error, ApiError};
use crate::security::rbac::{Permission, Role};
use crate::state::SharedState;
use axum::extract::{State};
use axum::http::StatusCode;
use axum::Json;
use chrono::Utc;
use serde::{Deserialize, Serialize};

#[derive(Debug, Deserialize)]
pub struct UpsertInput {
    pub scope: String,
    pub key: String,
    pub content: String,
    #[serde(default)]
    pub tags: Vec<String>,
    #[serde(default)]
    pub confidence: Option<f64>,
}

#[derive(Debug, Deserialize)]
pub struct UpdateInput {
    pub content: String,
    #[serde(default)]
    pub tags: Option<Vec<String>>,
    #[serde(default)]
    pub confidence: Option<f64>,
}

#[derive(Debug, Deserialize)]
pub struct MemoryListQuery {
    #[serde(default)]
    pub scope: Option<String>,
    /// 关键词，匹配 key 或 content
    #[serde(default)]
    pub q: Option<String>,

    /// limit / offset 统一由 PageQuery 提供
    #[serde(default)]
    pub limit: Option<usize>,
    #[serde(default)]
    pub offset: Option<usize>,
}
/// 关键词长度上限（字符数）：够用且挡住超长关键词放大。
pub const MAX_KEYWORD_CHARS: usize = 200;

#[derive(Debug, Serialize)]
pub struct MemoryList {
    /// 过滤后的全量条数（不受分页影响），前端据此渲染页码
    pub total: usize,
    /// 本页实际条数
    pub count: usize,
    pub limit: usize,
    pub offset: usize,
    /// 后面还有数据，直接驱动"加载更多"
    pub has_more: bool,
    pub items: Vec<MemoryEntry>,
}

impl MemoryList {
    fn new(items: Vec<MemoryEntry>, total: usize, limit: usize, offset: usize) -> Self {
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

#[derive(Debug, Serialize)]
pub struct UpsertResult {
    pub created: bool,
    pub entry: MemoryEntry,
}

fn permission_for(scope: MemoryScope) -> Permission {
    match scope {
        MemoryScope::Private => Permission::Write,
        MemoryScope::Shared => Permission::MemoryWriteShared,
        MemoryScope::System => Permission::MemoryWriteSystem,
    }
}

fn can_read(user: &AuthUser, entry: &MemoryEntry) -> bool {
    entry.scope.as_str() != "private" || entry.owner_id == user.id
}

fn can_manage(user: &AuthUser, entry: &MemoryEntry) -> bool {
    entry.owner_id == user.id || user.role.rank() >= Role::Admin.rank()
}

/// `POST /api/v1/memories` — 同一 (scope, owner, key) 重复写入即版本 +1。
pub async fn upsert(
    State(state): State<SharedState>,
    user: AuthUser,
    ApiJson(body): ApiJson<UpsertInput>,
) -> Result<(StatusCode, Json<UpsertResult>), ApiError> {
    let scope = MemoryScope::parse(body.scope.trim())
        .ok_or_else(|| validation_error(vec![("scope", "可选 private / shared / system".into())]))?;
    user.require(permission_for(scope))?;

    let write = MemoryWrite {
        scope,
        key: body.key.trim().to_string(),
        content: body.content,
        tags: body.tags,
        confidence: body.confidence.unwrap_or(0.8).clamp(0.0, 1.0),
    };
    write.validate().map_err(validation_error)?;

    let now = Utc::now();
    let (entry, created) = state.store.write(|db| {
        let existing_id = db
            .memories
            .iter()
            .find(|(_, m)| {
                m.scope.as_str() == scope.as_str()
                    && m.owner_id == user.id
                    && m.key == write.key
            })
            .map(|(id, _)| id.clone());

        match existing_id.and_then(|id| db.memories.get_mut(&id).map(|e| (id.clone(), e))) {
            Some((_, entry)) => {
                entry.content = write.content.clone();
                entry.tags = write.tags.clone();
                entry.confidence = write.confidence;
                entry.version += 1;
                entry.updated_at = now;
                entry.written_by = user.id.clone();
                (entry.clone(), false)
            }
            None => {
                let entry = MemoryEntry {
                    id: uuid::Uuid::new_v4().to_string(),
                    scope,
                    owner_id: user.id.clone(),
                    key: write.key.clone(),
                    content: write.content.clone(),
                    tags: write.tags.clone(),
                    version: 1,
                    written_by: user.id.clone(),
                    created_at: now,
                    updated_at: now,
                    access_count: 0,
                    confidence: write.confidence,
                };
                db.memories.insert(entry.id.clone(), entry.clone());
                (entry, true)
            }
        }
    });

    state.metrics.inc("sa_memory_writes_total");
    // 写穿：记忆写入立刻落盘（同任务，理由见 AppState::persist_now）
    state.persist_now();
    state.audit.record(
        &user.id,
        if created {
            "memory.create"
        } else {
            "memory.update"
        },
        &entry.id,
        true,
        &format!("scope={} version={}", entry.scope.as_str(), entry.version),
        "",
    );

    let code = if created {
        StatusCode::CREATED
    } else {
        StatusCode::OK
    };
    Ok((code, Json(UpsertResult { created, entry })))
}

/// `GET /api/v1/memories?scope=shared&limit=20`
pub async fn list(
    State(state): State<SharedState>,
    user: AuthUser,
    ApiQuery(q): ApiQuery<MemoryListQuery>,
) -> Result<Json<MemoryList>, ApiError> {
    let (limit, offset) = window_checked(q.limit, q.offset, 50, 200)?;
    let scope_filter = match q.scope.as_deref().map(str::trim).filter(|s| !s.is_empty()) {
        None => None,
        Some(raw) => Some(
            MemoryScope::parse(raw)
                .ok_or_else(|| validation_error(vec![("scope", "可选 private / shared / system".into())]))?,
        ),
    };
    // 关键词同时匹配 key 与 content：前端搜索框一个输入就能用，不必让用户选搜哪个字段
    let keyword = normalize_keyword(q.q.as_deref(), "q", MAX_KEYWORD_CHARS)?;

    let mut items: Vec<MemoryEntry> = state.store.read(|db| {
        db.memories
            .values()
            .filter(|m| can_read(&user, m))
            .filter(|m| {
                scope_filter
                    .as_ref()
                    .map(|s| m.scope.as_str() == s.as_str())
                    .unwrap_or(true)
            })
            .filter(|m| match &keyword {
                None => true,
                Some(k) => {
                    // 三个字段一起搜：用户记得的是"内容"还是"标签"不该由他先猜一遍
                    m.key.to_lowercase().contains(k)
                        || m.content.to_lowercase().contains(k)
                        || m.tags.iter().any(|tg| tg.to_lowercase().contains(k))
                }
            })
            .cloned()
            .collect()
    });
    let total = items.len();
    items.sort_by(|a, b| b.updated_at.cmp(&a.updated_at));
    let items: Vec<MemoryEntry> = items.into_iter().skip(offset).take(limit).collect();

    state.metrics.inc("sa_memory_reads_total");
    Ok(Json(MemoryList::new(items, total, limit, offset)))
}

fn load_visible(state: &SharedState, user: &AuthUser, id: &str) -> Result<MemoryEntry, ApiError> {
    let entry = state
        .store
        .read(|db| db.memories.get(id).cloned())
        .ok_or_else(|| ApiError::NotFound("记忆不存在".into()))?;
    if !can_read(user, &entry) {
        return Err(ApiError::Forbidden("无权访问该记忆".into()));
    }
    Ok(entry)
}

/// `GET /api/v1/memories/{id}`
pub async fn detail(
    State(state): State<SharedState>,
    user: AuthUser,
    ApiPath(id): ApiPath<String>,
) -> Result<Json<MemoryEntry>, ApiError> {
    let entry = load_visible(&state, &user, &id)?;
    Ok(Json(entry))
}

/// `PUT /api/v1/memories/{id}` — 仅 owner 或 admin。
pub async fn update(
    State(state): State<SharedState>,
    user: AuthUser,
    ApiPath(id): ApiPath<String>,
    ApiJson(body): ApiJson<UpdateInput>,
) -> Result<Json<MemoryEntry>, ApiError> {
    let current = load_visible(&state, &user, &id)?;
    if !can_manage(&user, &current) {
        return Err(ApiError::Forbidden("仅 owner 或管理员可修改该记忆".into()));
    }

    let payload = MemoryWrite {
        scope: current.scope,
        key: current.key.clone(),
        content: body.content,
        tags: body.tags.unwrap_or_else(|| current.tags.clone()),
        confidence: body.confidence.unwrap_or(current.confidence).clamp(0.0, 1.0),
    };
    payload.validate().map_err(validation_error)?;

    let now = Utc::now();
    let updated = state.store.write(|db| {
        db.memories.get_mut(&id).map(|entry| {
            entry.content = payload.content.clone();
            entry.tags = payload.tags.clone();
            entry.confidence = payload.confidence;
            entry.version += 1;
            entry.updated_at = now;
            entry.written_by = user.id.clone();
            entry.clone()
        })
    });
    let entry = updated.ok_or_else(|| ApiError::NotFound("记忆不存在".into()))?;
    // 写穿：记忆更新立刻落盘
    state.persist_now();
    state.audit.record(
        &user.id,
        "memory.update",
        &entry.id,
        true,
        &format!("version={}", entry.version),
        "",
    );
    Ok(Json(entry))
}

/// `DELETE /api/v1/memories/{id}` — 仅 owner 或 admin；system 级需管理员。
pub async fn remove(
    State(state): State<SharedState>,
    user: AuthUser,
    ApiPath(id): ApiPath<String>,
) -> Result<StatusCode, ApiError> {
    let current = load_visible(&state, &user, &id)?;
    if !can_manage(&user, &current) {
        return Err(ApiError::Forbidden("仅 owner 或管理员可删除该记忆".into()));
    }
    if current.scope.as_str() == "system" && user.role.rank() < Role::Admin.rank() {
        return Err(ApiError::Forbidden("system 级记忆需管理员权限".into()));
    }
    state.store.write(|db| db.memories.remove(&id));
    // 写穿：删除也要立刻落盘，否则重启后被删的记忆会"复活"
    state.persist_now();
    state.audit.record(&user.id, "memory.delete", &id, true, "", "");
    Ok(StatusCode::NO_CONTENT)
}
