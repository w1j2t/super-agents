//! 应用状态：所有子系统在此汇聚，handler 只依赖这一处。
//!
//! # 装配边界（模块化的关键一环）
//!
//! 这个文件是**唯一**知道"某个槽位当前装的是哪个具体实现"的地方：
//! `metrics` 与 `limiter` 在这里以 `Arc<dyn 端口>` 的形态出现，
//! handler / service / executor 拿到的全是 trait 对象，换部件不需要改它们。
//!
//! 加一个新槽位的标准动作只有三步：
//! 1. 在领域模块里定 trait + 若干实现 + `XxxKind::build()`；
//! 2. 在这里加一个字段并赋值；
//! 3. 在 `ports::registry()` 里登记一行（让人能从 `/api/v1/observability/components` 看到）。

use crate::config::Config;
use crate::obs::alert::Alerter;
use crate::obs::audit::AuditSink;
use crate::obs::metrics::MetricsExporter;
use crate::orchestration::cancel::Cancellations;
use crate::orchestration::events::EventBus;
use crate::orchestration::executor::StepRunner;
use crate::ports::ComponentInfo;
use crate::security::ratelimit::Limiter;
use crate::services::auth::AuthService;
use crate::services::cluster_registry::ClusterRegistry;
use crate::store::Store;
use std::sync::Arc;

pub struct AppState {
    pub config: Arc<Config>,
    pub store: Arc<Store>,
    /// 指标记录 + 导出（可插拔部件，`SA_METRICS_EXPORT`）
    pub metrics: Arc<dyn MetricsExporter>,
    pub audit: Arc<dyn AuditSink>,
    /// 限流后端（可插拔部件，`SA_LIMITER`）
    pub limiter: Arc<dyn Limiter>,
    /// 执行后端（可插拔部件）：`None` = 内置确定性模拟器
    pub runner: Option<Arc<dyn StepRunner>>,
    /// 异步任务的取消登记簿：让 `cancelled` 成为真能到达的状态。
    /// 它不是可替换部件（没有第二套实现的意义），所以不进 `ports` 注册表。
    pub cancellations: Arc<Cancellations>,
    /// 任务状态事件总线（SSE 进度的数据源）。
    /// 与 `cancellations` 同理：它是进程内广播，不是"可换实现"的槽位。
    pub events: Arc<EventBus>,
    /// 集群心跳登记表：把本节点写进共享目录，并把别人的心跳读回来。
    ///
    /// 它**不跑后台循环**——循环挂在 `main` 里，因为"每 5 秒写一次"属于进程生命周期，
    /// 不是状态装配的一部分。放在这里只为了"取数口径唯一"：
    /// `/cluster` 与后台心跳都通过 [`crate::services::cluster_view`] 采集，两处必然一致。
    pub cluster: Arc<ClusterRegistry>,
    /// 进程启动时刻（UTC）：集群视图要报"这个节点从几点开始扛活"。
    ///
    /// 为什么不复用 `metrics.uptime_secs()`：那是**时长**，回答不了"从几点起"；
    /// 而 `Instant` 转不成墙上时间，只能在这里存一个绝对时刻。
    /// 两者并存不是冗余——时长算得快，时刻说得清。
    pub started_at: chrono::DateTime<chrono::Utc>,
    /// 阈值告警器：持有"同一告警 5 分钟内只报一次"的去重状态。
    ///
    /// 它必须是**进程级单一实例**，不能每个请求 new 一个：去重窗口活在实例里，
    /// 每次 new 都等于窗口重置，于是每次抓取都发一遍告警——正是要防的那个行为。
    pub alerts: Arc<Alerter>,
}

pub type SharedState = Arc<AppState>;

impl AppState {
    /// 构造状态；任一部件初始化失败都会返回错误（拒绝带病启动）。
    pub fn new(config: Config, store: Store) -> Result<Self, String> {
        let runner = crate::orchestration::runners::build(&config)?;
        let audit = config
            .audit
            .open(config.audit_capacity, config.audit_file.clone().or_else(|| {
                (config.audit == crate::obs::audit::AuditKind::File)
                    .then(|| config.data_dir.join("audit.jsonl"))
            }))?;
        let metrics = config.metrics_export.build();
        // 启动期就存在的静态事实，用仪表暴露出来（部署清单核对时很有用）
        metrics.gauge_set("sa_agents_total", "", crate::domain::agent::AGENTS.len() as f64);
        metrics.gauge_set(
            "sa_capabilities_total",
            "",
            crate::domain::capability::CAPABILITIES.len() as f64,
        );
        let limiter = config.limiter.build();
        // 集群登记表必须在 `config` 被 move 进 `Arc` 之前装配：它要读
        // `cluster_dir` / `node_id` / `cluster_ttl_secs` 三个字段。
        // 关闭（`SA_CLUSTER=off`）时拿到的是空操作实例，所以这里不需要 if——
        // 把判断留在这里，就等于在五个调用点各留一颗定时炸弹。
        let cluster = Arc::new(ClusterRegistry::from_config(&config));
        // 告警策略在构造时就定下来：运行中改环境变量不会生效，
        // 这是刻意的——阈值中途变化会让"这条到底报没报过"失去可比性。
        let alerts = Arc::new(Alerter::new(config.alert.clone()));
        Ok(Self {
            config: Arc::new(config),
            store: Arc::new(store),
            metrics,
            audit,
            limiter,
            runner,
            cancellations: Arc::new(Cancellations::new()),
            // 事件总线：每个订阅者独立缓冲（256 条），一条任务只产生 3 条事件，
            // 缓冲足够容纳"刚连上就补齐最近状态"；发布永不阻塞执行线程。
            events: Arc::new(EventBus::new()),
            cluster,
            started_at: chrono::Utc::now(),
            alerts,
        })
    }

