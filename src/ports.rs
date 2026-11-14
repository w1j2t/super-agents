//! 可插拔部件注册表：一处汇总"这台机器现在装的是哪个部件、还能换什么"。
//!
//! # 为什么要这个模块
//!
//! 五个可插拔槽位（存储 / 执行 / 审计 / 限流 / 指标导出）分散在
//! `store`、`orchestration`、`obs`、`security` 四处，运维想知道"现在跑的到底是哪套"
//! 只能翻环境变量 + 猜代码默认值。这里把事实收拢成一个只读视图：
//!
//! 时钟曾经想做成第六个槽位，最终没做：全站时间只从 `chrono::Utc::now()` 取，
//! 没有任何地方需要注入假时钟，凭空多一层抽象只会让测试变慢。等真有"回放历史时刻"
//! 的诉求再加，届时按下方三步走即可。
//!
//! - **active** 取自**运行中的实例本身**（`kind()`），不是读配置猜的——
//!   配置和实例万一对不上，注册表说的一定是实话。
//! - **available** 列出该槽位现有实现，换部件只动一个环境变量。
//! - **restart_required** 目前恒为 `true`：部件在启动期装配，运行期不热插拔。
//!   这条如实写出来，避免让人误以为改环境变量会即时生效。

use crate::config::Config;
use crate::obs::audit::AuditSink;
use crate::obs::metrics::{MetricsExporter, MetricsKind};
use crate::orchestration::executor::StepRunner;
use crate::orchestration::runners::RunnerKind;
use crate::security::ratelimit::{Limiter, LimiterKind};
use crate::store::backends::StorageKind;
use crate::store::Store;
use serde::Serialize;

/// 一个可插拔槽位的当前状况。
#[derive(Debug, Serialize)]
pub struct ComponentInfo {
    /// 槽位名（唯一）
    pub slot: &'static str,
    /// 当前生效实现（取自实例，不猜）
    pub active: &'static str,
    /// 可用实现清单
    pub available: Vec<&'static str>,
    /// 控制它的环境变量
    pub env_var: &'static str,
    /// 端口契约（一句话说清这个部件必须提供什么）
    pub contract: &'static str,
    /// 换件是否必须重启进程
    pub restart_required: bool,
    /// 体检结论：`ok` = 正常工作；其它值都说明"能用但有前提"
    pub health: &'static str,
    /// 人话说明：这个部件当前的取舍
    pub notes: String,
}

