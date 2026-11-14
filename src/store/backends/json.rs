//! JSON 快照后端：内存主存 + 原子落盘。
//!
//! 设计取舍：个人部署数据量小（万级以内），整库快照读写简单可靠；
//! 需要并发写或大数据量时，换 `SqliteBackend` 即可，业务代码零改动。

use super::{Backend, StorageKind};
use crate::store::db::Db;
use crate::store::{snapshot, StoreError};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{RwLock, RwLockReadGuard, RwLockWriteGuard};

pub struct JsonBackend {
    inner: RwLock<Db>,
    path: PathBuf,
    dirty: AtomicBool,
}

impl JsonBackend {
    /// 打开（或初始化）快照库。文件损坏时留档并从空库启动，绝不静默丢数据。
    pub fn open(path: PathBuf) -> Result<Self, StoreError> {
        // 传进来的是目录时必须当场说清楚：否则 `fs::read(目录)` 只会抛出
        // “拒绝访问 (os error 5)”，运维根本看不出是路径给错了层级。
        if path.is_dir() {
            return Err(StoreError::Io(format!(
                "快照路径指向了目录 {}，SA_STORAGE=json 需要的是快照文件路径（如 <目录>/db.json）",
                path.display()
            )));
        }
        let db = if path.exists() {
            let raw = std::fs::read(&path).map_err(|e| StoreError::Io(e.to_string()))?;
            match serde_json::from_slice::<Db>(&raw) {
                Ok(db) => db,
                Err(e) => {
                    tracing::error!(error = %e, path = %path.display(), "快照解析失败，转入留档并从空库启动");
                    if let Ok(archived) = snapshot::quarantine(&path) {
                        tracing::error!(archived = %archived.display(), "损坏快照已留档");
                    }
                    Db::default()
                }
            }
        } else {
            Db::default()
        };

        Ok(Self {
            inner: RwLock::new(db),
            path,
            dirty: AtomicBool::new(false),
        })
    }
}

impl Backend for JsonBackend {
    fn kind(&self) -> StorageKind {
        StorageKind::Json
    }

    fn path(&self) -> &Path {
        &self.path
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

    /// 强制落盘（写盘失败不 panic，交由调用方决定是否终止）。
    fn flush(&self) -> Result<usize, StoreError> {
        let bytes = {
            let guard: RwLockReadGuard<Db> = self.inner.read().expect("存储读锁未被毒化");
            serde_json::to_vec(&*guard).map_err(|e| StoreError::Serde(e.to_string()))?
        };
        let written =
            snapshot::atomic_write(&self.path, &bytes).map_err(|e| StoreError::Io(e.to_string()))?;
        self.dirty.store(false, Ordering::Relaxed);
        Ok(written)
    }

    /// 仅在脏页时落盘，供后台定时任务调用。
    fn flush_if_dirty(&self) -> Result<bool, StoreError> {
        if !self.dirty.swap(false, Ordering::Relaxed) {
            return Ok(false);
        }
        match self.flush() {
            Ok(_) => Ok(true),
            Err(e) => {
                self.dirty.store(true, Ordering::Relaxed);
                Err(e)
            }
        }
    }

    fn is_dirty(&self) -> bool {
        self.dirty.load(Ordering::Relaxed)
    }
}
