//! 审计：所有授权决策、越权尝试、管理动作留痕（有界队列，防止内存膨胀）。
//!
//! # 可插拔部件（模块化改造 · 第 3 个部件）
//!
//! 端口是 [`AuditSink`]，只要求实现两个方法：`record` 与 `events`。
//! `list` / `page` / `denied_count` 全部由 trait 默认实现基于 `events` 推导，
//! 这样新增一个后端（Loki / CLS / Kafka）时，**实现者只需写 2 个方法**，
//! 分页、过滤、拒绝计数这些语义不会被各后端各写一遍、写出偏差。
//!
//! 现成实现：
//! - [`MemoryAuditSink`]：有界环形队列，进程重启即失（默认）
//! - [`FileAuditSink`]：内存队列 + JSONL 追加落盘，重启可追溯（`SA_AUDIT=file`）
//!
//! 装配点唯一：`Config::from_env` 读环境变量 → `AuditKind::open()`。
//! 业务层（handlers / services / orchestration）只见 `&dyn AuditSink`，不认具体实现。

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::collections::VecDeque;
use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

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

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AuditEvent {
    pub id: String,
    pub at: DateTime<Utc>,
    pub actor: String,
    pub action: String,
    pub target: String,
    pub decision: String,
    pub reason: String,
    pub request_id: String,
}

/// 审计出口端口。实现者只需 `record` + `events`，其余语义由默认实现保证一致。
pub trait AuditSink: Send + Sync {
    /// 记录一条审计事件（不返回错误：审计失败不得阻断业务，由实现自行降级并告警）。
    fn record(
        &self,
        actor: &str,
        action: &str,
        target: &str,
        allowed: bool,
        reason: &str,
        request_id: &str,
    );

    /// 返回全量事件（按入队顺序，最旧在前）。分页与过滤由默认实现负责。
    fn events(&self) -> Vec<AuditEvent>;

    /// 后端名字，用于 `/observability/overview` 暴露"当前审计出口是谁"。
    fn kind(&self) -> &'static str;

    /// 审计流水是否真落盘（进程重启后还在）。
    ///
    /// 为什么端口要暴露这一点：审计的价值几乎全在"事后追查"，一个重启就清空的
    /// 审计出口等于没有审计。`observability/components` 必须能如实标出这件事，
    /// 否则运维会误以为"审计槽位健康 = 审计可信"。
    ///
    /// 默认 `false`：新增实现若忘了声明，宁可被标成"不落盘"也不要被误报健康。
    fn durable(&self) -> bool {
        false
    }

    /// 启动自检：真写一条 boot 记录并回读确认。
    ///
    /// 为什么必须"真写"：`record` 按契约不返回错误，落盘失败只能降级 + 告警。
    /// 于是磁盘满、目录权限被改、挂载点掉线这些情况在启动时**看不出来**——
    /// 进程照常 `healthz=ok`，出事时才发现台账是空的。启动探针把这件事变成硬门槛：
    /// 写不进或读不回，就不许启动。
    ///
    /// 默认 `Ok(())`：内存后端没有"可验证的持久物"，假装自检反而误导。
    fn probe(&self) -> Result<(), String> {
        Ok(())
    }

    /// 运行期是否已降级（后端写失败后只能退回内存），返回原因。
    ///
    /// 与 [`AuditSink::probe`] 分工：probe 管启动那一刻，degraded 管跑起来之后。
    /// 运维要能区分"启动时好好的、跑了三天才写不进"这种情况。
    fn degraded(&self) -> Option<String> {
        None
    }

    /// 因容量上限被挤掉（丢最旧）的审计事件累计条数。
    ///
    /// 为什么端口必须有这个数：审计是"有界环形 + 溢出去最旧"的，容量满了以后
    /// 历史被静默覆盖——这对合规场景是**实质性风险**（要查的那条可能刚好被挤掉）。
    /// 没有这个计数器，"审计丢了"只能靠人猜；有了它，`sa_audit_dropped_total`
    /// 一涨就说明该调大 `SA_AUDIT_CAPACITY` 或该接外部归档了。
    ///
    /// 默认 `0`：新增实现若没能力统计，宁可报 0 被当成"暂不支持"，
    /// 也不要编一个数字出去——错的审计指标比没有更糟。
    fn dropped(&self) -> u64 {
        0
    }

