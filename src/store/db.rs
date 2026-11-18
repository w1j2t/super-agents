//! 数据模型（持久化契约）。
//!
//! 这里只描述"数据长什么样"，不含任何读写实现；换存储引擎（JSON → SQLite → 远端）
//! 时本文件**完全不需要改动**，这是模块化的分界线。

use crate::domain::memory::MemoryEntry;
use crate::domain::onboarding::OnboardingProgress;
use crate::domain::task::TaskRecord;
use crate::domain::user::{Session, User};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashMap};

pub const SCHEMA_VERSION: u32 = 1;

#[derive(Debug, Clone, Serialize, Deserialize)]
// FIX-003：缺字段不再等于整库损坏。结构级 `default` 让缺失字段回落到
// `Db::default()` 的同名字段，而多出来的未知字段 serde 默认忽略——
// 于是"加字段"这一侧是前向兼容的；反向的"库比程序新"由 json.rs 的版本闸门拒绝。
#[serde(default)]
pub struct Db {
    pub schema_version: u32,
    /// user_id -> User
    pub users: HashMap<String, User>,
    /// username_lower -> user_id
    pub username_index: HashMap<String, String>,
    /// email_lower -> user_id
    pub email_index: HashMap<String, String>,
    pub sessions: HashMap<String, Session>,
    /// user_id -> onboarding progress
    pub progress: HashMap<String, OnboardingProgress>,
    pub tasks: HashMap<String, TaskRecord>,
    pub memories: HashMap<String, MemoryEntry>,
}

impl Default for Db {
    fn default() -> Self {
        Self {
            schema_version: SCHEMA_VERSION,
            users: HashMap::new(),
            username_index: HashMap::new(),
            email_index: HashMap::new(),
            sessions: HashMap::new(),
            progress: HashMap::new(),
            tasks: HashMap::new(),
            memories: HashMap::new(),
        }
    }
}

impl Db {
    pub fn bootstrapped(&self) -> bool {
        !self.users.is_empty()
    }

    pub fn user_by_username(&self, username_lower: &str) -> Option<&User> {
        self.username_index
            .get(username_lower)
            .and_then(|id| self.users.get(id))
    }

    pub fn user_by_email(&self, email_lower: &str) -> Option<&User> {
        self.email_index
            .get(email_lower)
            .and_then(|id| self.users.get(id))
    }

    pub fn user_count(&self) -> usize {
        self.users.len()
    }

    /// 计数型摘要，供 /metrics 与 /admin/overview 使用
    pub fn stats(&self) -> BTreeMap<String, u64> {
        let mut map = BTreeMap::new();
        map.insert("users".into(), self.users.len() as u64);
        map.insert("sessions".into(), self.sessions.len() as u64);
        map.insert("tasks".into(), self.tasks.len() as u64);
        map.insert("memories".into(), self.memories.len() as u64);
        map.insert("onboarding".into(), self.progress.len() as u64);
        map
    }
}
