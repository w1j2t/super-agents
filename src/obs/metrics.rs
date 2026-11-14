//! 全链路可观测：计数器 / 仪表 / 直方图摘要，可切换导出格式。
//!
//! # 为什么有 `MetricsExporter` 这一层
//!
//! 早前 handler 直接持有具体类型 `Arc<Metrics>`，"指标怎么输出"和"指标怎么记录"
//! 焊死在一起：想换成 JSON（调试）或直接关掉（压测基线）都要改一串文件。
//! 现在拆成两件事：
//!
//! - **记录**：`Metrics` 负责攒数（计数器 / 仪表 / 直方图摘要）
//! - **导出**：`MetricsExporter` 决定对外长什么样
//!   - `prometheus`：Prometheus 文本（生产默认）
//!   - `json`：JSON 快照（调试、给前端做实时看板）
//!   - `noop`：全丢弃（压测基线、排查"是不是指标拖慢了"）
//!
//! 换导出器只改 `SA_METRICS_EXPORT`，`AppState` 里的类型是 `Arc<dyn MetricsExporter>`，
//! 任何 handler 都不需要认识具体实现，也就不会因为换部件被牵连。

use std::collections::{BTreeMap, HashMap};
use std::sync::Mutex;
use std::time::Instant;

#[derive(Default, Clone)]
struct Hist {
    count: u64,
    sum: f64,
    min: f64,
    max: f64,
}

// ---------------------------------------------------------------------------
// 端口：所有导出器都必须满足的契约
// ---------------------------------------------------------------------------

/// 指标导出端口。
///
/// 记录类方法语义固定（计数器只增、仪表覆盖、直方图攒摘要），
/// 导出类方法（`content_type` / `render`）由实现决定格式。
pub trait MetricsExporter: Send + Sync {
    fn inc(&self, name: &str);
    fn inc_by(&self, name: &str, labels: &str, by: u64);
    fn gauge_set(&self, name: &str, labels: &str, value: f64);
    fn observe(&self, name: &str, labels: &str, value: f64);
    /// 把计数器**对齐成绝对值**，而不是累加。
    ///
    /// 为什么需要它：有些数字的真值不在导出器里，而藏在别处的原子变量中
    /// （例如 `EventBus` 自己统计的"被丢弃的广播事件"）。抓取时要把真值同步进来；
    /// 若用 `inc_by` 累加，同一段差值会被反复计入，数字只会虚高。
    fn counter_set(&self, name: &str, labels: &str, value: u64);
    fn uptime_secs(&self) -> u64;
    fn snapshot(&self) -> MetricsSnapshot;
    /// 导出格式的 MIME（`/metrics` 直接拿它当 Content-Type）。
    fn content_type(&self) -> &'static str;
    /// 导出正文。
    fn render(&self) -> String;
    /// 部件名（注册表与概览页展示用）。
    fn kind(&self) -> &'static str;

    /// 便捷方法：所有实现共享同一套标签口径，避免各处手写格式漂移。
    fn inc_http(&self, route: &str, method: &str, status: u16) {
        let labels = format!("route=\"{route}\",method=\"{method}\",status=\"{status}\"");
        self.inc_by("sa_http_requests_total", &labels, 1);
    }
}

// ---------------------------------------------------------------------------
// 实现一：内存攒数 + Prometheus 文本导出（生产默认）
// ---------------------------------------------------------------------------

pub struct Metrics {
    started: Instant,
    counters: Mutex<HashMap<(String, String), u64>>,
    gauges: Mutex<HashMap<(String, String), f64>>,
    hist: Mutex<HashMap<(String, String), Hist>>,
}

impl Default for Metrics {
    fn default() -> Self {
        Self::new()
    }
}

impl Metrics {
    pub fn new() -> Self {
        Self {
            started: Instant::now(),
            counters: Mutex::new(HashMap::new()),
            gauges: Mutex::new(HashMap::new()),
            hist: Mutex::new(HashMap::new()),
        }
    }
}

impl MetricsExporter for Metrics {
    fn inc(&self, name: &str) {
        self.inc_by(name, "", 1);
    }