    /// 最新 N 条（可按 actor 过滤）。
    fn list(&self, limit: usize, actor: Option<&str>) -> Vec<AuditEvent> {
        let mut v: Vec<AuditEvent> = self
            .events()
            .into_iter()
            .filter(|e| actor.map(|a| e.actor == a).unwrap_or(true))
            .collect();
        v.reverse();
        v.truncate(limit);
        v
    }

    /// 分页读取：返回 (本页事件, 过滤后的全量条数)。
    fn page(
        &self,
        limit: usize,
        offset: usize,
        actor: Option<&str>,
        decision: Option<&str>,
    ) -> (Vec<AuditEvent>, usize) {
        let filtered: Vec<AuditEvent> = self
            .events()
            .into_iter()
            .filter(|e| actor.map(|a| e.actor == a).unwrap_or(true))
            .filter(|e| decision.map(|d| e.decision == d).unwrap_or(true))
            .collect();
        let total = filtered.len();
        let items = filtered
            .into_iter()
            .rev()
            .skip(offset)
            .take(limit)
            .collect();
        (items, total)
    }

    fn denied_count(&self) -> u64 {
        self.events()
            .iter()
            .filter(|e| e.decision == "deny")
            .count() as u64
    }
}

fn make_event(
    actor: &str,
    action: &str,
    target: &str,
    allowed: bool,
    reason: &str,
    request_id: &str,
) -> AuditEvent {
    AuditEvent {
        id: uuid::Uuid::new_v4().to_string(),
        at: Utc::now(),
        actor: actor.to_string(),
        action: action.to_string(),
        target: target.to_string(),
        decision: if allowed { "allow" } else { "deny" }.to_string(),
        reason: reason.to_string(),
        request_id: request_id.to_string(),
    }
}

fn log_deny(event: &AuditEvent) {
    if event.decision != "allow" {
        tracing::warn!(
            actor = %event.actor,
            action = %event.action,
            target = %event.target,
            reason = %event.reason,
            request_id = %event.request_id,
            "审计：权限拒绝"
        );
    }
}

/// 内存审计出口：有界环形队列，溢出丢最旧。
pub struct MemoryAuditSink {
    capacity: usize,
    events: Mutex<VecDeque<AuditEvent>>,
    /// 被容量挤掉的条数（真值在此，`/metrics` 抓取时用 counter_set 对齐绝对值）。
    dropped: AtomicU64,
}

impl MemoryAuditSink {
    pub fn new(capacity: usize) -> Self {
        Self {
            capacity: capacity.max(64),
            events: Mutex::new(VecDeque::new()),
            dropped: AtomicU64::new(0),
        }
    }

    /// 有界入队（溢出去最旧）。供本类型与委托类型（如 `FileAuditSink`）共用，
    /// 保证"有界 + 丢最旧"这条语义只有一份实现。
    ///
    /// 每挤掉一条就 `dropped + 1`：这是"审计覆盖了历史"的唯一证据来源。
    /// 用原子变量而不是加锁累加——它在持锁路径里，多一次 Mutex 会白白拉长临界区。
    pub(crate) fn push_bounded(&self, event: AuditEvent) {
        let mut guard = recover_poison(self.events.lock(), "audit.events");
        guard.push_back(event);
        while guard.len() > self.capacity {
            guard.pop_front();
            self.dropped.fetch_add(1, Ordering::Relaxed);
        }
    }
}

impl AuditSink for MemoryAuditSink {
    fn record(
        &self,
        actor: &str,
        action: &str,
        target: &str,
        allowed: bool,
        reason: &str,
        request_id: &str,
    ) {
        let event = make_event(actor, action, target, allowed, reason, request_id);
        log_deny(&event);
        self.push_bounded(event);
    }

    fn events(&self) -> Vec<AuditEvent> {
        self.events
            .lock()
            .map(|g| g.iter().cloned().collect())
            .unwrap_or_default()
    }

    fn kind(&self) -> &'static str {
        "memory"
    }

    fn dropped(&self) -> u64 {
        self.dropped.load(Ordering::Relaxed)
    }
}

/// 文件审计出口：内存队列 + JSONL 追加落盘（重启后仍可追溯）。
///
/// 落盘失败**不阻断业务**：首次失败打一条 warn，之后静默降级为纯内存，
/// 避免磁盘异常把整个后端拖垮——审计是旁路，不是主链路。
pub struct FileAuditSink {
    inner: MemoryAuditSink,
    path: PathBuf,
    file: Mutex<Option<File>>,
    warn_once: AtomicBool,
    /// 首次写失败的原因（有就代表已降级为纯内存）；只在第一次记，避免刷屏
    last_error: Mutex<Option<String>>,
}

