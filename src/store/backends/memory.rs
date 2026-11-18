//! 纯内存后端：不落盘、重启即清空。
//!
//! 存在的意义有两个：
//! 1. 证明"换一个部件不动全局"——它与 JSON 后端互换后，全部接口行为一致；
//! 2. 作为压测 / 演示 / 单测的零 IO 后端。

use super::{Backend, StorageKind};
use crate::store::db::Db;
use crate::store::StoreError;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{RwLock, RwLockReadGuard, RwLockWriteGuard};

pub struct MemoryBackend {
    inner: RwLock<Db>,
    dirty: AtomicBool,
}

impl MemoryBackend {
    pub fn new() -> Self {
        Self {
            inner: RwLock::new(Db::default()),
            dirty: AtomicBool::new(false),
        }
    }
}

impl Default for MemoryBackend {
    fn default() -> Self {
        Self::new()
    }
}

/// 锁中毒恢复口径（三方审查 M2）。
///
/// 持锁线程 panic 会让锁永久中毒；继续 `unwrap`/`expect` 会把"一次 panic"
/// 放大成"此后每个请求都 panic"的雪崩。审计流水、计数器、限流桶在 panic 时
/// 并不处于不一致状态（要么未改，要么改完才放锁），所以恢复内部值继续服务；
/// 告警只打一次，避免刷屏把首因埋掉。
fn recover_poison<T>(res: std::sync::LockResult<T>, tag: &'static str) -> T {
    use std::sync::atomic::{AtomicBool, Ordering};
    static WARNED: AtomicBool = AtomicBool::new(false);
    match res {
        Ok(v) => v,
        Err(e) => {
            if !WARNED.swap(true, Ordering::Relaxed) {
                tracing::error!(tag, "锁中毒，已恢复内部数据；请排查持锁线程的 panic");
            }
            e.into_inner()
        }
    }
}

impl Backend for MemoryBackend {
    fn kind(&self) -> StorageKind {
        StorageKind::Memory
    }

    fn path(&self) -> &Path {
        Path::new("")
    }

    fn with_read(&self, f: &mut dyn FnMut(&Db)) {
        let guard: RwLockReadGuard<Db> = recover_poison(self.inner.read(), "store.memory.read");
        f(&guard);
    }

    fn with_write(&self, f: &mut dyn FnMut(&mut Db)) {
        let mut guard: RwLockWriteGuard<Db> = recover_poison(self.inner.write(), "store.memory.write");
        f(&mut guard);
        self.dirty.store(true, Ordering::Relaxed);
    }

    fn flush(&self) -> Result<usize, StoreError> {
        self.dirty.store(false, Ordering::Relaxed);
        Ok(0)
    }

    fn flush_if_dirty(&self) -> Result<bool, StoreError> {
        Ok(self.dirty.swap(false, Ordering::Relaxed))
    }

    fn is_dirty(&self) -> bool {
        self.dirty.load(Ordering::Relaxed)
    }
}