    fn inc_by(&self, name: &str, labels: &str, by: u64) {
        let mut map = self.counters.lock().expect("metrics 锁未被毒化");
        *map.entry((name.to_string(), labels.to_string())).or_insert(0) += by;
    }

    fn gauge_set(&self, name: &str, labels: &str, value: f64) {
        let mut map = self.gauges.lock().expect("metrics 锁未被毒化");
        map.insert((name.to_string(), labels.to_string()), value);
    }

    fn counter_set(&self, name: &str, labels: &str, value: u64) {
        let mut map = self.counters.lock().expect("metrics 锁未被毒化");
        map.insert((name.to_string(), labels.to_string()), value);
    }

    fn observe(&self, name: &str, labels: &str, value: f64) {
        let mut map = self.hist.lock().expect("metrics 锁未被毒化");
        let h = map
            .entry((name.to_string(), labels.to_string()))
            .or_insert(Hist {
                count: 0,
                sum: 0.0,
                min: value,
                max: value,
            });
        h.count += 1;
        h.sum += value;
        h.min = h.min.min(value);
        h.max = h.max.max(value);
    }

    fn uptime_secs(&self) -> u64 {
        self.started.elapsed().as_secs()
    }

    fn snapshot(&self) -> MetricsSnapshot {
        MetricsSnapshot {
            uptime_secs: self.uptime_secs(),
            counters: self
                .counters
                .lock()
                .map(|m| m.clone().into_iter().collect())
                .unwrap_or_default(),
        }
    }

    fn content_type(&self) -> &'static str {
        "text/plain; version=0.0.4; charset=utf-8"
    }

    fn kind(&self) -> &'static str {
        "prometheus"
    }

    /// Prometheus 文本格式导出。
    ///
    /// 关键约束：**同一个 metric name 的 `# TYPE` 只能出现一次**。
    /// 早前实现是"每个 label 组合各打一次 TYPE"，同一个计数器带 3 种 route 标签
    /// 就会输出 3 行重复 TYPE，`promtool check metrics` 会直接判为非法。
    /// 这里先按 name 归组，TYPE/HELP 打一次，再逐条打样本。
    fn render(&self) -> String {
        let mut out = String::new();
        out.push_str("# HELP sa_uptime_seconds 服务运行时长（秒）\n");
        out.push_str("# TYPE sa_uptime_seconds gauge\n");
        out.push_str(&format!("sa_uptime_seconds {}\n", self.uptime_secs()));

        // counter：按 name 归组
        let counters = group_by_name(self.counters.lock().map(|m| m.clone()).unwrap_or_default());
        for (name, samples) in counters {
            out.push_str(&format!("# TYPE {name} counter\n"));
            for (labels, value) in samples {
                out.push_str(&sample_line(&name, &labels, value.to_string().as_str()));
            }
        }

        // gauge：按 name 归组
        let gauges = group_by_name(self.gauges.lock().map(|m| m.clone()).unwrap_or_default());
        for (name, samples) in gauges {
            out.push_str(&format!("# TYPE {name} gauge\n"));
            for (labels, value) in samples {
                out.push_str(&sample_line(&name, &labels, value.to_string().as_str()));
            }
        }

        // 直方图摘要：一个 name 展开成 count/sum/min/max 四个 family，各打一次 TYPE
        let hist = self.hist.lock().map(|m| m.clone()).unwrap_or_default();
        let mut grouped: BTreeMap<String, Vec<(String, Hist)>> = BTreeMap::new();
        for ((name, labels), h) in hist {
            grouped.entry(name).or_default().push((labels, h));
        }
        for (name, mut samples) in grouped {
            samples.sort_by(|a, b| a.0.cmp(&b.0));
            out.push_str(&format!("# TYPE {name}_count counter\n"));
            out.push_str(&format!("# TYPE {name}_sum counter\n"));
            out.push_str(&format!("# TYPE {name}_min gauge\n"));
            out.push_str(&format!("# TYPE {name}_max gauge\n"));
            for (labels, h) in samples {
                out.push_str(&sample_line(&format!("{name}_count"), &labels, &h.count.to_string()));
                out.push_str(&sample_line(&format!("{name}_sum"), &labels, &h.sum.to_string()));
                out.push_str(&sample_line(&format!("{name}_min"), &labels, &h.min.to_string()));
                out.push_str(&sample_line(&format!("{name}_max"), &labels, &h.max.to_string()));
            }
        }
        out
    }
}