/// 启动自检写入的动作名。固定在台账里就能当"重启时间线"用：
/// 一条 `audit.boot` 就是一次进程启动，配合 `at` 字段可以直接对账。
const BOOT_ACTION: &str = "audit.boot";

/// 启动回读上限：避免把几个 G 的历史台账一次性读进内存（只取尾部，够运维排查即可）。
const MAX_LOAD_BYTES: u64 = 8 * 1024 * 1024;

/// 回读已有 JSONL，填回内存队列。
///
/// 三个必须守住的细节：
/// 1. 文件过大只读尾部，且丢弃可能被截断的首行；
/// 2. 半截行 / 坏行**跳过而不 panic**，一条脏数据不能让整个后端起不来；
/// 3. 只保留最后 `capacity` 条，与运行期有界语义一致。
fn load_tail(path: &Path, capacity: usize) -> Vec<AuditEvent> {
    let mut buf: Vec<u8> = Vec::new();
    let mut truncated = false;
    if let Ok(mut f) = File::open(path) {
        let len = f.metadata().map(|m| m.len()).unwrap_or(0);
        if len > MAX_LOAD_BYTES {
            truncated = f.seek(SeekFrom::End(-(MAX_LOAD_BYTES as i64))).is_ok();
        }
        if f.read_to_end(&mut buf).is_err() {
            return Vec::new();
        }
    } else {
        return Vec::new();
    }
    let text = String::from_utf8_lossy(&buf);
    let mut lines: Vec<&str> = text.lines().collect();
    if truncated && !lines.is_empty() {
        lines.remove(0);
    }
    let mut out: VecDeque<AuditEvent> = VecDeque::new();
    for line in lines {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        if let Ok(ev) = serde_json::from_str::<AuditEvent>(line) {
            out.push_back(ev);
            while out.len() > capacity.max(64) {
                out.pop_front();
            }
        }
    }
    out.into_iter().collect()
}

impl FileAuditSink {
    pub fn open(capacity: usize, path: PathBuf) -> std::io::Result<Self> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let file = OpenOptions::new().create(true).append(true).open(&path)?;
        let inner = MemoryAuditSink::new(capacity);
        // 重启后仍能查到历史台账 —— 否则"落盘"只是个好听的名字。
        for ev in load_tail(&path, inner.capacity) {
            inner.push_bounded(ev);
        }
        Ok(Self {
            inner,
            path,
            file: Mutex::new(Some(file)),
            warn_once: AtomicBool::new(false),
            last_error: Mutex::new(None),
        })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// 记下"已经写不进去了"这件事。只保留第一条原因：
    /// 磁盘满之后每一次 append 都会失败，把每次都存下来没有意义，反而吃内存。
    fn mark_degraded(&self, reason: &str) {
        if let Ok(mut slot) = self.last_error.lock() {
            if slot.is_none() {
                *slot = Some(reason.to_string());
            }
        }
        if !self.warn_once.swap(true, Ordering::Relaxed) {
            tracing::warn!(path = %self.path.display(), reason = %reason, "审计落盘失败，已降级为纯内存审计");
        }
    }

    /// 严格追加：把失败**交给调用方**。
    ///
    /// 与 [`FileAuditSink::append`] 的分工：业务路径不能因为审计失败而中断（契约要求），
    /// 所以那边吞掉错误；启动探针恰恰相反——它存在的全部意义就是拿到这个错误。
    /// 同一个写入动作，两种错误处理策略，所以抽成两层的写法。
    fn append_strict(&self, event: &AuditEvent) -> Result<(), String> {
        let mut guard = self
            .file
            .lock()
            .map_err(|_| "审计文件句柄锁已中毒".to_string())?;
        let Some(file) = guard.as_mut() else {
            return Err("审计文件句柄不可用".to_string());
        };
        let line = serde_json::to_string(event).map_err(|e| format!("审计事件序列化失败：{e}"))?;
        writeln!(file, "{line}")
            .and_then(|_| file.flush())
            .map_err(|e| format!("写审计文件失败：{e}"))
    }

    fn append(&self, event: &AuditEvent) {
        if let Err(err) = self.append_strict(event) {
            self.mark_degraded(&err);
        }
    }
}

