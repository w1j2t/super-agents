//! 阈值告警：命中判定 → 5 分钟去重 → 风暴聚合 → 打点。
//!
//! # 为什么把"去重"和"聚合"当成主功能，而不是顺手加的一行 if
//!
//! 没有去重的告警系统在真实事故里的表现是可预测的：第一条告警很有用，
//! 接下来 200 条把它淹没，值班的人开始无视整个通道——**告警越多，有效信息越少**。
//! 所以在 `observability_alerting` 这条能力里，"同一告警 5 分钟内只报一次"与
//! "风暴时只留严重级别"不是优化项，而是它能不能被信任的前提。
//!
//! 这也是本模块只有 `evaluate` 一个入口的原因：判定、去重、聚合、打点必须
//! **在同一个临界区里一次性完成**。若拆成"判定层 → 过滤层 → 上报层"三段，
//! 每一段都要重建"这条告警是不是刚报过"的判断，迟早出现两条路径口径不一致。
//!
//! # 自动处置边界（刻意不做的事）
//!
//! 能力声明里写的是"自动处置限于停止重试与降档，不做数据删除"。本项目里
//! 这两件事**已经各自有归属**：
//!
//! - 停止重试 = 执行器的熔断器（`sa_task_circuit_open_total` 可验证）
//! - 降档 = 预算超支时的降档（`sa_budget_downgrades_total` 可验证）
//!
//! 告警系统再去实现一遍，就会出现"两个地方都在决定要不要重试"的经典事故源。
//! 因此本模块只做两件事：**识别 + 通知**，并在 `auto_action` 字段里如实报出
//! "此刻是哪一个既有机制在兜底"。这是边界，不是缺口——文档 `docs/85` 有取舍论证。
//!
//! # 落点
//!
//! - 指标：`sa_alerts_total{severity,rule}` / `sa_alerts_deduped_total` /
//!   `sa_alerts_aggregated_total`（声明里要求的 `sa_http_5xx_total` 与
//!   `sa_task_failures_total` 分别以 5xx 标签维度与独立计数器真实存在）
//! - 接口：`GET /api/v1/observability/alerts`（抓取即评估，见 `handlers::observability`）

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::Serialize;

use super::metrics::{MetricsExporter, MetricsSnapshot};

// ---------------------------------------------------------------------------
// 严重级别
// ---------------------------------------------------------------------------

/// 告警级别。`Ord` 必须按"严重度递增"排列——风暴聚合要按它挑出必须单列的那些。
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Severity {
    Info,
    Warning,
    Critical,
}

impl Severity {
    pub fn as_str(&self) -> &'static str {
        match self {
            Severity::Info => "info",
            Severity::Warning => "warning",
            Severity::Critical => "critical",
        }
    }
}

// ---------------------------------------------------------------------------
// 规则
// ---------------------------------------------------------------------------

/// 一条阈值规则的静态定义。
///
/// 阈值与"最小样本量"都在 [`AlertPolicy`] 里，不写死在这里：同一份规则在
/// 开发机（请求数只有几十）与生产（请求数上万）需要完全不同的灵敏度和门槛，
/// 把数字写进常量等于让两种环境共用一个必然错位的判据。
pub struct AlertRule {
    pub id: &'static str,
    pub title: &'static str,
    pub severity: Severity,
    /// 读哪个指标（或哪两个指标的比值），人看着就知道这条规则在盯什么
    pub observed_from: &'static str,
}

