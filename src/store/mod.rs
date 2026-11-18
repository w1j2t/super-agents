//! 存储层门面。
//!
//! 结构分三段，边界清晰：
//! - `db`：数据模型（数据长什么样），与存储引擎无关；
//! - `backends`：具体后端实现（JSON 快照 / 纯内存 / 未来的 SQLite），由端口 `Backend` 约束；
//! - 本文件：`Store` 门面，业务层唯一依赖的入口。
//!
//! 换部件路径：新增 `backends/xxx.rs` 实现 `Backend` → 在 `backends::open()` 登记一行 →
//! 用 `SA_STORAGE=xxx` 启动。**handler / service / domain / 测试都不用动。**

pub mod backends;
pub mod db;
pub mod snapshot;

pub use backends::{Backend, StorageKind};
pub use db::{Db, SCHEMA_VERSION};

use std::path::{Path, PathBuf};

#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    #[error("存储读取失败: {0}")]
    Io(String),
    #[error("序列化失败: {0}")]
    Serde(String),
}

/// 存储门面：对上层暴露稳定 API，对内可插拔后端。
pub struct Store {
    backend: Box<dyn Backend>,
}

impl Store {
    /// 用指定后端打开存储。生产装配走这里（`Backend` 由配置决定）。
    pub fn with_kind(kind: StorageKind, path: PathBuf) -> Result<Self, StoreError> {
        Ok(Self {
            backend: backends::open(kind, path)?,
        })
    }

    /// 默认后端（JSON 快照）。
    pub fn open(path: PathBuf) -> Result<Self, StoreError> {
        Self::with_kind(StorageKind::Json, path)
    }

    /// 纯内存存储（不落盘）。
    ///
    /// 直接构造而不是走 `backends::open(...).expect(...)`：内存后端本来就不做 IO，
    /// "不会失败"这件事由 `MemoryBackend::new()` 的返回类型保证，不必在生产路径上
    /// 再留一个 panic 面（三方审查 M2）。
    pub fn in_memory() -> Self {
        Self {
            backend: Box::new(backends::memory::MemoryBackend::new()),
        }
    }

    pub fn kind(&self) -> StorageKind {
        self.backend.kind()
    }

    pub fn path(&self) -> &Path {
        self.backend.path()
    }

    /// 只读访问：闭包在一致视图下执行，禁止在其中做 IO 或阻塞调用。
    pub fn read<T>(&self, f: impl FnOnce(&Db) -> T) -> T {
        let mut slot: Option<T> = None;
        let mut f = Some(f);
        self.backend.with_read(&mut |db| {
            // 后端契约要求恰好调用一次；用 take 保证 FnOnce 语义与"只取一次"。
            if let Some(f) = f.take() {
                slot = Some(f(db));
            }
        });
        match slot {
            Some(v) => v,
            // 走不到这里：`with_read` 的两个实现都必然调用闭包，`read_always_invokes_closure`
            // 单测锁住这条契约。真违约说明是代码 bug，宁可带日志崩，也不要静默返回
            // 一份"看起来读到空数据"的结果——后者比崩更难查（三方审查 M2）。
            None => {
                tracing::error!("存储后端未执行读闭包：Backend 端口违约");
                unreachable!("Backend 端口违约：with_read 未调用闭包")
            }
        }
    }

    /// 写访问：写锁内完成读-改-写，天然原子；返回后自动标记脏页。
    pub fn write<T>(&self, f: impl FnOnce(&mut Db) -> T) -> T {
        let mut slot: Option<T> = None;
        let mut f = Some(f);
        self.backend.with_write(&mut |db| {
            if let Some(f) = f.take() {
                slot = Some(f(db));
            }
        });
        match slot {
            Some(v) => v,
            None => {
                tracing::error!("存储后端未执行写闭包：Backend 端口违约");
                unreachable!("Backend 端口违约：with_write 未调用闭包")
            }
        }
    }

    pub fn flush(&self) -> Result<usize, StoreError> {
        self.backend.flush()
    }

