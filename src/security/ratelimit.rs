//! 令牌桶限流：按 key（IP / 用户 / IP+账号）独立计桶，超限返回 Retry-After。
//!
//! # 可插拔限流后端
//!
//! `Limiter` 是端口，装谁由 `SA_LIMITER` 决定：
//!
//! | 值 | 实现 | 用途 |
//! |----|------|------|
//! | `memory`（默认） | `RateLimiter` 进程内令牌桶 | 生产 |
//! | `noop` | `NoopLimiter` 一律放行 | 压测基线、只想关限流的调试 |
//!
//! 端口方法 `acquire` 返回 `LimitOutcome`（是否放行 / 需等待秒数 / 剩余配额），
//! 同时保留默认方法 `check`，让既有调用点（`auth`、`task`）一行都不用改。

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::Instant;

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

#[derive(Debug, Clone, Copy)]
pub struct Quota {
    pub capacity: f64,
    pub refill_per_sec: f64,
}

impl Quota {
    pub const fn new(capacity: f64, refill_per_sec: f64) -> Self {
        Self {
            capacity,
            refill_per_sec,
        }
    }

    /// 多节点部署的配额切分：把单机配额按节点数等分。
    ///
    /// 进程内令牌桶的各节点状态互不可见，N 个节点等于把配额放大 N 倍（三方审查 H3：
    /// 登录限流是暴力破解的唯一防线，放大 N 倍等于防线失效）。切分后
    /// 「N 节点合计 ≈ 单机配额」，绕过成本回到预期。
    ///
    /// 容量至少留 1：切到 0 会让所有请求被静默拒绝，这种"限流成拒绝服务"比绕过更难查。
    pub fn shard(self, nodes: u32) -> Self {
        let n = nodes.max(1) as f64;
        if n <= 1.0 {
            return self;
        }
        Self {
            capacity: (self.capacity / n).max(1.0),
            refill_per_sec: (self.refill_per_sec / n).max(0.000_001),
        }
    }
}

/// 一次取令牌的结果。
/// 桶表容量硬上限（FIX-006）。取 2 万：正常单机使用（每用户几个键）远达不到，
/// 而攻击者刷键空间时能尽早触发"过载拒绝"，而不是把内存吃光。
const MAX_BUCKETS: usize = 20_000;

/// 限流键长度上限（FIX-006）。键里带 IP / user_id 这类外部输入，
/// 不截断就等于让外部决定单个桶占多少内存。
const MAX_KEY_BYTES: usize = 128;