/// 内置规则表。
///
/// 五条分别对应能力声明里点名的四类信号（错误率 / 失败率 / 限流 / 越权），
/// 外加熔断（`sa_task_circuit_open_total`）——它是"停止重试"已经发生的事实，
/// 值班时需要看到，而不是再去 `/metrics` 里翻。
pub const RULES: [AlertRule; 5] = [
    AlertRule {
        id: "http_5xx_ratio",
        title: "HTTP 5xx 占比过高",
        severity: Severity::Critical,
        observed_from: "sa_http_requests_total{status=5xx} / sa_http_requests_total",
    },
    AlertRule {
        id: "task_failure_ratio",
        title: "任务失败率过高",
        severity: Severity::Warning,
        observed_from: "sa_task_failures_total / sa_tasks_started_total",
    },
    AlertRule {
        id: "rate_limited_spike",
        title: "提交限流次数偏多",
        severity: Severity::Warning,
        observed_from: "sa_task_submit_rate_limited_total",
    },
    AlertRule {
        id: "authz_denied_spike",
        title: "越权拒绝次数偏多",
        severity: Severity::Critical,
        observed_from: "sa_authz_denied_total",
    },
    AlertRule {
        id: "circuit_open",
        title: "执行器已熔断（停止重试）",
        severity: Severity::Critical,
        observed_from: "sa_task_circuit_open_total",
    },
];

// ---------------------------------------------------------------------------
// 策略（阈值）
// ---------------------------------------------------------------------------

/// 告警策略：阈值、最小样本量与去重/聚合参数。
///
/// 全部可由环境变量覆盖，**这是门禁能做硬断言的前提**：门禁不靠"跑到真出错"
/// 来制造告警（那既慢又不稳定），而是把阈值降到必然命中，再去断言
/// "命中 → 去重 → 聚合"三段行为。参数不能覆盖的话，告警判据就只能靠祈祷。
#[derive(Debug, Clone)]
pub struct AlertPolicy {
    /// 同一规则的去重窗口（秒）。0 表示不去重（门禁用来对照验证"窗口真的在起作用"）
    pub dedup_secs: u64,
    /// 风暴阈值：单次评估中被去重后的非严重告警超过这个条数就折叠成一条
    pub storm_threshold: usize,
    /// 5xx 占比阈值（0.2 = 20%）
    pub http_5xx_ratio: f64,
    /// 5xx 规则的最小请求样本量（样本太少时比值没有意义）
    pub min_http_requests: u64,
    /// 任务失败率阈值
    pub task_failure_ratio: f64,
    /// 失败率规则的最小任务样本量
    pub min_tasks: u64,
    /// 限流命中的绝对次数阈值
    pub rate_limited_hits: u64,
    /// 越权拒绝的绝对次数阈值
    pub authz_denied_hits: u64,
    /// 熔断次数的绝对阈值
    pub circuit_open_hits: u64,
}

impl Default for AlertPolicy {
    fn default() -> Self {
        Self {
            dedup_secs: 300,
            storm_threshold: 5,
            http_5xx_ratio: 0.2,
            min_http_requests: 20,
            task_failure_ratio: 0.3,
            min_tasks: 10,
            rate_limited_hits: 5,
            authz_denied_hits: 5,
            circuit_open_hits: 1,
        }
    }
}

impl AlertPolicy {
    /// 从环境变量读策略。
    ///
    /// 非法值**不 panic**，而是回落到默认值并打一条 warn：告警配置写错
    /// 不该让整个服务起不来——这恰恰是"监控把业务拖死"的最常见形态。
    pub fn from_env() -> Self {
        let d = Self::default();
        let f = |key: &str, default: f64| -> f64 {
            match std::env::var(key) {
                Ok(v) => match v.trim().parse::<f64>() {
                    Ok(x) if x.is_finite() && x >= 0.0 => x,
                    _ => {
                        tracing::warn!(key, value = %v, "告警策略取值非法，回落到默认值");
                        default
                    }
                },
                Err(_) => default,
            }
        };
        let u = |key: &str, default: u64| -> u64 {
            match std::env::var(key) {
                Ok(v) => match v.trim().parse::<u64>() {
                    Ok(x) => x,
                    _ => {
                        tracing::warn!(key, value = %v, "告警策略取值非法，回落到默认值");
                        default
                    }
                },
                Err(_) => default,
            }
        };
        Self {
            dedup_secs: u("SA_ALERT_DEDUP_SECS", d.dedup_secs),
            storm_threshold: usize::try_from(u("SA_ALERT_STORM_THRESHOLD", d.storm_threshold as u64))
                .unwrap_or(d.storm_threshold),
            http_5xx_ratio: f("SA_ALERT_5XX_RATIO", d.http_5xx_ratio),
            min_http_requests: u("SA_ALERT_MIN_HTTP", d.min_http_requests),
            task_failure_ratio: f("SA_ALERT_TASK_FAILURE_RATIO", d.task_failure_ratio),
            min_tasks: u("SA_ALERT_MIN_TASKS", d.min_tasks),
            rate_limited_hits: u("SA_ALERT_RATE_LIMITED_HITS", d.rate_limited_hits),
            authz_denied_hits: u("SA_ALERT_AUTHZ_DENIED_HITS", d.authz_denied_hits),
            circuit_open_hits: u("SA_ALERT_CIRCUIT_OPEN_HITS", d.circuit_open_hits),
        }
    }
}

