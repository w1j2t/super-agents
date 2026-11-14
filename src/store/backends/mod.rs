//! 存储后端端口（Port）与注册表。
//!
//! 边界约定：**业务层只认识 `crate::store::Store` 门面**，不认识具体后端。
//! 换部件（JSON 快照 → SQLite → 远端 KV）只需两步：
//! 1. 在本目录新增一个实现 `Backend` 的文件；
//! 2. 在下方 `open()` 工厂里登记一行。
//! 门面、handler、service、domain、测试**均无需改动**。

use crate::store::db::Db;
use crate::store::StoreError;
use std::path::{Path, PathBuf};

pub mod json;
pub mod memory;

/// 可用存储后端。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StorageKind {
    /// 内存主存 + JSON 快照持久化（默认，零外部依赖）
    Json,
    /// 纯内存（重启即清空，用于压测 / 演示 / 单测）
    Memory,
}

impl StorageKind {
    pub const ALL: [&'static str; 2] = ["json", "memory"];

    pub fn as_str(&self) -> &'static str {
        match self {
            StorageKind::Json => "json",
            StorageKind::Memory => "memory",
        }
    }

    pub fn parse(s: &str) -> Option<StorageKind> {
        match s.trim().to_ascii_lowercase().as_str() {
            "json" | "file" | "snapshot" => Some(StorageKind::Json),
            "memory" | "mem" | "ephemeral" => Some(StorageKind::Memory),
            _ => None,
        }
    }

    /// 该后端是否需要落盘（用于启动日志与运维提示）。
    pub fn is_durable(&self) -> bool {
        matches!(self, StorageKind::Json)
    }
}

/// 后端端口：仓储访问的**唯一**入口。
///
/// 用 `&mut dyn FnMut` 而非泛型，是为了让 `Store` 能以 `Box<dyn Backend>`
/// 做运行期替换（trait object），同时把"一次读/写必须原子完成"的语义压在端口上。
pub trait Backend: Send + Sync {
    fn kind(&self) -> StorageKind;

    /// 数据落点（内存后端返回空路径）。
    fn path(&self) -> &Path;

    /// 在一致视图下执行只读闭包。实现必须**恰好调用一次** `f`。
    fn with_read(&self, f: &mut dyn FnMut(&Db));

    /// 在写锁下执行可变闭包并标记脏页。实现必须**恰好调用一次** `f`。
    fn with_write(&self, f: &mut dyn FnMut(&mut Db));

    fn flush(&self) -> Result<usize, StoreError>;

    fn flush_if_dirty(&self) -> Result<bool, StoreError>;

    fn is_dirty(&self) -> bool;
}

/// 工厂：新增后端只需在这里加一条分支。
pub fn open(kind: StorageKind, path: PathBuf) -> Result<Box<dyn Backend>, StoreError> {
    match kind {
        StorageKind::Json => Ok(Box::new(json::JsonBackend::open(path)?)),
        StorageKind::Memory => Ok(Box::new(memory::MemoryBackend::new())),
    }
}