/// 按字符边界截断限流键：直接按字节切会切碎 UTF-8 触发 panic。
fn clip_key(key: &str) -> &str {
    if key.len() <= MAX_KEY_BYTES {
        return key;
    }
    let mut end = MAX_KEY_BYTES;
    while end > 0 && !key.is_char_boundary(end) {
        end -= 1;
    }
    &key[..end]
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct LimitOutcome {
    pub allowed: bool,
    /// 被拒时需要等待的秒数（放行时为 0）
    pub retry_after_secs: u64,
    /// 本次扣减后剩余可用次数（向下取整）
    pub remaining: u32,
    /// 该桶的容量（把配额告诉调用方，前端才能画"还剩几次"）
    pub limit: u32,
}

impl LimitOutcome {
    pub fn allowed(remaining: u32, limit: u32) -> Self {
        Self {
            allowed: true,
            retry_after_secs: 0,
            remaining,
            limit,
        }
    }
}

// ---------------------------------------------------------------------------
// 端口
// ---------------------------------------------------------------------------

/// 限流端口：所有限流后端都要满足它。
pub trait Limiter: Send + Sync {
    /// 取一个令牌。
    fn acquire(&self, key: &str, quota: Quota) -> LimitOutcome;
    /// 部件名（注册表展示用）。
    fn kind(&self) -> &'static str;
    /// 是否真的在限流（`false` = 装的是空实现，注册表要如实标出）。
    fn enforces(&self) -> bool {
        true
    }

    /// 兼容旧调用姿势：放行 `Ok(())`，被拒 `Err(需等待秒数)`。
    fn check(&self, key: &str, quota: Quota) -> Result<(), u64> {
        let outcome = self.acquire(key, quota);
        if outcome.allowed {
            Ok(())
        } else {
            Err(outcome.retry_after_secs)
        }
    }
}

// ---------------------------------------------------------------------------
// 实现一：进程内令牌桶（生产默认）
// ---------------------------------------------------------------------------

struct Bucket {
    tokens: f64,
    last: Instant,
}

pub struct RateLimiter {
    buckets: Mutex<HashMap<String, Bucket>>,
    /// 集群节点数（单机恒为 1）。构造时确定：扩容后需重启才重算，
    /// 否则"运行中节点数变了、配额跟着抖"会让限流行为不可复现。
    shard: u32,
}

impl Default for RateLimiter {
    fn default() -> Self {
        Self::new()
    }
}

impl RateLimiter {
    pub fn new() -> Self {
        Self::with_shard(1)
    }

    /// 集群模式构造：`nodes` 为活跃节点总数（含自身）。
    pub fn with_shard(nodes: u32) -> Self {
        Self {
            buckets: Mutex::new(HashMap::new()),
            shard: nodes.max(1),
        }
    }

    /// 当前分片数（排障/门禁断言用）。
    pub fn shard(&self) -> u32 {
        self.shard
    }

    /// 当前桶数量（注册表 / 排障时看"是不是桶爆了"）。
    pub fn bucket_count(&self) -> usize {
        self.buckets.lock().map(|m| m.len()).unwrap_or(0)
    }
}

impl Limiter for RateLimiter {
    fn acquire(&self, key: &str, quota: Quota) -> LimitOutcome {
        // 多节点时按节点数等分配额，保证总量不随节点数放大
        let quota = quota.shard(self.shard);
        let now = Instant::now();
        let limit = quota.capacity.max(0.0) as u32;
        let key = clip_key(key);
        let mut map = recover_poison(self.buckets.lock(), "ratelimit.buckets");
        // FIX-006：桶表必须有硬上限，而且上限不能只靠"清理过期"来保证。
        // 旧实现只在 len > 10_000 时 retain 一次：当键全是新近创建的（刷 login:{ip}、
        // refresh:{sub}、register:{ip} 这类带攻击者可控成分的键）时过期清理等于空转，
        // 表会一路长到 OOM。现在的顺序是：表满 → 先清过期 → 仍满则按"过载"拒绝。
        // 拒绝而不是放行，是因为"内存快被撑爆"时放行等于把限流器本身变成攻击面。
        if !map.contains_key(key) && map.len() >= MAX_BUCKETS {
            map.retain(|_, b| now.duration_since(b.last).as_secs() < 3600);
            if map.len() >= MAX_BUCKETS {
                tracing::warn!(
                    buckets = map.len(),
                    key = key,
                    "限流桶表已满，新键被拒绝（疑似对注册/登录/刷新接口的键空间扫描）"
                );
                return LimitOutcome {
                    allowed: false,
                    retry_after_secs: 60,
                    remaining: 0,
                    limit,
                };
            }
        }
        let bucket = map.entry(key.to_string()).or_insert(Bucket {
            tokens: quota.capacity,
            last: now,
        });
        let elapsed = now.duration_since(bucket.last).as_secs_f64();
        bucket.last = now;
        bucket.tokens = (bucket.tokens + elapsed * quota.refill_per_sec).min(quota.capacity);

        if bucket.tokens >= 1.0 {
            bucket.tokens -= 1.0;
            LimitOutcome::allowed(bucket.tokens.floor() as u32, limit)
        } else {
            let need = 1.0 - bucket.tokens;
            let wait = (need / quota.refill_per_sec.max(0.0001)).ceil() as u64;
            LimitOutcome {
                allowed: false,
                retry_after_secs: wait.max(1),
                remaining: 0,
                limit,
            }
        }
    }

    fn kind(&self) -> &'static str {
        "memory"
    }
}

// ---------------------------------------------------------------------------
// 实现二：空限流（一律放行）
// ---------------------------------------------------------------------------