// ---------------------------------------------------------------------------
// 一条告警
// ---------------------------------------------------------------------------

/// 一条已发出的告警。
#[derive(Debug, Clone, Serialize)]
pub struct Alert {
    pub rule: &'static str,
    pub title: &'static str,
    pub severity: Severity,
    /// 触发时的观测值（比值类规则给比值，次数类规则给次数）
    pub observed: f64,
    pub threshold: f64,
    /// 人话解释"为什么报"，值班不用自己反推公式
    pub detail: String,
    /// 该规则自进程启动以来累计命中次数（含被去重压下的那些）
    pub hits: u64,
    /// 首次发出的时间（RFC3339，UTC）
    pub first_fired_at: String,
    /// 最近一次命中时间（RFC3339，UTC）——连续命中时它一直在走，便于判断"还在恶化吗"
    pub last_seen_at: String,
}

/// 一次评估的结果。
#[derive(Debug, Default)]
pub struct Evaluation {
    /// 本次新发出的告警（去重与聚合之后）
    pub alerts: Vec<Alert>,
    /// 被去重压下的规则 id（本次命中但窗口内已报过）
    pub deduped: Vec<&'static str>,
    /// 被风暴折叠掉的条数
    pub aggregated: u64,
}

/// 此刻是哪一个既有机制在承担"自动处置"。
///
/// 刻意只回答"谁在兜底"，不自己去执行处置——理由见模块文档。
#[derive(Debug, Clone, Serialize)]
pub struct AutoAction {
    /// `circuit_breaker` / `budget_downgrade` / `none`
    pub kind: &'static str,
    /// 人话说明
    pub detail: String,
}

// ---------------------------------------------------------------------------
// 告警器
// ---------------------------------------------------------------------------

struct Inner {
    /// 每条规则最近一次**发出**的时刻（去重窗口的锚点）
    last_fired: HashMap<&'static str, Instant>,
    /// 每条规则累计命中次数（不受去重影响）
    hits: HashMap<&'static str, u64>,
    /// 已发出、尚未过期的告警（按规则 id 存，同一规则只有最新一条）
    active: HashMap<&'static str, Alert>,
    /// 累计被去重压下的次数
    suppressed_total: u64,
    /// 累计被风暴折叠的条数
    aggregated_total: u64,
    /// 累计发出的告警条数
    fired_total: u64,
}

/// 阈值告警器。
///
/// 线程安全：所有可变状态在同一个 `Mutex` 里，`evaluate` 从头到尾持锁——
/// 判定与发布必须原子，否则两个并发抓取会各发一条同样的告警，去重就白做了。
pub struct Alerter {
    policy: AlertPolicy,
    inner: Mutex<Inner>,
}

/// 内部状态被毒化时的兜底：告警系统绝不能因为自己出错而让业务 500。
fn lock_or_recover(inner: &Mutex<Inner>) -> std::sync::MutexGuard<'_, Inner> {
    match inner.lock() {
        Ok(g) => g,
        Err(poisoned) => poisoned.into_inner(),
    }
}