/// 汇总全部槽位。
///
/// 参数刻意传"实例引用"而不是"配置"，保证 `active` 永远是运行中的真相。
///
/// `runner` 传的是**实例**（`None` 表示用内置 `SimRunner`），不是 `SA_RUNNER` 的字符串：
/// 早先这里读 `config.runner.as_str()`，结果是"配置说 sim、实际装的却是别的"时注册表也会
/// 跟着说假话。现在一律问实例要 `backend()`，配置怎么写的都不影响结论。
pub fn registry(
    cfg: &Config,
    store: &Store,
    runner: Option<&dyn StepRunner>,
    audit: &dyn AuditSink,
    metrics: &dyn MetricsExporter,
    limiter: &dyn Limiter,
) -> Vec<ComponentInfo> {
    let storage_kind = store.kind();
    let durable = storage_kind.is_durable();
    // 实例是唯一真相：没有装配外部执行器，就是内置模拟器。
    let runner_backend: &'static str = match runner {
        Some(r) => r.backend(),
        None => crate::orchestration::executor::SIM_RUNNER.backend(),
    };
    // 实现者若漏了在 `RunnerKind::ALL` 里登记，这里如实暴露，不假装成 sim。
    let runner_registered = RunnerKind::ALL.contains(&runner_backend);

    vec![
        ComponentInfo {
            slot: "storage",
            active: storage_kind.as_str(),
            available: StorageKind::ALL.to_vec(),
            env_var: "SA_STORAGE",
            contract: "Store::read / write / kind / stats —— 状态存取",
            restart_required: true,
            health: if durable { "ok" } else { "no_persistence" },
            notes: if durable {
                format!(
                    "落盘目录 {}，写后 {}ms 内必落盘",
                    cfg.data_dir.display(),
                    cfg.flush_interval_ms
                )
            } else {
                "纯内存：进程退出即丢全部数据，仅适合测试".to_string()
            },
        },
        ComponentInfo {
            slot: "runner",
            active: runner_backend,
            available: RunnerKind::ALL.to_vec(),
            env_var: "SA_RUNNER",
            contract: "StepRunner::backend / run_step —— 单步执行",
            restart_required: true,
            health: if !runner_registered {
                "unknown_backend"
            } else if runner_backend == "sim" {
                "simulated"
            } else {
                "ok"
            },
            notes: if !runner_registered {
                format!(
                    "该执行后端的 backend()=\"{runner_backend}\" 未登记进 RunnerKind::ALL，\
                     前端清单与文档会对不上；请补登记"
                )
            } else if runner_backend == "sim" {
                "确定性模拟器：产出固定结果，不做真实外部调用".to_string()
            } else {
                format!("真实执行，单步硬超时 {}s", cfg.runner_timeout_secs)
            },
        },
        ComponentInfo {
            slot: "audit",
            active: audit.kind(),
            available: crate::obs::audit::AuditKind::ALL.to_vec(),
            env_var: "SA_AUDIT",
            contract: "AuditSink::record / page / denied_count —— 审计流水",
            restart_required: true,
            // 与 storage 同一把尺子：不落盘就不是 ok。memory 后端重启即清空，
            // 事后追查拿不到任何流水，运维必须一眼看见这件事。
            health: if audit.durable() { "ok" } else { "no_persistence" },
            notes: if audit.durable() {
                format!(
                    "逐条追加落盘（环形容量 {} 条）；重启后从文件尾部读回",
                    cfg.audit_capacity
                )
            } else {
                format!(
                    "环形容量 {} 条，只存在内存：重启即清空，不适合合规追责",
                    cfg.audit_capacity
                )
            },
        },
        ComponentInfo {
            slot: "limiter",
            active: limiter.kind(),
            available: LimiterKind::ALL.to_vec(),
            env_var: "SA_LIMITER",
            contract: "Limiter::acquire —— 取令牌，返回放行/需等待/剩余配额",
            restart_required: true,
            health: if limiter.enforces() { "ok" } else { "not_enforcing" },
            notes: if limiter.enforces() {
                "令牌桶：按 key 独立计桶，超限回 429 + Retry-After".to_string()
            } else {
                "空实现：一律放行，对外不设防（仅压测/排障用）".to_string()
            },
        },
        ComponentInfo {
            slot: "metrics",
            active: metrics.kind(),
            available: MetricsKind::ALL.to_vec(),
            env_var: "SA_METRICS_EXPORT",
            contract: "MetricsExporter::inc/gauge_set/observe/render —— 指标记录与导出",
            restart_required: true,
            health: if metrics.kind() == "noop" {
                "not_recording"
            } else {
                "ok"
            },
            notes: if metrics.kind() == "noop" {
                "指标全部丢弃：/metrics 只剩运行时长".to_string()
            } else {
                format!("导出格式 {}", metrics.content_type())
            },
        },
    ]
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Config;
    use crate::obs::audit::AuditKind;
    use crate::obs::metrics::MetricsKind;
    use crate::security::ratelimit::LimiterKind;

    /// 每个测试用独立目录：JSON 后端会独占快照文件，共用路径会互相"拒绝访问"。
    fn cfg() -> Config {
        let dir = std::env::temp_dir().join(format!("sa-ports-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).expect("测试目录应可创建");
        Config::for_test(dir)
    }

    fn view(store_kind: StorageKind, limiter: LimiterKind, metrics: MetricsKind) -> Vec<ComponentInfo> {
        let c = cfg();
        let store = Store::with_kind(store_kind, c.data_dir.join("db.json"))
            .expect("测试存储应能打开");
        let audit = AuditKind::Memory
            .open(16, None)
            .expect("内存审计应能打开");
        let m = metrics.build();
        let l = limiter.build();
        // None = 内置 SimRunner，与生产默认装配一致
        registry(&c, &store, None, audit.as_ref(), m.as_ref(), l.as_ref())
    }

    /// 假执行器：只为验证"active 取自实例"。backend 名可任意指定。
    struct FakeRunner(&'static str);

    impl StepRunner for FakeRunner {
        fn backend(&self) -> &'static str {
            self.0
        }

        fn run_step(
            &self,
            _req: &crate::orchestration::executor::StepRequest<'_>,
        ) -> crate::orchestration::executor::StepOutcome {
            unreachable!("本测试只查注册表，从不执行步骤")
        }
    }

    /// 五个槽位一个都不能少，且 slot 名唯一。
    #[test]
    fn registry_covers_every_slot_once() {
        let reg = view(StorageKind::Memory, LimiterKind::Memory, MetricsKind::Prometheus);
        let mut slots: Vec<&str> = reg.iter().map(|c| c.slot).collect();
        assert_eq!(slots.len(), 5, "当前共 5 个可插拔槽位");
        slots.sort_unstable();
        slots.dedup();
        assert_eq!(slots.len(), 5, "slot 名必须唯一");
        for c in &reg {
            assert!(!c.available.is_empty(), "{} 必须列出可实现", c.slot);
            assert!(
                c.available.contains(&c.active),
                "{} 的 active={} 必须在 available 清单里",
                c.slot,
                c.active
            );
            assert!(c.restart_required, "当前所有部件都需重启生效，必须如实标注");
        }
    }

    /// active 必须来自实例本身：换成 noop 限流 + 内存存储，注册表要如实反映。
    #[test]
    fn active_reflects_live_instance_not_config() {
        let reg = view(StorageKind::Memory, LimiterKind::Noop, MetricsKind::Noop);
        let limiter = reg.iter().find(|c| c.slot == "limiter").unwrap();
        assert_eq!(limiter.active, "noop");
        assert_eq!(limiter.health, "not_enforcing");
        let metrics = reg.iter().find(|c| c.slot == "metrics").unwrap();
        assert_eq!(metrics.health, "not_recording");
        let storage = reg.iter().find(|c| c.slot == "storage").unwrap();
        assert_eq!(storage.health, "no_persistence");
        // 内存审计同样不得报 ok：不落盘的审计不是"健康"，是"能用但有前提"
        let audit = reg.iter().find(|c| c.slot == "audit").unwrap();
        assert_eq!(audit.active, "memory");
        assert_eq!(audit.health, "no_persistence");
        assert!(audit.notes.contains("重启即清空"), "notes 必须把后果说透");
    }

    /// 换了会落盘的审计后端，health 必须回到 ok —— 反向也要成立，不能一律报坏。
    #[test]
    fn durable_audit_reports_healthy() {
        let c = cfg();
        let store = Store::with_kind(StorageKind::Json, c.data_dir.join("db.json")).unwrap();
        let path = c.data_dir.join("audit.jsonl");
        let audit = AuditKind::File.open(16, Some(path)).expect("文件审计应能打开");
        let m = MetricsKind::Prometheus.build();
        let l = LimiterKind::Memory.build();
        // None = 内置 SimRunner，与生产默认装配一致（此处只验 audit 槽位）
        let reg = registry(&c, &store, None, audit.as_ref(), m.as_ref(), l.as_ref());
        let item = reg.iter().find(|x| x.slot == "audit").unwrap();
        assert_eq!(item.active, "file");
        assert_eq!(item.health, "ok");
        assert!(item.notes.contains("落盘"));
    }

    /// 健康值必须落在约定的枚举里，避免各写各的字符串。
    #[test]
    fn health_values_are_from_fixed_set() {
        let allowed = ["ok", "simulated", "no_persistence", "not_enforcing", "not_recording"];
        for kind in [StorageKind::Json, StorageKind::Memory] {
            for reg in [
                view(kind, LimiterKind::Memory, MetricsKind::Prometheus),
                view(kind, LimiterKind::Noop, MetricsKind::Noop),
            ] {
                for c in reg {
                    assert!(
                        allowed.contains(&c.health),
                        "{} 的 health={} 不在约定集合内",
                        c.slot,
                        c.health
                    );
                }
            }
        }
    }
}