impl AuditSink for FileAuditSink {
    fn record(
        &self,
        actor: &str,
        action: &str,
        target: &str,
        allowed: bool,
        reason: &str,
        request_id: &str,
    ) {
        let event = make_event(actor, action, target, allowed, reason, request_id);
        log_deny(&event);
        self.inner.push_bounded(event.clone());
        self.append(&event);
    }

    fn events(&self) -> Vec<AuditEvent> {
        self.inner.events()
    }

    fn kind(&self) -> &'static str {
        "file"
    }

    /// 委托给内存环形队列：file 后端同样是"内存有界 + 溢出去最旧"，
    /// 落盘的 JSONL 会无限增长但运行期查询只看内存尾部，所以"覆盖了历史"
    /// 这条对两者含义相同，不该各算一套。
    fn dropped(&self) -> u64 {
        self.inner.dropped()
    }

    /// 逐条 append 到 jsonl，重启后能读回尾巴 —— 这是 file 后端存在的全部理由。
    fn durable(&self) -> bool {
        true
    }

    /// 自检三步：写一条 boot 记录 → **重新打开文件回读尾部** → 确认那条 id 在里面。
    ///
    /// 为什么回读要重新读盘而不是看内存队列：内存队列在写入前就已经 push 了，
    /// 它只能证明"代码执行到了这里"，证明不了"字节落到磁盘上"。磁盘满 / 权限变更 /
    /// 网络盘掉线时，`flush()` 也可能成功返回而数据仍在页缓存里被丢弃——
    /// 所以这里信的是"再打开一次文件能读到"，这是最接近真实读者视角的证明。
    fn probe(&self) -> Result<(), String> {
        let boot = make_event(
            "system",
            BOOT_ACTION,
            &self.path.display().to_string(),
            true,
            "启动自检：确认审计台账可写可读",
            "boot",
        );
        // 先入内存队列：让这次启动在 /observability/audit 里也能被看到，不只是磁盘上有
        self.inner.push_bounded(boot.clone());

        if let Err(err) = self.append_strict(&boot) {
            self.mark_degraded(&err);
            return Err(format!("{} 不可写：{err}", self.path.display()));
        }

        // 只回读尾部 64 条：boot 记录刚写进去，一定在尾巴上，不必扫全文件
        let found = load_tail(&self.path, 64).iter().any(|e| e.id == boot.id);
        if !found {
            let err = format!("回读 {} 时找不到刚写入的 boot 记录", self.path.display());
            self.mark_degraded(&err);
            return Err(err);
        }
        Ok(())
    }

    fn degraded(&self) -> Option<String> {
        self.last_error.lock().ok().and_then(|slot| slot.clone())
    }
}

/// 审计后端注册表（可插拔部件的装配点）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AuditKind {
    Memory,
    File,
}

impl AuditKind {
    /// 全部合法取值（与 `parse` / `open` 必须同步，静态检查会核对）。
    pub const ALL: [&'static str; 2] = ["memory", "file"];

    pub fn as_str(&self) -> &'static str {
        match self {
            AuditKind::Memory => "memory",
            AuditKind::File => "file",
        }
    }

    pub fn parse(raw: &str) -> Option<Self> {
        match raw.trim().to_ascii_lowercase().as_str() {
            "memory" | "mem" | "ephemeral" => Some(AuditKind::Memory),
            "file" | "jsonl" | "disk" | "durable" => Some(AuditKind::File),
            _ => None,
        }
    }