impl Alerter {
    pub fn new(policy: AlertPolicy) -> Self {
        Self {
            policy,
            inner: Mutex::new(Inner {
                last_fired: HashMap::new(),
                hits: HashMap::new(),
                active: HashMap::new(),
                suppressed_total: 0,
                aggregated_total: 0,
                fired_total: 0,
            }),
        }
    }

    pub fn policy(&self) -> &AlertPolicy {
        &self.policy
    }

    /// 评估一次：读数 → 判阈值 → 去重 → 风暴聚合 → 打点。
    ///
    /// `now` 由调用方传入而不是内部取 `Instant::now()`：单测需要模拟
    /// "5 分钟过去了"，真取当前时刻的话，去重窗口就没法在毫秒级的测试里验证。
    pub fn evaluate(
        &self,
        snap: &MetricsSnapshot,
        metrics: &dyn MetricsExporter,
        now: Instant,
    ) -> Evaluation {
        let mut out = Evaluation::default();
        let mut guard = lock_or_recover(&self.inner);

        // ---- 第一段：逐条规则判阈值 ----
        // 只收集"命中"的原始结果，去重与聚合留到后面统一处理：
        // 先过滤再聚合，和先聚合再过滤，得到的条数完全不同。
        let mut hit: Vec<(&AlertRule, f64, f64, String)> = Vec::new();
        for rule in RULES.iter() {
            if let Some((observed, threshold, detail)) = self.check(rule, snap) {
                hit.push((rule, observed, threshold, detail));
            }
        }

        // ---- 第二段：去重 ----
        let mut fresh: Vec<(&AlertRule, f64, f64, String)> = Vec::new();
        for (rule, observed, threshold, detail) in hit {
            let hits = guard.hits.entry(rule.id).or_insert(0);
            *hits += 1;
            let hits_now = *hits;
            metrics.inc_by(
                "sa_alerts_total",
                &format!("severity=\"{}\",rule=\"{}\"", rule.severity.as_str(), rule.id),
                1,
            );
            // 不管报不报，命中本身就是事实，先写进 last_seen——值班看的是"还在恶化吗"
            let seen = chrono::Utc::now().to_rfc3339();
            if let Some(a) = guard.active.get_mut(rule.id) {
                a.last_seen_at = seen;
                a.observed = observed;
                a.hits = hits_now;
            }

            let within = guard.last_fired.get(rule.id).map(|t| {
                now.saturating_duration_since(*t) < Duration::from_secs(self.policy.dedup_secs)
            });
            if within == Some(true) {
                guard.suppressed_total += 1;
                metrics.inc("sa_alerts_deduped_total");
                out.deduped.push(rule.id);
                continue;
            }
            fresh.push((rule, observed, threshold, detail));
        }

        // ---- 第三段：风暴聚合 ----
        // 只折叠"非严重"：Critical 在任何情况下都必须逐条可见，
        // 把严重级别的告警合并掉，等于在最需要细节的时刻把细节丢了。
        let non_critical: Vec<usize> = fresh
            .iter()
            .enumerate()
            .filter(|(_, (r, _, _, _))| r.severity < Severity::Critical)
            .map(|(i, _)| i)
            .collect();
        let mut folded: Vec<&'static str> = Vec::new();
        if non_critical.len() > self.policy.storm_threshold {
            for i in non_critical.iter().rev() {
                let (rule, _, _, _) = fresh.remove(*i);
                folded.push(rule.id);
            }
            let n = folded.len() as u64;
            guard.aggregated_total += n;
            metrics.inc_by("sa_alerts_aggregated_total", "", n);
            out.aggregated = n;
        }

        // ---- 第四段：发布 ----
        for (rule, observed, threshold, detail) in fresh {
            guard.last_fired.insert(rule.id, now);
            guard.fired_total += 1;
            let fired_at = chrono::Utc::now().to_rfc3339();
            let alert = Alert {
                rule: rule.id,
                title: rule.title,
                severity: rule.severity,
                observed,
                threshold,
                detail,
                hits: *guard.hits.get(rule.id).unwrap_or(&1),
                first_fired_at: guard
                    .active
                    .get(rule.id)
                    .map(|a| a.first_fired_at.clone())
                    .unwrap_or_else(|| fired_at.clone()),
                last_seen_at: fired_at,
            };
            guard.active.insert(rule.id, alert.clone());
            out.alerts.push(alert);
        }

        // 被折叠的规则也要刷新 last_fired，否则下一轮它们会立刻全部"复发"，
        // 风暴期间变成 5 条/次 的持续抖动量。
        for id in folded {
            guard.last_fired.insert(id, now);
        }

        out
    }