    /// 写穿：把尚未落盘的变更立刻刷到磁盘。
    ///
    /// 存在的理由是一个**真实缺陷**：默认 500ms 才定时落盘，任务提交后如果进程被强杀
    /// （任务管理器结束、崩溃、断电），刚提交的任务在快照里根本不存在——重启后 404。
    /// 任务与记忆属于"用户已经在界面上看到它成功了"的数据，不能让它停在内存里等窗口。
    ///
    /// 由 `SA_FLUSH_ON_WRITE` 控制（默认开）；关掉后回落到纯定时落盘，行为与旧版一致。
    /// 刷盘次数进指标 `sa_store_write_through_total`，失败进 `sa_store_write_through_failed_total`，
    /// 这样"到底有没有真刷"可以在 /metrics 上核对，而不是靠信任。
    pub fn persist_now(&self) {
        if !self.config.flush_on_write {
            return;
        }
        match self.store.flush_if_dirty() {
            Ok(true) => self.metrics.inc("sa_store_write_through_total"),
            Ok(false) => {}
            Err(err) => {
                self.metrics.inc("sa_store_write_through_failed_total");
                tracing::error!(error = %err, "写穿落盘失败");
            }
        }
    }

    pub fn auth(&self) -> AuthService<'_> {
        AuthService {
            config: self.config.as_ref(),
            store: self.store.as_ref(),
            metrics: self.metrics.as_ref(),
            audit: self.audit.as_ref(),
            limiter: self.limiter.as_ref(),
        }
    }

    /// 启动期收敛：把上一次运行遗留的 `queued` / `running` 任务标成 `cancelled`。
    ///
    /// 为什么必须在启动时做这件事：异步模式让"进程跑到一半被杀"成为真实场景
    /// （关窗、崩溃、断电）。这些任务在快照里永远是 `running`，前端会一直转圈，
    /// 而没有任何线程会再来推进它们——这正是"看起来能用、实际坏了"的典型。
    ///
    /// 判定依据是**取消登记簿**：登记簿是进程内状态，启动时必然是空的，
    /// 因此启动后仍处于 queued/running 的任务一定没有执行体。
    /// 返回被收敛的条数，调用方负责打日志。
    pub fn reconcile_interrupted(&self) -> usize {
        let now = chrono::Utc::now();
        self.store.write(|db| {
            let mut fixed = 0usize;
            // 先收集 id：不能一边持有可变借用一边查登记簿（借用检查 + 语义都要求两段式）
            let orphan_ids: Vec<String> = db
                .tasks
                .iter()
                .filter(|(id, t)| !t.status.is_terminal() && self.cancellations.handle(id).is_none())
                .map(|(id, _)| id.clone())
                .collect();
            for id in orphan_ids {
                if let Some(t) = db.tasks.get_mut(&id) {
                    t.status = crate::domain::task::TaskStatus::Cancelled;
                    t.finished_at = Some(now);
                    t.updated_at = now;
                    t.result_summary = "任务已被取消：服务重启时该任务仍在执行，执行体已不存在。".into();
                    t.trace.push(crate::domain::task::TraceEntry {
                        at: now,
                        step_index: u8::try_from(t.results.len()).unwrap_or(u8::MAX),
                        agent_id: "-".into(),
                        phase: "reconcile".into(),
                        outcome: "cancelled".into(),
                        detail: "服务启动时发现无执行体的未完成任务，已收敛为已取消".into(),
                        attempt: 0,
                        tokens: 0,
                        duration_ms: None,
                    });
                    fixed += 1;
                }
            }
            fixed
        })
    }

    /// 同步执行（不可取消）：提交即跑完再返回，用于 `mode=sync`。
    pub fn exec(&self) -> crate::orchestration::executor::Executor<'_> {
        self.exec_with_cancel(None)
    }

    /// 带取消旗子的执行器：`mode=async` 的任务会传入登记簿里的旗子。
    /// 治理链路（鉴权 / 预算 / 重试 / 熔断 / 审计 / 指标）与同步路径完全一致，
    /// 唯一差别是每个步骤边界会多读一次旗子。
    pub fn exec_with_cancel<'a>(
        &'a self,
        cancel: Option<&'a std::sync::atomic::AtomicBool>,
    ) -> crate::orchestration::executor::Executor<'a> {
        crate::orchestration::executor::Executor {
            store: self.store.as_ref(),
            metrics: self.metrics.as_ref(),
            audit: self.audit.as_ref(),
            retry_limit: self.config.task_retry_limit,
            max_failed_steps: 2,
            backoff_ms: 200,
            runner: self.runner.as_deref(),
            cancel,
            concurrency: self.config.step_concurrency,
        }
    }

    /// 当前装配了哪些部件（只读视图，`GET /api/v1/observability/components`）。
    ///
    /// 传的是**实例**而非配置字符串：注册表里的 `active` 必须来自运行中的对象，
    /// 否则"配置说一套、跑的是另一套"时，这个接口会变成一纸谎言。
    pub fn components(&self) -> Vec<ComponentInfo> {
        crate::ports::registry(
            self.config.as_ref(),
            self.store.as_ref(),
            self.runner.as_deref(),
            self.audit.as_ref(),
            self.metrics.as_ref(),
            self.limiter.as_ref(),
        )
    }

    pub fn shared(self) -> SharedState {
        Arc::new(self)
    }
}