    /// 仅在脏页时落盘，供后台定时任务调用。
    pub fn flush_if_dirty(&self) -> Result<bool, StoreError> {
        self.backend.flush_if_dirty()
    }

    pub fn is_dirty(&self) -> bool {
        self.backend.is_dirty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::user::UserStatus;
    use crate::security::rbac::Role;
    use chrono::Utc;

    fn temp_path() -> PathBuf {
        let dir = std::env::temp_dir().join(format!("sa_store_{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        dir.join("db.json")
    }

    fn sample_user() -> crate::domain::user::User {
        let now = Utc::now();
        crate::domain::user::User {
            id: "u1".into(),
            username: "bob".into(),
            username_lower: "bob".into(),
            email: "bob@example.com".into(),
            email_lower: "bob@example.com".into(),
            display_name: "Bob".into(),
            password_hash: "pbkdf2_sha256$1000$c2FsdA==$aGFzaA==".into(),
            role: Role::Owner,
            status: UserStatus::Active,
            locale: "zh-CN".into(),
            timezone: "Asia/Shanghai".into(),
            created_at: now,
            updated_at: now,
            last_login_at: None,
            failed_login_count: 0,
            locked_until: None,
        }
    }

    #[test]
    fn persists_and_reloads() {
        let path = temp_path();
        {
            let store = Store::open(path.clone()).unwrap();
            store.write(|db| {
                db.users.insert("u1".into(), sample_user());
                db.username_index.insert("bob".into(), "u1".into());
            });
            store.flush().unwrap();
        }
        let reopened = Store::open(path.clone()).unwrap();
        reopened.read(|db| {
            assert_eq!(db.user_count(), 1);
            assert_eq!(db.user_by_username("bob").unwrap().id, "u1");
        });
        let _ = std::fs::remove_dir_all(path.parent().unwrap());
    }

    #[test]
    fn corrupt_snapshot_is_quarantined() {
        let path = temp_path();
        std::fs::write(&path, b"{not-json").unwrap();
        let store = Store::open(path.clone()).unwrap();
        store.read(|db| assert_eq!(db.user_count(), 0));
        let archives: Vec<_> = std::fs::read_dir(path.parent().unwrap())
            .unwrap()
            .filter_map(|e| e.ok())
            .filter(|e| e.file_name().to_string_lossy().contains(".corrupt-"))
            .collect();
        assert_eq!(archives.len(), 1, "损坏快照必须留档");
        let _ = std::fs::remove_dir_all(path.parent().unwrap());
    }

    #[test]
    fn dirty_flag_tracks_writes() {
        let path = temp_path();
        let store = Store::open(path.clone()).unwrap();
        assert!(!store.is_dirty());
        store.write(|db| db.memories.insert("m1".into(), dummy_memory()));
        assert!(store.is_dirty());
        assert!(store.flush_if_dirty().unwrap());
        assert!(!store.is_dirty());
        assert!(!store.flush_if_dirty().unwrap());
        let _ = std::fs::remove_dir_all(path.parent().unwrap());
    }

    /// 模块化核心证据：同一套业务代码（read/write 门面）在两个后端上行为完全一致。
    #[test]
    fn backends_are_interchangeable() {
        for kind in [StorageKind::Json, StorageKind::Memory] {
            let path = temp_path();
            let store = Store::with_kind(kind, path.clone()).unwrap();
            assert_eq!(store.kind(), kind);

            store.write(|db| {
                db.users.insert("u1".into(), sample_user());
                db.username_index.insert("bob".into(), "u1".into());
            });
            store.read(|db| {
                assert_eq!(db.user_count(), 1, "{kind:?} 写后必须读得到");
                assert_eq!(db.user_by_username("bob").unwrap().id, "u1");
            });

            // 分页/统计这类读路径同样与后端无关
            store.read(|db| assert_eq!(db.stats().get("users").copied(), Some(1)));

            store.flush().unwrap();
            if kind.is_durable() {
                let reopened = Store::with_kind(kind, path.clone()).unwrap();
                reopened.read(|db| assert_eq!(db.user_count(), 1, "持久后端重启后数据必须还在"));
            }
            let _ = std::fs::remove_dir_all(path.parent().unwrap());
        }
    }

    /// 内存后端必须诚实：不允许假装落盘。
    #[test]
    fn memory_backend_is_ephemeral() {
        let store = Store::in_memory();
        assert_eq!(store.kind(), StorageKind::Memory);
        assert!(store.path().as_os_str().is_empty(), "内存后端不应有落盘路径");
        store.write(|db| db.users.insert("u1".into(), sample_user()));
        assert_eq!(store.flush().unwrap(), 0, "内存后端落盘字节数必须为 0");
        assert!(!store.is_dirty());
    }

    /// M2 契约锁：`read`/`write` 必须真的把闭包跑一遍并原样交回结果。
    ///
    /// 这条测试的意义不是"再测一遍存取"，而是把 `Backend::with_read/with_write`
    /// 的端口契约钉死：两个后端都不得跳过闭包。哪天新增第三个后端漏调闭包，
    /// `Store::read` 会带着"Backend 端口违约"的日志崩，而不是静默返回空数据。
    #[test]
    fn read_write_always_invoke_closure_and_return_value() {
        for kind in [StorageKind::Json, StorageKind::Memory] {
            let store = match kind {
                StorageKind::Json => Store::open(temp_path())
                    .expect("JSON 后端应可打开"),
                StorageKind::Memory => Store::in_memory(),
            };
            let mut hits = 0;
            let got = store.read(|db| {
                hits += 1;
                db.users.len()
            });
            assert_eq!(hits, 1, "{kind:?}: 读闭包必须恰好执行一次");
            assert_eq!(got, 0, "{kind:?}: 读闭包返回值必须原样交回");

            let mut hits = 0;
            let wrote = store.write(|db| {
                hits += 1;
                db.users.insert("u1".into(), sample_user());
                db.users.len()
            });
            assert_eq!(hits, 1, "{kind:?}: 写闭包必须恰好执行一次");
            assert_eq!(wrote, 1, "{kind:?}: 写闭包返回值必须原样交回");
            assert_eq!(store.read(|db| db.users.len()), 1, "{kind:?}: 写入必须可见");
        }
    }

    /// M2 中毒恢复：持锁线程 panic 一次，不能让锁永中毒、把后续每个请求都拖崩。
    #[test]
    fn poisoned_lock_recovers_instead_of_cascading_panic() {
        use std::sync::Arc;
        for kind in [StorageKind::Json, StorageKind::Memory] {
            let store = Arc::new(match kind {
                StorageKind::Json => Store::open(temp_path())
                    .expect("JSON 后端应可打开"),
                StorageKind::Memory => Store::in_memory(),
            });
            store.write(|db| db.users.insert("keep".into(), sample_user()));

            // 在持写锁的闭包里 panic，让内在锁中毒。
            let s = Arc::clone(&store);
            let hooked = std::thread::spawn(move || {
                let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    s.write(|_db| panic!("故意 panic，制造锁中毒"));
                }));
            });
            hooked.join().expect("panic 应被 catch_unwind 吞掉");

            // 中毒后仍必须可读可写：这是 recover_poison 的全部意义。
            assert_eq!(
                store.read(|db| db.users.len()),
                1,
                "{kind:?}: 锁中毒后读必须恢复，不得级联 panic"
            );
            store.write(|db| db.users.insert("after".into(), sample_user()));
            assert_eq!(store.read(|db| db.users.len()), 2, "{kind:?}: 中毒后写必须生效");
        }
    }

    fn dummy_memory() -> crate::domain::memory::MemoryEntry {
        let now = Utc::now();
        crate::domain::memory::MemoryEntry {
            id: "m1".into(),
            scope: crate::domain::memory::MemoryScope::Private,
            owner_id: "u1".into(),
            key: "k".into(),
            content: "c".into(),
            tags: vec![],
            version: 1,
            written_by: "super-agent".into(),
            created_at: now,
            updated_at: now,
            access_count: 0,
            confidence: 0.8,
        }
    }
}