    /// 单条规则的判定。返回 `(观测值, 阈值, 人话解释)`，未命中返回 `None`。
    fn check(&self, rule: &AlertRule, snap: &MetricsSnapshot) -> Option<(f64, f64, String)> {
        let p = &self.policy;
        match rule.id {
            "http_5xx_ratio" => {
                let total = snap.get("sa_http_requests_total");
                // 标签是 `status="500"` 这种三位码（见 `MetricsExporter::inc_http`），
                // 所以 `status="5` 正好圈住 5xx，不会误伤 4xx——不需要再减一次 4xx。
                let bad = snap.get_labelled("sa_http_requests_total", "status=\"5");
                if total < p.min_http_requests {
                    return None;
                }
                let ratio = bad as f64 / total as f64;
                if ratio < p.http_5xx_ratio {
                    return None;
                }
                Some((
                    ratio,
                    p.http_5xx_ratio,
                    format!(
                        "5xx {bad} 条 / 总请求 {total} 条 = {:.1}%，超过阈值 {:.1}%",
                        ratio * 100.0,
                        p.http_5xx_ratio * 100.0
                    ),
                ))
            }
            "task_failure_ratio" => {
                let started = snap.get("sa_tasks_started_total");
                let failed = snap.get("sa_task_failures_total");
                if started < p.min_tasks {
                    return None;
                }
                let ratio = failed as f64 / started as f64;
                if ratio < p.task_failure_ratio {
                    return None;
                }
                Some((
                    ratio,
                    p.task_failure_ratio,
                    format!(
                        "失败 {failed} 条 / 受理 {started} 条 = {:.1}%，超过阈值 {:.1}%",
                        ratio * 100.0,
                        p.task_failure_ratio * 100.0
                    ),
                ))
            }
            "rate_limited_spike" => {
                let n = snap.get("sa_task_submit_rate_limited_total");
                if n < p.rate_limited_hits {
                    return None;
                }
                Some((
                    n as f64,
                    p.rate_limited_hits as f64,
                    format!("提交限流累计 {n} 次，达到阈值 {}", p.rate_limited_hits),
                ))
            }
            "authz_denied_spike" => {
                let n = snap.get("sa_authz_denied_total");
                if n < p.authz_denied_hits {
                    return None;
                }
                Some((
                    n as f64,
                    p.authz_denied_hits as f64,
                    format!("越权拒绝累计 {n} 次，达到阈值 {}", p.authz_denied_hits),
                ))
            }
            "circuit_open" => {
                let n = snap.get("sa_task_circuit_open_total");
                if n < p.circuit_open_hits {
                    return None;
                }
                Some((
                    n as f64,
                    p.circuit_open_hits as f64,
                    format!("执行器熔断累计 {n} 次（已在停止重试），达到阈值 {}", p.circuit_open_hits),
                ))
            }
            _ => None,
        }
    }

    /// 已发出且未过期的告警（按严重度倒序、同级按规则 id 排序，输出稳定便于前端 diff）。
    pub fn active(&self) -> Vec<Alert> {
        let guard = lock_or_recover(&self.inner);
        self.active_locked(&guard, Instant::now())
    }