/// 一行样本：有标签就带 `{...}`，没有就裸名。
fn sample_line(name: &str, labels: &str, value: &str) -> String {
    if labels.is_empty() {
        format!("{name} {value}\n")
    } else {
        format!("{name}{{{labels}}} {value}\n")
    }
}

/// 把 `(name, labels) -> value` 展开成 `name -> [(labels, value)]`，name 与 labels 都排序，
/// 保证输出稳定（同一份数据两次抓取顺序一致，diff 才看得懂）。
fn group_by_name<V: Copy>(
    map: HashMap<(String, String), V>,
) -> BTreeMap<String, Vec<(String, V)>> {
    let mut out: BTreeMap<String, Vec<(String, V)>> = BTreeMap::new();
    for ((name, labels), value) in map {
        out.entry(name).or_default().push((labels, value));
    }
    for v in out.values_mut() {
        v.sort_by(|a, b| a.0.cmp(&b.0));
    }
    out
}

// ---------------------------------------------------------------------------
// 实现二：JSON 快照导出（调试 / 前端实时看板）
// ---------------------------------------------------------------------------

/// 把同一份内存计数导出成 JSON。
///
/// 用途：前端做"实时指标面板"时直接 `JSON.parse`，不必自己解析 Prometheus 文本；
/// 也方便手工 `curl | jq` 排查某个计数器有没有涨。
pub struct JsonExporter {
    inner: Metrics,
}

impl Default for JsonExporter {
    fn default() -> Self {
        Self::new()
    }
}

impl JsonExporter {
    pub fn new() -> Self {
        Self {
            inner: Metrics::new(),
        }
    }
}

impl MetricsExporter for JsonExporter {
    fn inc(&self, name: &str) {
        self.inner.inc(name);
    }
    fn inc_by(&self, name: &str, labels: &str, by: u64) {
        self.inner.inc_by(name, labels, by);
    }
    fn gauge_set(&self, name: &str, labels: &str, value: f64) {
        self.inner.gauge_set(name, labels, value);
    }
    fn counter_set(&self, name: &str, labels: &str, value: u64) {
        self.inner.counter_set(name, labels, value);
    }
    fn observe(&self, name: &str, labels: &str, value: f64) {
        self.inner.observe(name, labels, value);
    }
    fn uptime_secs(&self) -> u64 {
        self.inner.uptime_secs()
    }
    fn snapshot(&self) -> MetricsSnapshot {
        self.inner.snapshot()
    }
    fn content_type(&self) -> &'static str {
        "application/json; charset=utf-8"
    }
    fn kind(&self) -> &'static str {
        "json"
    }
    fn render(&self) -> String {
        let snap = self.inner.snapshot();
        let body = serde_json::json!({
            "uptime_secs": snap.uptime_secs,
            "counters": snap
                .counters
                .iter()
                .map(|((name, labels), v)| serde_json::json!({
                    "name": name, "labels": labels, "value": v,
                }))
                .collect::<Vec<_>>(),
        });
        serde_json::to_string_pretty(&body).unwrap_or_else(|_| "{}".to_string())
    }
}

// ---------------------------------------------------------------------------
// 实现三：空导出器（压测基线 / 关闭指标）
// ---------------------------------------------------------------------------

/// 丢弃一切记录。
///
/// 用途：① 压测时排除指标锁竞争对结果的影响；② 明确"这台机器不留指标"。
/// 注意它会连 `sa_agents_total` 这类启动期仪表一起吞掉，`/metrics` 只返回空文本——
/// 这是刻意行为，注册表里会把 `records=false` 标出来，不会让人误以为"指标坏了"。
pub struct NoopExporter {
    started: Instant,
}

impl Default for NoopExporter {
    fn default() -> Self {
        Self::new()
    }
}

impl NoopExporter {
    pub fn new() -> Self {
        Self {
            started: Instant::now(),
        }
    }
}

