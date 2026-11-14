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

impl Backend for MemoryBackend {
    fn kind(&self) -> StorageKind {
        StorageKind::Memory
    }

    fn path(&self) -> &Path {
        Path::new("")
    }

    fn with_read(&self, f: &mut dyn FnMut(&Db)) {
        let guard: RwLockReadGuard<Db> = self.inner.read().expect("存储读锁未被毒化");
        f(&guard);
    }

    fn with_write(&self, f: &mut dyn FnMut(&mut Db)) {
        let mut guard: RwLockWriteGuard<Db> = self.inner.write().expect("存储写锁未被毒化");
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
