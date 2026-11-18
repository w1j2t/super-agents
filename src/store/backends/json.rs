//! JSON 快照后端：内存主存 + 原子落盘。
//!
//! 设计取舍：个人部署数据量小（万级以内），整库快照读写简单可靠；
//! 需要并发写或大数据量时，换 `SqliteBackend` 即可，业务代码零改动。

use super::{Backend, StorageKind};
use crate::store::db::{Db, SCHEMA_VERSION};
use crate::store::{snapshot, StoreError};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Mutex, RwLock, RwLockReadGuard, RwLockWriteGuard};

pub struct JsonBackend {
    inner: RwLock<Db>,
    path: PathBuf,
    dirty: AtomicBool,
    /// 写入代数：每次 `with_write` 自增。
    ///
    /// 存在的唯一理由是堵住"丢脏标记窗口"：`flush` 序列化完之后、清 dirty 之前，
    /// 若又有一次写入把 dirty 置 true，旧实现会用无条件 `dirty.store(false)` 把它抹掉，
    /// 于是这次新写入**永远等不到下一次落盘**（直到下一次有人写）。进程随后被杀就是丢数据。
    /// 有了代数，flush 只在自己进门前后的代数未变时才允许清 dirty。
    generation: AtomicU64,
    /// 落盘串行锁：同进程内不允许两次 flush 交叉（tmp 名已唯一，但串行能省掉无谓的整库重复序列化）。
    flush_lock: Mutex<()>,
}

/// 快照读不出来时的统一收尾：留档、钉住"非首装"标记、从空库启动。
///
/// FIX-003 之前这里只有"解析失败就 quarantine + 空库"，没有任何 schema 与版本判断；
/// FIX-002 之前空库还会自动开放注册。两件事叠在一起就是"写坏一个文件拿 owner"。
fn recover_from_corrupt(path: &Path, err: &str) -> Db {
    tracing::error!(error = err, path = %path.display(), "快照解析失败，转入留档并从空库启动");
    if let Ok(archived) = snapshot::quarantine(path) {
        tracing::error!(archived = %archived.display(), "损坏快照已留档");
    }
    let lock = path
        .parent()
        .unwrap_or_else(|| Path::new("."))
        .join(crate::services::auth::RECOVERY_LOCK_FILE);
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let detail = serde_json::to_string(err).unwrap_or_else(|_| "\"unknown\"".to_string());
    let body = format!("{{\"reason\":\"snapshot_unreadable\",\"unix\":{secs},\"error\":{detail}}}\n");
    match std::fs::write(&lock, body) {
        Ok(_) => tracing::error!(
            lock = %lock.display(),
            "已写入恢复标记：注册自举已钉住，确认数据后再删除该文件"
        ),
        Err(e) => tracing::error!(
            error = %e,
            "恢复标记写入失败；注册默认关闭，请人工确认注册开关状态"
        ),
    }
    Db::default()
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
            // FIX-003：先判版本、再判字段。顺序很要紧——更高 schema 的库被旧程序读到，
            // 缺失字段会被 serde 默认值抹平，程序随后照常写回，等于把新数据洗成旧格式。
            // 所以"版本更高"必须拒绝启动，而不是留档后从空库起（那才是真丢数据）。
            match serde_json::from_slice::<serde_json::Value>(&raw) {
                Ok(value) => {
                    let raw_version = value
                        .get("schema_version")
                        .and_then(|v| v.as_u64())
                        .unwrap_or(0) as u32;
                    if raw_version > SCHEMA_VERSION {
                        return Err(StoreError::Io(format!(
                            "快照 schema_version={raw_version} 高于本程序支持的 {SCHEMA_VERSION}：\
                             拒绝读取（降级读会丢字段并回写洗数据）。请换用新版后端，或改用备份快照。"
                        )));
                    }
                    match serde_json::from_value::<Db>(value) {
                        Ok(mut db) => {
                            if raw_version < SCHEMA_VERSION {
                                tracing::warn!(
                                    from = raw_version,
                                    to = SCHEMA_VERSION,
                                    "快照版本较旧：缺失字段已按默认值补齐，版本号已升级"
                                );
                            }
                            db.schema_version = SCHEMA_VERSION;
                            db
                        }
                        Err(e) => recover_from_corrupt(&path, &e.to_string()),
                    }
                }
                Err(e) => recover_from_corrupt(&path, &e.to_string()),
            }
        } else {
            Db::default()
        };

        Ok(Self {
            inner: RwLock::new(db),
            path,
            dirty: AtomicBool::new(false),
            generation: AtomicU64::new(0),
            flush_lock: Mutex::new(()),
        })
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

impl Backend for JsonBackend {
    fn kind(&self) -> StorageKind {
        StorageKind::Json
    }

    fn path(&self) -> &Path {
        &self.path
    }

    fn with_read(&self, f: &mut dyn FnMut(&Db)) {
        let guard: RwLockReadGuard<Db> = recover_poison(self.inner.read(), "store.json.read");
        f(&guard);
    }

    fn with_write(&self, f: &mut dyn FnMut(&mut Db)) {
        let mut guard: RwLockWriteGuard<Db> = recover_poison(self.inner.write(), "store.json.write");
        f(&mut guard);
        // 顺序不可颠倒：先推进代数再置脏。
        // 若先置脏再推进代数，flush 可能读到旧代数、序列化到新内容、然后因代数变化而
        // 保守保留 dirty —— 只是多一次落盘；反过来则可能把"已序列化"误判成"已覆盖"。
        self.generation.fetch_add(1, Ordering::SeqCst);
        self.dirty.store(true, Ordering::SeqCst);
    }

    /// 强制落盘（写盘失败不 panic，交由调用方决定是否终止）。
    ///
    /// 关键约束：**只有本次序列化覆盖了全部已知写入时**才允许清 dirty。
    /// 判据是代数：`gen0` 在序列化前读，落盘后再读一次，相等才说明期间没有新写入漏在外面。
    fn flush(&self) -> Result<usize, StoreError> {
        let _serial = self.flush_lock.lock().unwrap_or_else(|e| e.into_inner());
        let gen0 = self.generation.load(Ordering::SeqCst);
        let bytes = {
            let guard: RwLockReadGuard<Db> = recover_poison(self.inner.read(), "store.json.read");
            serde_json::to_vec(&*guard).map_err(|e| StoreError::Serde(e.to_string()))?
        };
        let written =
            snapshot::atomic_write(&self.path, &bytes).map_err(|e| StoreError::Io(e.to_string()))?;
        // 代数变了说明"这份快照已经过期"：dirty 必须保持 true，
        // 让后续的 flush_if_dirty 还会再落一次盘，而不是把没写进去的写入永久留在内存里。
        if self.generation.load(Ordering::SeqCst) == gen0 {
            self.dirty.store(false, Ordering::SeqCst);
        } else {
            self.dirty.store(true, Ordering::SeqCst);
        }
        Ok(written)
    }

    /// 仅在脏页时落盘，供后台定时任务调用。
    ///
    /// 不再用 `swap(false)` 提前清脏：一旦落盘失败，中间那段时间的写入会被当成"已落盘"，
    /// 失败回填也只是把 dirty 再置一次，挡不住"落盘期间的写入"被一起清掉。
    fn flush_if_dirty(&self) -> Result<bool, StoreError> {
        if !self.dirty.load(Ordering::SeqCst) {
            return Ok(false);
        }
        self.flush()?;
        Ok(true)
    }

    fn is_dirty(&self) -> bool {
        self.dirty.load(Ordering::SeqCst)
    }
}