/// 一律放行。
///
/// 用途：压测拿"无限流基线"，或临时排查"是不是限流把正常请求挡了"。
/// 装它等于**对外不设防**，所以 `enforces()` 返回 `false`，注册表会明确标出"未限流"。
pub struct NoopLimiter;

impl Default for NoopLimiter {
    fn default() -> Self {
        Self::new()
    }
}

impl NoopLimiter {
    pub fn new() -> Self {
        Self
    }
}

impl Limiter for NoopLimiter {
    fn acquire(&self, _key: &str, quota: Quota) -> LimitOutcome {
        LimitOutcome::allowed(u32::MAX, quota.capacity.max(0.0) as u32)
    }

    fn kind(&self) -> &'static str {
        "noop"
    }

    fn enforces(&self) -> bool {
        false
    }
}

// ---------------------------------------------------------------------------
// 部件选择
// ---------------------------------------------------------------------------

/// 限流部件（`SA_LIMITER`）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LimiterKind {
    Memory,
    Noop,
}

impl LimiterKind {
    pub const ALL: [&'static str; 2] = ["memory", "noop"];

    pub fn as_str(&self) -> &'static str {
        match self {
            LimiterKind::Memory => "memory",
            LimiterKind::Noop => "noop",
        }
    }

    pub fn parse(s: &str) -> Option<Self> {
        match s.trim().to_ascii_lowercase().as_str() {
            "memory" | "token_bucket" | "tokenbucket" => Some(LimiterKind::Memory),
            "noop" | "off" | "none" => Some(LimiterKind::Noop),
            _ => None,
        }
    }

    pub fn build(self) -> std::sync::Arc<dyn Limiter> {
        self.build_sharded(1)
    }