    /// 读锁后的取活跃逻辑。`active()` 与 `stats()` 共用它，避免两处口径分叉
    /// （一个把过期告警算进 `active_total`、另一个不算，前端会看到自相矛盾的数字）。
    fn active_locked(&self, guard: &Inner, now: Instant) -> Vec<Alert> {
        let window = Duration::from_secs(self.policy.dedup_secs.max(1));
        let mut v: Vec<Alert> = guard
            .active
            .values()
            .filter(|a| {
                // 超过一个去重窗口没再命中的，视为已恢复：继续挂在列表里会让人以为还没好
                guard
                    .last_fired
                    .get(a.rule)
                    .map(|t| now.saturating_duration_since(*t) < window)
                    .unwrap_or(false)
            })
            .cloned()
            .collect();
        v.sort_by(|a, b| b.severity.cmp(&a.severity).then(a.rule.cmp(b.rule)));
        v
    }

    /// 统计口径（`GET /alerts` 里的 `stats`）。
    pub fn stats(&self) -> AlertStats {
        let guard = lock_or_recover(&self.inner);
        AlertStats {
            fired_total: guard.fired_total,
            suppressed_total: guard.suppressed_total,
            aggregated_total: guard.aggregated_total,
            active_total: self.active_locked(&guard, Instant::now()).len() as u64,
            dedup_window_secs: self.policy.dedup_secs,
            storm_threshold: self.policy.storm_threshold as u64,
            rules_total: RULES.len() as u64,
        }
    }

    /// 当前兜底的自动处置（从指标里读既有事实，不自己动手）。
    pub fn auto_action(&self, snap: &MetricsSnapshot) -> AutoAction {
        if snap.get("sa_task_circuit_open_total") > 0 {
            return AutoAction {
                kind: "circuit_breaker",
                detail: "执行器已熔断：后续步骤不再重试（sa_task_circuit_open_total 可核对）".into(),
            };
        }
        if snap.get("sa_budget_downgrades_total") > 0 {
            return AutoAction {
                kind: "budget_downgrade",
                detail: "预算超支已触发降档（sa_budget_downgrades_total 可核对）".into(),
            };
        }
        AutoAction {
            kind: "none",
            detail: "无自动处置：告警只做通知与标记，处置权在值班人手里".into(),
        }
    }
}

/// 告警统计。
#[derive(Debug, Clone, Serialize)]
pub struct AlertStats {
    /// 累计发出的告警条数
    pub fired_total: u64,
    /// 累计被 5 分钟窗口压下的次数
    pub suppressed_total: u64,
    /// 累计被风暴折叠的条数
    pub aggregated_total: u64,
    /// 当前仍活跃（窗口内命中过）的规则数
    pub active_total: u64,
    pub dedup_window_secs: u64,
    pub storm_threshold: u64,
    pub rules_total: u64,
}