impl MetricsExporter for NoopExporter {
    fn inc(&self, _name: &str) {}
    fn inc_by(&self, _name: &str, _labels: &str, _by: u64) {}
    fn gauge_set(&self, _name: &str, _labels: &str, _value: f64) {}
    fn counter_set(&self, _name: &str, _labels: &str, _value: u64) {}
    fn observe(&self, _name: &str, _labels: &str, _value: f64) {}
    fn uptime_secs(&self) -> u64 {
        self.started.elapsed().as_secs()
    }
    fn snapshot(&self) -> MetricsSnapshot {
        MetricsSnapshot {
            uptime_secs: self.uptime_secs(),
            counters: Vec::new(),
        }
    }
    fn content_type(&self) -> &'static str {
        "text/plain; version=0.0.4; charset=utf-8"
    }
    fn kind(&self) -> &'static str {
        "noop"
    }
    fn render(&self) -> String {
        format!(
            "# 指标导出已关闭（SA_METRICS_EXPORT=noop）\nsa_uptime_seconds {}\n",
            self.uptime_secs()
        )
    }
}

// ---------------------------------------------------------------------------
// 快照结构
// ---------------------------------------------------------------------------

pub struct MetricsSnapshot {
    pub uptime_secs: u64,
    pub counters: Vec<((String, String), u64)>,
}

impl MetricsSnapshot {
    pub fn get(&self, name: &str) -> u64 {
        self.counters
            .iter()
            .filter(|((n, _), _)| n == name)
            .map(|(_, v)| *v)
            .sum()
    }

    /// 按标签过滤取值：`get_labelled("sa_http_requests_total", "status=\"429\"")`。
    pub fn get_labelled(&self, name: &str, label_substr: &str) -> u64 {
        self.counters
            .iter()
            .filter(|((n, l), _)| n == name && l.contains(label_substr))
            .map(|(_, v)| *v)
            .sum()
    }
}

// ---------------------------------------------------------------------------
// 部件选择
// ---------------------------------------------------------------------------

/// 指标导出部件（`SA_METRICS_EXPORT`）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MetricsKind {
    Prometheus,
    Json,
    Noop,
}

impl MetricsKind {
    pub const ALL: [&'static str; 3] = ["prometheus", "json", "noop"];

    pub fn as_str(&self) -> &'static str {
        match self {
            MetricsKind::Prometheus => "prometheus",
            MetricsKind::Json => "json",
            MetricsKind::Noop => "noop",
        }
    }

    pub fn parse(s: &str) -> Option<Self> {
        match s.trim().to_ascii_lowercase().as_str() {
            "prometheus" | "prom" => Some(MetricsKind::Prometheus),
            "json" => Some(MetricsKind::Json),
            "noop" | "off" | "none" => Some(MetricsKind::Noop),
            _ => None,
        }
    }