    /// 集群模式：按活跃节点数切分配额（`Noop` 不受影响——它本来就放行）。
    pub fn build_sharded(self, nodes: u32) -> std::sync::Arc<dyn Limiter> {
        match self {
            LimiterKind::Memory => std::sync::Arc::new(RateLimiter::with_shard(nodes)),
            LimiterKind::Noop => std::sync::Arc::new(NoopLimiter::new()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cluster_shard_divides_quota_instead_of_multiplying_it() {
        // H3：不切分时 4 个节点等于把配额放大 4 倍；切分后单节点只拿 1/4
        let base = Quota::new(10.0, 10.0 / 60.0);
        assert_eq!(base.shard(1).capacity, 10.0, "单机不切分，行为与改动前一致");

        let sharded = base.shard(4);
        assert!((sharded.capacity - 2.5).abs() < 1e-9, "容量按节点数等分");
        assert!(
            (sharded.refill_per_sec * 4.0 - base.refill_per_sec).abs() < 1e-9,
            "补充速率同样等分，N 节点合计 ≈ 单机"
        );

        // 容量最少留 1：否则节点多到一定程度就变成"全员拒绝"
        assert_eq!(base.shard(1000).capacity, 1.0);
        assert_eq!(base.shard(0).capacity, 10.0, "0 节点按单机处理，不做除法恐慌");
    }

    #[test]
    fn sharded_limiter_gives_each_node_a_quarter() {
        let q = Quota::new(4.0, 0.0);
        let a = RateLimiter::with_shard(4);
        let b = RateLimiter::with_shard(4);
        assert_eq!(a.shard(), 4);
        // 每个节点只放行 1 次（4/4），而不是 4 次
        assert!(a.acquire("login:1.2.3.4", q).allowed);
        assert!(!a.acquire("login:1.2.3.4", q).allowed);
        // 两个节点合计仍只有 2 次（4 节点总配额 4 次）
        assert!(b.acquire("login:1.2.3.4", q).allowed);
        assert!(!b.acquire("login:1.2.3.4", q).allowed);
        // 单机实例照旧拿满 4 次
        let solo = RateLimiter::new();
        for _ in 0..4 {
            assert!(solo.acquire("login:1.2.3.4", q).allowed);
        }
        assert!(!solo.acquire("login:1.2.3.4", q).allowed);
    }

    #[test]
    fn burst_then_throttle() {
        let limiter = RateLimiter::new();
        let quota = Quota::new(3.0, 0.001);
        for _ in 0..3 {
            assert!(limiter.check("k", quota).is_ok());
        }
        assert!(limiter.check("k", quota).is_err());
    }

    #[test]
    fn keys_are_isolated() {
        let limiter = RateLimiter::new();
        let quota = Quota::new(1.0, 0.001);
        assert!(limiter.check("a", quota).is_ok());
        assert!(limiter.check("b", quota).is_ok());
        assert!(limiter.check("a", quota).is_err());
    }

    /// 任务提交的实际上线口径：每用户每小时 60 次。这里锁定该常量的数值语义，
    /// 避免后续有人"顺手调大"而无感知。
    #[test]
    fn task_submit_quota_is_60_per_hour_per_user() {
        use crate::api::handlers::task::TASK_SUBMIT_QUOTA;

        assert_eq!(TASK_SUBMIT_QUOTA.capacity, 60.0, "每小时窗口容量应为 60");
        assert!(
            (TASK_SUBMIT_QUOTA.refill_per_sec - 60.0 / 3600.0).abs() < f64::EPSILON,
            "回填速率应为 60 次/3600 秒"
        );

        let limiter = RateLimiter::new();
        let key = "task_submit:user-1";
        for i in 0..60 {
            assert!(
                limiter.check(key, TASK_SUBMIT_QUOTA).is_ok(),
                "第 {} 次提交应在限额内",
                i + 1
            );
        }
        let wait = limiter
            .check(key, TASK_SUBMIT_QUOTA)
            .expect_err("第 61 次必须被限流");
        assert_eq!(wait, 60, "耗尽后需等待约 60 秒才能攒回 1 个令牌");

        // 另一个用户不受影响：限流按用户维度隔离
        assert!(limiter.check("task_submit:user-2", TASK_SUBMIT_QUOTA).is_ok());
    }

    /// `acquire` 要如实报告剩余配额 —— 前端"还剩几次"全靠它。
    #[test]
    fn acquire_reports_remaining_quota() {
        let limiter = RateLimiter::new();
        let quota = Quota::new(5.0, 0.0);
        let first = limiter.acquire("k", quota);
        assert!(first.allowed);
        assert_eq!(first.limit, 5);
        assert_eq!(first.remaining, 4, "取走一个后应剩 4");
        for _ in 0..4 {
            limiter.acquire("k", quota);
        }
        let denied = limiter.acquire("k", quota);
        assert!(!denied.allowed);
        assert_eq!(denied.remaining, 0);
        assert!(denied.retry_after_secs >= 1);
    }

    /// 换限流后端不殃及调用方：同一段代码跑在两种实现上都编译、都工作。
    #[test]
    fn limiter_backends_are_interchangeable() {
        let quota = Quota::new(1.0, 0.0);
        for backend in [LimiterKind::Memory, LimiterKind::Noop] {
            let limiter = backend.build();
            assert_eq!(limiter.kind(), backend.as_str());
            assert_eq!(limiter.check("x", quota).is_ok(), true);
            // 第二次：memory 拒、noop 仍放行 —— 行为差异正是"换部件"的意义
            let second = limiter.acquire("x", quota);
            match backend {
                LimiterKind::Memory => assert!(!second.allowed),
                LimiterKind::Noop => assert!(second.allowed),
            }
        }
    }

    #[test]
    fn noop_limiter_declares_it_does_not_enforce() {
        assert!(!NoopLimiter::new().enforces());
        assert!(RateLimiter::new().enforces());
    }

    #[test]
    fn kind_parsing_accepts_aliases_and_rejects_garbage() {
        assert_eq!(LimiterKind::parse("memory"), Some(LimiterKind::Memory));
        assert_eq!(LimiterKind::parse(" Token_Bucket "), Some(LimiterKind::Memory));
        assert_eq!(LimiterKind::parse("off"), Some(LimiterKind::Noop));
        assert_eq!(LimiterKind::parse("redis"), None);
    }
}