// ---------------------------------------------------------------------------
// 单测
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::obs::metrics::{Metrics, MetricsExporter};

    fn snap_with(pairs: &[(&str, &str, u64)]) -> MetricsSnapshot {
        let m = Metrics::new();
        for (name, labels, v) in pairs {
            m.inc_by(name, labels, *v);
        }
        m.snapshot()
    }

    fn policy(dedup_secs: u64) -> AlertPolicy {
        AlertPolicy {
            dedup_secs,
            storm_threshold: 5,
            // 阈值压到必然命中，单测不依赖"真的跑出错误"
            http_5xx_ratio: 0.1,
            min_http_requests: 1,
            task_failure_ratio: 0.1,
            min_tasks: 1,
            rate_limited_hits: 1,
            authz_denied_hits: 1,
            circuit_open_hits: 1,
        }
    }

    #[test]
    fn fires_when_threshold_hit() {
        let a = Alerter::new(policy(300));
        let m = Metrics::new();
        let snap = snap_with(&[("sa_task_submit_rate_limited_total", "", 3)]);
        let ev = a.evaluate(&snap, &m, Instant::now());
        assert!(ev.alerts.iter().any(|x| x.rule == "rate_limited_spike"));
        assert_eq!(a.stats().fired_total, 1);
        assert_eq!(m.snapshot().get("sa_alerts_total"), 1);
    }

    #[test]
    fn dedups_within_window() {
        let a = Alerter::new(policy(300));
        let m = Metrics::new();
        let snap = snap_with(&[("sa_task_submit_rate_limited_total", "", 3)]);
        let t0 = Instant::now();
        assert_eq!(a.evaluate(&snap, &m, t0).alerts.len(), 1);
        // 立刻再来一次：不该重复发，但命中计数要涨
        let ev = a.evaluate(&snap, &m, t0 + Duration::from_secs(1));
        assert!(ev.alerts.is_empty());
        assert_eq!(ev.deduped, vec!["rate_limited_spike"]);
        assert_eq!(a.stats().suppressed_total, 1);
        // 指标上必须能看到"压下过"，否则运维会以为告警坏了
        assert_eq!(m.snapshot().get("sa_alerts_deduped_total"), 1);
        assert_eq!(m.snapshot().get("sa_alerts_total"), 2);
    }

    #[test]
    fn refires_after_window_expires() {
        let a = Alerter::new(policy(300));
        let m = Metrics::new();
        let snap = snap_with(&[("sa_task_submit_rate_limited_total", "", 3)]);
        let t0 = Instant::now();
        assert_eq!(a.evaluate(&snap, &m, t0).alerts.len(), 1);
        // 过了窗口：必须重新发，"压下去"不能变成"永远压着"
        let ev = a.evaluate(&snap, &m, t0 + Duration::from_secs(301));
        assert_eq!(ev.alerts.len(), 1);
        assert_eq!(a.stats().suppressed_total, 0);
    }

    #[test]
    fn zero_dedup_window_never_suppresses() {
        let a = Alerter::new(policy(0));
        let m = Metrics::new();
        let snap = snap_with(&[("sa_task_submit_rate_limited_total", "", 3)]);
        let t0 = Instant::now();
        assert_eq!(a.evaluate(&snap, &m, t0).alerts.len(), 1);
        assert_eq!(a.evaluate(&snap, &m, t0).alerts.len(), 1);
        assert_eq!(a.stats().suppressed_total, 0);
    }

    #[test]
    fn storm_folds_non_critical_but_keeps_all_critical() {
        // 三条非严重 + 两条严重，阈值 1 → 非严重应被折成一条也不留，严重全留
        let mut p = policy(0);
        p.storm_threshold = 1;
        let a = Alerter::new(p);
        let m = Metrics::new();
        let snap = snap_with(&[
            ("sa_task_submit_rate_limited_total", "", 5),
            ("sa_task_failures_total", "", 5),
            ("sa_tasks_started_total", "", 5),
            ("sa_authz_denied_total", "", 5),
            ("sa_task_circuit_open_total", "", 1),
        ]);
        let ev = a.evaluate(&snap, &m, Instant::now());
        // 非严重两条（限流 + 失败率）被折叠
        assert_eq!(ev.aggregated, 2);
        // 严重两条必须逐条可见
        assert_eq!(ev.alerts.len(), 2);
        assert!(ev.alerts.iter().all(|x| x.severity == Severity::Critical));
        assert_eq!(m.snapshot().get("sa_alerts_aggregated_total"), 2);
    }

    #[test]
    fn below_threshold_stays_quiet() {
        let a = Alerter::new(AlertPolicy::default());
        let m = Metrics::new();
        // 只有 3 个请求、0 个 5xx：样本量不足，任何规则都不该报
        let snap = snap_with(&[("sa_http_requests_total", "status=\"200\"", 3)]);
        assert!(a.evaluate(&snap, &m, Instant::now()).alerts.is_empty());
        assert_eq!(a.stats().fired_total, 0);
    }

    #[test]
    fn auto_action_reports_existing_mechanism() {
        let a = Alerter::new(AlertPolicy::default());
        let idle = snap_with(&[("sa_http_requests_total", "status=\"200\"", 1)]);
        assert_eq!(a.auto_action(&idle).kind, "none");
        let tripped = snap_with(&[("sa_task_circuit_open_total", "", 1)]);
        assert_eq!(a.auto_action(&tripped).kind, "circuit_breaker");
        let budget = snap_with(&[("sa_budget_downgrades_total", "", 2)]);
        assert_eq!(a.auto_action(&budget).kind, "budget_downgrade");
    }
}