    /// 构造审计出口。`file` 必须给可用路径，否则返回错误由启动流程拒启——不静默退回内存。
    pub fn open(
        &self,
        capacity: usize,
        path: Option<PathBuf>,
    ) -> Result<Arc<dyn AuditSink>, String> {
        match self {
            AuditKind::Memory => Ok(Arc::new(MemoryAuditSink::new(capacity))),
            AuditKind::File => {
                let path = path.ok_or_else(|| "SA_AUDIT=file 时必须提供 SA_AUDIT_FILE".to_string())?;
                FileAuditSink::open(capacity, path.clone())
                    .map(|s| Arc::new(s) as Arc<dyn AuditSink>)
                    .map_err(|e| format!("审计文件不可用 {}: {e}", path.display()))
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp_jsonl() -> PathBuf {
        let dir = std::env::temp_dir().join(format!("sa_audit_{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        dir.join("audit.jsonl")
    }

    /// 落盘不是目的，重启后还能查到才是目的。
    #[test]
    fn file_sink_reload_after_restart() {
        let path = tmp_jsonl();
        {
            let s = FileAuditSink::open(200, path.clone()).unwrap();
            for i in 0..5 {
                s.record("u1", "task.run", "t1", i % 2 == 0, "drill", "req-1");
            }
        }
        let s2 = FileAuditSink::open(200, path).unwrap();
        assert_eq!(s2.events().len(), 5, "重启后应回读到 5 条历史审计");
        assert_eq!(s2.denied_count(), 2, "deny 计数应跨重启保持一致");
        assert_eq!(s2.kind(), "file");
    }

    /// 一条坏行不能毁掉整个台账，也不能让进程起不来。
    #[test]
    fn file_sink_skips_corrupt_lines() {
        let path = tmp_jsonl();
        std::fs::write(&path, "{\"broken\": \nnot json at all\n").unwrap();
        let s = FileAuditSink::open(200, path).unwrap();
        assert!(s.events().is_empty(), "坏行必须被跳过");
        s.record("u2", "auth.login", "t2", false, "密码错误", "req-2");
        assert_eq!(s.events().len(), 1, "坏行之后仍能正常追加");
    }

    /// 回读也要守有界语义：不能因为历史文件长就把内存撑爆。
    #[test]
    fn file_sink_reload_respects_capacity() {
        let path = tmp_jsonl();
        {
            let s = FileAuditSink::open(64, path.clone()).unwrap();
            for i in 0..100 {
                s.record("u3", "mem.write", "k", true, "", &format!("r{i}"));
            }
        }
        let s2 = FileAuditSink::open(64, path).unwrap();
        assert_eq!(s2.events().len(), 64, "只保留最后 capacity 条");
        let last = s2.events().pop().unwrap();
        assert_eq!(last.request_id, "r99", "保留的必须是最后一条");
    }

    /// 启动自检必须真的留下痕迹：磁盘上要有 boot 行，重启后还能读到。
    #[test]
    fn probe_writes_boot_record_that_survives_restart() {
        let path = tmp_jsonl();
        let s = FileAuditSink::open(200, path.clone()).unwrap();
        s.probe().expect("正常目录必须自检通过");

        let text = std::fs::read_to_string(&path).unwrap();
        let last = text.lines().last().expect("磁盘上必须有 boot 行");
        let ev: AuditEvent = serde_json::from_str(last).unwrap();
        assert_eq!(ev.action, BOOT_ACTION, "最后一行必须是启动自检记录");
        assert_eq!(ev.actor, "system");
        assert_eq!(ev.decision, "allow", "自检不得计入拒绝数");
        assert_eq!(s.denied_count(), 0);

        // 换个实例回读：证明它是"落盘"的，不是只活在内存里
        let s2 = FileAuditSink::open(200, path).unwrap();
        assert!(s2.events().iter().any(|e| e.action == BOOT_ACTION));
        assert_eq!(s2.degraded(), None, "写成功就不能报降级");
    }

    /// 写不进去时必须**报错**而不是静默降级 —— 否则启动探针就失去了全部意义。
    #[test]
    fn probe_fails_loudly_when_file_handle_is_unusable() {
        let path = tmp_jsonl();
        let s = FileAuditSink::open(200, path).unwrap();
        // 模拟句柄失效（文件被外部锁死 / 磁盘掉线后的真实后果）
        *s.file.lock().unwrap() = None;

        let err = s.probe().expect_err("句柄不可用必须自检失败");
        assert!(err.contains("不可写"), "错误信息要能直接说明问题：{err}");
        assert!(s.degraded().is_some(), "写失败必须被记成降级，供 readyz 暴露");
    }

    /// 内存后端没有可验证的持久物：默认实现如实返回 Ok + 不报降级。
    #[test]
    fn memory_sink_probe_is_a_noop() {
        let s = MemoryAuditSink::new(10);
        assert!(s.probe().is_ok());
        assert_eq!(s.degraded(), None);
        assert!(!s.durable());
    }

    fn sink() -> MemoryAuditSink {
        MemoryAuditSink::new(100)
    }

    #[test]
    fn record_and_query() {
        let s = sink();
        s.record("u1", "memory.create", "m1", true, "", "r1");
        s.record("u2", "memory.delete", "m1", false, "越权", "r2");
        assert_eq!(s.list(10, None).len(), 2);
        assert_eq!(s.list(10, None)[0].action, "memory.delete", "最新的在前");
        assert_eq!(s.list(10, Some("u1")).len(), 1);
        assert_eq!(s.denied_count(), 1);
    }

    #[test]
    fn page_and_total() {
        let s = sink();
        for i in 0..5 {
            s.record("u1", "auth.login", "u1", true, "", &format!("r{i}"));
        }
        let (items, total) = s.page(2, 1, None, None);
        assert_eq!(total, 5);
        assert_eq!(items.len(), 2);
        assert_eq!(items[0].request_id, "r3", "倒序后 skip(1)");
    }

    #[test]
    fn capacity_drops_oldest() {
        let s = MemoryAuditSink::new(64);
        for i in 0..70 {
            s.record("u", "a", "t", true, "", &i.to_string());
        }
        let all = s.events();
        assert_eq!(all.len(), 64);
        assert_eq!(all[0].request_id, "6", "最旧的 6 条被挤出");
        // 被挤掉的条数必须与"总写入 - 现存"逐位相等：
        // 这个数字是 `sa_audit_dropped_total` 的唯一真值来源，少算一条就意味着
        // 合规场景下会漏报一条"已被覆盖的历史"。
        assert_eq!(s.dropped(), 6);
    }

    /// 未溢出的正常情况下必须恒为 0 —— 否则一旦把"写入"误算成"丢弃"，
    /// 告警会在审计完全健康时持续尖叫，很快就没人看它了。
    #[test]
    fn dropped_stays_zero_until_capacity_is_exceeded() {
        let s = MemoryAuditSink::new(64);
        for i in 0..64 {
            s.record("u", "a", "t", true, "", &i.to_string());
        }
        assert_eq!(s.dropped(), 0, "刚好写满不算丢弃");
        s.record("u", "a", "t", true, "", "overflow");
        assert_eq!(s.dropped(), 1, "第 65 条才挤掉 1 条");
    }

    /// file 后端与 memory 后端在"丢弃"口径上必须一致（委托而非各算一套）。
    #[test]
    fn file_backend_delegates_dropped_counter() {
        let tmp = std::env::temp_dir().join(format!("audit-drop-{}.jsonl", uuid::Uuid::new_v4()));
        let f = FileAuditSink::open(64, tmp).expect("文件审计应可创建");
        for i in 0..70 {
            f.record("u", "a", "t", true, "", &i.to_string());
        }
        assert_eq!(f.dropped(), 6, "file 后端要与 memory 后端同口径");
    }

    #[test]
    fn default_trait_methods_give_same_answers_as_memory_backend() {
        // 两个后端在 list/page/denied_count 上必须给出完全相同的结果（口径一致性）
        let m = MemoryAuditSink::new(100);
        let tmp = std::env::temp_dir().join(format!("audit-test-{}.jsonl", uuid::Uuid::new_v4()));
        let f = FileAuditSink::open(100, tmp.clone()).expect("文件审计应可创建");
        for (i, allowed) in [(0, true), (1, false), (2, true)] {
            m.record("u", "act", "t", allowed, "why", &i.to_string());
            f.record("u", "act", "t", allowed, "why", &i.to_string());
        }
        assert_eq!(m.denied_count(), f.denied_count());
        assert_eq!(m.list(2, None).len(), f.list(2, None).len());
        assert_eq!(m.page(1, 1, None, Some("allow")).1, f.page(1, 1, None, Some("allow")).1);
        // 文件后端真的落盘了：行数 = 事件数
        let lines = std::fs::read_to_string(&tmp).unwrap().lines().count();
        assert_eq!(lines, 3);
        let _ = std::fs::remove_file(&tmp);
    }

    #[test]
    fn audit_kind_registry_roundtrip() {
        for name in AuditKind::ALL.iter() {
            let k = AuditKind::parse(name).expect("ALL 里的每个值都必须能被 parse");
            assert_eq!(k.as_str(), *name, "ALL 与 as_str 必须一一对应");
        }
        assert!(AuditKind::parse("kafka").is_none(), "未实现的后端必须拒绝，不得静默降级");
    }

    #[test]
    fn file_kind_requires_path() {
        let err = match AuditKind::File.open(10, None) {
            Ok(_) => panic!("缺路径必须失败，不得静默降级"),
            Err(e) => e,
        };
        assert!(err.contains("SA_AUDIT_FILE"));
        let sink = AuditKind::File.open(10, Some(std::env::temp_dir().join("sa-audit-req.jsonl")));
        assert_eq!(sink.unwrap().kind(), "file");
    }
}
