//! FIX-010：登录失败退避，按「主体 + 来源 IP」记账，不按账号。
//!
//! 原来的做法是把 `locked_until` 写在用户记录上，于是同一处代码同时长出了两个洞：
//!
//! 1. 触发锁定的那一次返回 `403 + "已锁定"`，直接对外宣告"这个账号存在"——
//!    前面所有等价耗时（dummy_hash）防护全被这一条消息作废；
//! 2. 锁是按账号的，任何人知道账号名，每 15 分钟发 5 个错误密码就能把人永久锁在门外，
//!    零成本、无需任何凭证。
//!
//! 这里改成：失败次数按 `(主体, 来源 IP)` 记账，退避时间按失败次数指数增长。
//! 攻击者只能消耗掉"自己这份配额"，锁不到别人；对同一个主体换 IP 也拿不到
//! "账号是否存在"的信号，因为不存在的账号同样会被记账、同样退避。
//!
//! 与 FIX-007 保持一致的口径：这是**进程内**状态，多实例部署时各自计数
//! （启动时已有告警），要全局一致请在网关层做。

use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

/// 桶上限：与 `security::ratelimit` 同一口径，防止任意账号名/IP 组合把内存撑爆。
const MAX_BUCKETS: usize = 20_000;
/// 前 4 次失败不放假（正常人打错密码不该被罚），第 5 次开始退避。
const FAILS_BEFORE_BACKOFF: u32 = 5;
/// 退避封顶 15 分钟：再长就等于永久拒绝服务，与"不锁死人"的初衷相悖。
const MAX_BACKOFF_SECS: u64 = 900;

#[derive(Default, Clone, Copy)]
struct Entry {
    fails: u32,
    until: Option<Instant>,
}

/// 主体可以是 `id:<user_id>`（账号已存在）或 `acct:<归一化输入>`（账号不存在）。
/// 用 id 而不是输入文本，是为了让「用户名 / 邮箱 / 大小写变体」共用同一份额度，
/// 否则同一个账号换写法就能把可尝试次数翻几倍。
pub struct LoginGuard {
    buckets: Mutex<HashMap<(String, String), Entry>>,
}

impl Default for LoginGuard {
    fn default() -> Self {
        Self::new()
    }
}

/// 退避时长：第 5 次失败 1s，之后 2s、4s…翻倍，封顶 15 分钟。
/// 对数增长意味着"慢速爆破"的成本随尝试次数急剧上升，而误按错键的正常用户
/// 最多等 1 秒就能重试。
fn backoff_for(fails: u32) -> Duration {
    if fails < FAILS_BEFORE_BACKOFF {
        return Duration::ZERO;
    }
    let exp = (fails - FAILS_BEFORE_BACKOFF).min(16);
    let secs = 1u64.checked_shl(exp).unwrap_or(MAX_BACKOFF_SECS);
    Duration::from_secs(secs.min(MAX_BACKOFF_SECS))
}

impl LoginGuard {
    pub fn new() -> Self {
        Self {
            buckets: Mutex::new(HashMap::new()),
        }
    }

    /// 把输入归一化成记账主体。空输入也照样记账（不能给"随便填个空"开免责通道）。
    pub fn subject_for(user_id: Option<&str>, account: &str) -> String {
        match user_id {
            Some(id) => format!("id:{id}"),
            None => format!("acct:{}", account.trim().to_lowercase()),
        }
    }

    fn key(subject: &str, ip: &str) -> (String, String) {
        (subject.trim().to_string(), ip.trim().to_string())
    }

    /// 还在退避期就返回剩余时长。调用方按"普通失败"处理即可，**不得**把它翻译成
    /// 任何"你被锁了"的文案——那会重新长回账号枚举。
    pub fn blocked_for(&self, subject: &str, ip: &str) -> Option<Duration> {
        let key = Self::key(subject, ip);
        let mut map = self.buckets.lock().ok()?;
        let entry = *map.get(&key)?;
        let until = entry.until?;
        let now = Instant::now();
        if until > now {
            return Some(until - now);
        }
        // 退避结束：清掉过期状态但保留失败次数（次数决定下一次更长的退避）。
        if let Some(e) = map.get_mut(&key) {
            e.until = None;
        }
        None
    }

