//! 共享记忆：分层作用域 + 版本化写入 + 访问热度（数据飞轮的基础）。

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MemoryScope {
    /// 仅本人可见
    Private,
    /// 多 agent / 多会话共享
    Shared,
    /// 系统级（配置、治理结论），仅 owner/admin 可写
    System,
}

impl MemoryScope {
    pub fn as_str(&self) -> &'static str {
        match self {
            MemoryScope::Private => "private",
            MemoryScope::Shared => "shared",
            MemoryScope::System => "system",
        }
    }

    pub fn parse(s: &str) -> Option<MemoryScope> {
        match s.to_ascii_lowercase().as_str() {
            "private" => Some(MemoryScope::Private),
            "shared" => Some(MemoryScope::Shared),
            "system" => Some(MemoryScope::System),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemoryEntry {
    pub id: String,
    pub scope: MemoryScope,
    pub owner_id: String,
    pub key: String,
    pub content: String,
    pub tags: Vec<String>,
    pub version: u32,
    pub written_by: String,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
    pub access_count: u64,
    /// 0.0-1.0，供调度打分使用；冲突写入会下调
    pub confidence: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemoryWrite {
    pub scope: MemoryScope,
    pub key: String,
    pub content: String,
    #[serde(default)]
    pub tags: Vec<String>,
    #[serde(default = "default_confidence")]
    pub confidence: f64,
}

fn default_confidence() -> f64 {
    0.8
}

impl MemoryWrite {
    pub fn validate(&self) -> Result<(), Vec<(&'static str, String)>> {
        let mut errs = Vec::new();
        if self.key.trim().is_empty() || self.key.len() > 128 {
            errs.push(("key", "必填且不超过 128 字符".to_string()));
        }
        if self.content.trim().is_empty() || self.content.len() > 8_000 {
            errs.push(("content", "必填且不超过 8000 字符".to_string()));
        }
        if self.tags.len() > 16 {
            errs.push(("tags", "最多 16 个标签".to_string()));
        }
        if !(0.0..=1.0).contains(&self.confidence) {
            errs.push(("confidence", "取值范围 0.0-1.0".to_string()));
        }
        if errs.is_empty() {
            Ok(())
        } else {
            Err(errs)
        }
    }
}