    /// 构造导出器：一处决定"装哪个部件"，其它地方只认 `MetricsExporter`。
    pub fn build(self) -> std::sync::Arc<dyn MetricsExporter> {
        match self {
            MetricsKind::Prometheus => std::sync::Arc::new(Metrics::new()),
            MetricsKind::Json => std::sync::Arc::new(JsonExporter::new()),
            MetricsKind::Noop => std::sync::Arc::new(NoopExporter::new()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn renders_prometheus_text() {
        let m = Metrics::new();
        m.inc("sa_registrations_total");
        m.inc_http("/api/v1/auth/login", "POST", 200);
        m.observe("sa_task_duration_ms", "", 12.0);
        let text = m.render();
        assert!(text.contains("sa_uptime_seconds"));
        assert!(text.contains("sa_registrations_total 1"));
        assert!(text.contains("route=\"/api/v1/auth/login\""));
        assert!(text.contains("sa_task_duration_ms_count 1"));
    }

    /// 回归：同一计数器带多种标签时，`# TYPE` 只能出现一次。
    /// 这是 `promtool check metrics` 的硬要求，早前实现每个标签组合各打一次，会被判非法。
    #[test]
    fn type_line_emitted_once_per_metric_name() {
        let m = Metrics::new();
        m.inc_http("/a", "GET", 200);
        m.inc_http("/b", "POST", 404);
        m.inc_http("/c", "DELETE", 500);
        let text = m.render();
        let type_lines = text
            .lines()
            .filter(|l| l.starts_with("# TYPE sa_http_requests_total "))
            .count();
        assert_eq!(type_lines, 1, "同名指标的 TYPE 行必须只有一行");
        let samples = text
            .lines()
            .filter(|l| l.starts_with("sa_http_requests_total{"))
            .count();
        assert_eq!(samples, 3, "三条样本都要在");
    }

    /// 直方图摘要的四个 family 同样各只打一次 TYPE。
    #[test]
    fn histogram_type_lines_are_unique() {
        let m = Metrics::new();
        m.observe("sa_task_duration_ms", "route=\"/a\"", 1.0);
        m.observe("sa_task_duration_ms", "route=\"/b\"", 3.0);
        let text = m.render();
        for suffix in ["_count", "_sum", "_min", "_max"] {
            let n = text
                .lines()
                .filter(|l| l.starts_with(&format!("# TYPE sa_task_duration_ms{suffix} ")))
                .count();
            assert_eq!(n, 1, "sa_task_duration_ms{suffix} 的 TYPE 行必须唯一");
        }
    }

    #[test]
    fn json_exporter_produces_parsable_json() {
        let e = JsonExporter::new();
        e.inc("sa_x_total");
        let text = e.render();
        let v: serde_json::Value = serde_json::from_str(&text).expect("必须是合法 JSON");
        assert_eq!(v["counters"][0]["name"], "sa_x_total");
        assert!(e.content_type().starts_with("application/json"));
    }

    /// 换导出器后调用方代码完全不变：这正是"换部件不殃及全部"的可验证形式。
    #[test]
    fn exporters_are_interchangeable() {
        let exporters: Vec<Box<dyn MetricsExporter>> = vec![
            Box::new(Metrics::new()),
            Box::new(JsonExporter::new()),
            Box::new(NoopExporter::new()),
        ];
        for e in exporters {
            e.inc("sa_probe_total");
            e.inc_http("/api/v1/tasks", "POST", 201);
            e.gauge_set("sa_g", "", 1.0);
            e.observe("sa_h", "", 2.0);
            let _ = e.render();
            let _ = e.snapshot();
            assert!(!e.kind().is_empty());
            assert!(!e.content_type().is_empty());
        }
    }

    #[test]
    fn noop_reports_uptime_but_no_counters() {
        let e = NoopExporter::new();
        e.inc("sa_x_total");
        assert_eq!(e.snapshot().get("sa_x_total"), 0);
        assert!(e.render().contains("指标导出已关闭"));
    }

    #[test]
    fn kind_parsing_accepts_aliases_and_rejects_garbage() {
        assert_eq!(MetricsKind::parse("prom"), Some(MetricsKind::Prometheus));
        assert_eq!(MetricsKind::parse(" JSON "), Some(MetricsKind::Json));
        assert_eq!(MetricsKind::parse("off"), Some(MetricsKind::Noop));
        assert_eq!(MetricsKind::parse("influxdb"), None);
    }

    #[test]
    fn counter_set_aligns_absolute_value_instead_of_accumulating() {
        // 真值在别处（如 EventBus 的原子计数）时，抓取必须"对齐"而不是"累加"：
        // 连续抓两次同样的真值，数字应当停在原处，不该因为抓得多就涨。
        let m = Metrics::new();
        m.counter_set("sa_sse_events_dropped_total", "", 7);
        m.counter_set("sa_sse_events_dropped_total", "", 7);
        assert_eq!(m.snapshot().counters.len(), 1);
        let v = m
            .snapshot()
            .counters
            .into_iter()
            .find(|((name, _), _)| name == "sa_sse_events_dropped_total")
            .map(|(_, v)| v)
            .expect("计数器应存在");
        assert_eq!(v, 7, "重复对齐同一真值不得累加");

        m.counter_set("sa_sse_events_dropped_total", "", 11);
        let v2 = m
            .snapshot()
            .counters
            .into_iter()
            .find(|((name, _), _)| name == "sa_sse_events_dropped_total")
            .map(|(_, v)| v)
            .unwrap();
        assert_eq!(v2, 11);

        // 导出文本里也应带上它，且 TYPE 只出现一次
        let text = m.render();
        assert_eq!(text.matches("# TYPE sa_sse_events_dropped_total counter").count(), 1);
        assert!(text.contains("sa_sse_events_dropped_total 11"));
    }
}