    /// 记一次失败，返回本次施加的退避（`ZERO` 表示不惩罚）。
    pub fn record_failure(&self, subject: &str, ip: &str) -> Duration {
        let key = Self::key(subject, ip);
        let Ok(mut map) = self.buckets.lock() else {
            return Duration::ZERO;
        };
        if !map.contains_key(&key) && map.len() >= MAX_BUCKETS {
            // 先清已过期且未处于退避期的桶；仍满则放弃记账而不是无限扩容。
            let now = Instant::now();
            map.retain(|_, e| matches!(e.until, Some(t) if t > now));
            if map.len() >= MAX_BUCKETS {
                return Duration::ZERO;
            }
        }
        let entry = map.entry(key).or_default();
        entry.fails = entry.fails.saturating_add(1);
        let backoff = backoff_for(entry.fails);
        if !backoff.is_zero() {
            entry.until = Some(Instant::now() + backoff);
        }
        backoff
    }

    /// 登录成功：清掉这一份（主体+IP）的记录，别让之前打错的几次留下尾巴。
    pub fn record_success(&self, subject: &str, ip: &str) {
        if let Ok(mut map) = self.buckets.lock() {
            map.remove(&Self::key(subject, ip));
        }
    }

    /// 当前记账条目数（观测与测试用）。
    pub fn bucket_count(&self) -> usize {
        self.buckets.lock().map(|m| m.len()).unwrap_or(0)
    }
}

/// 进程内唯一实例。放全局而不是塞进 `AppState`：`AuthService` 在 API 层与测试里被
/// 反复按值构造，多一个字段就要改遍所有构造点，收益为零。
pub fn global() -> &'static LoginGuard {
    static GUARD: OnceLock<LoginGuard> = OnceLock::new();
    GUARD.get_or_init(LoginGuard::new)
}

/// 仅供测试：清空全局状态，避免单个测试进程里的记账互相污染。
#[cfg(test)]
pub fn reset_global_for_test() {
    if let Ok(mut map) = global().buckets.lock() {
        map.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backoff_grows_and_caps() {
        assert_eq!(backoff_for(1), Duration::ZERO, "前 4 次不该罚");
        assert_eq!(backoff_for(4), Duration::ZERO);
        assert_eq!(backoff_for(5), Duration::from_secs(1), "第 5 次从 1 秒起步");
        assert_eq!(backoff_for(6), Duration::from_secs(2));
        assert_eq!(backoff_for(7), Duration::from_secs(4));
        assert_eq!(
            backoff_for(200),
            Duration::from_secs(MAX_BACKOFF_SECS),
            "再多次也只能封顶 15 分钟，不能变成永久拒绝服务"
        );
    }

    #[test]
    fn failure_counter_is_per_subject_and_ip() {
        let g = LoginGuard::new();
        for _ in 0..5 {
            g.record_failure("id:u1", "1.1.1.1");
        }
        assert!(g.blocked_for("id:u1", "1.1.1.1").is_some(), "同一来源应被退避");
        assert!(
            g.blocked_for("id:u1", "2.2.2.2").is_none(),
            "换来源不该被牵连——锁的粒度是（主体+IP），不是账号"
        );
        g.record_success("id:u1", "1.1.1.1");
        assert!(g.blocked_for("id:u1", "1.1.1.1").is_none(), "成功后应清零");
    }

    #[test]
    fn unknown_account_is_tracked_the_same_way() {
        let g = LoginGuard::new();
        let subject = LoginGuard::subject_for(None, "NoSuchUser");
        for _ in 0..5 {
            g.record_failure(&subject, "9.9.9.9");
        }
        assert!(
            g.blocked_for(&subject, "9.9.9.9").is_some(),
            "不存在的账号也要照样退避，否则退避本身就成了存在性信号"
        );
    }

    #[test]
    fn username_variants_share_one_bucket_when_id_known() {
        let g = LoginGuard::new();
        let a = LoginGuard::subject_for(Some("u-1"), "alice");
        let b = LoginGuard::subject_for(Some("u-1"), "ALICE@example.com");
        assert_eq!(a, b, "同一用户的不同写法必须共用一份额度");
        assert_eq!(g.bucket_count(), 0);
    }
}
