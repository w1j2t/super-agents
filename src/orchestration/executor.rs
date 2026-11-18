//! 执行引擎：逐步鉴权 → 预算控制 → 重试纠错 → 熔断 → 合规复核 → 结果汇总 → 飞轮沉淀。
//!
//! 说明：本引擎执行的是"计划步骤"，步骤的效果由 `simulate` 提供确定性产出，
//! 接入真实模型/工具时只需替换 `simulate`，其余治理链路（鉴权、预算、重试、熔断、
//! 审计、指标、记忆沉淀）保持不变。

use crate::domain::agent::{self, AgentSpec};
use crate::domain::memory::{MemoryEntry, MemoryScope};
use crate::domain::task::{ModelProfile, PlanStep, StepResult, TaskRecord, TaskStatus, TraceEntry};
use crate::domain::user::User;
use crate::obs::audit::AuditSink;
use crate::obs::metrics::MetricsExporter;
use crate::orchestration::context::AssembledContext;
use crate::security::rbac;
use crate::store::Store;
use chrono::Utc;

pub const MAX_FAILED_STEPS: u32 = 3;
pub const MAX_DENIED_STREAK: u32 = 3;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum AbortKind {
    Budget,
    Circuit,
    Permission,
    /// 用户显式取消：不是故障，不能记进失败指标，否则"取消"会污染可用性口径
    Cancel,
}

pub struct Executor<'a> {
    pub store: &'a Store,
    pub metrics: &'a dyn MetricsExporter,
    pub audit: &'a dyn AuditSink,
    /// 单步最多重试次数（不含首次）
    pub retry_limit: u32,
    /// 单任务失败步骤熔断阈值
    pub max_failed_steps: u32,
    /// 重试退避基数（毫秒），测试可置 0
    pub backoff_ms: u64,
    /// 步骤执行后端；None 时使用默认 `SimRunner`（保持既有构造点零改动）
    pub runner: Option<&'a dyn StepRunner>,
    /// 取消旗子：`None` 表示本任务不可取消（同步接口直接执行完就走这条路）。
    /// 协作式取消只在**步骤边界**与**重试退避之前**生效，执行中的单步不会被打断。
    pub cancel: Option<&'a std::sync::atomic::AtomicBool>,
    /// 同一拓扑层内允许同时执行的步骤数（`SA_STEP_CONCURRENCY`，默认 1 = 严格串行）。
    ///
    /// 为什么默认 1 而不是 4：并行会同时拉起多个模型调用，对显存 / 内存的压力是叠加的。
    /// 本机实测（Core Ultra 7 155H 纯 CPU 推理）单步 129s 量级，两个 14B 并发会互相抢核
    /// 反而更慢；而三台 GX10 集群上是分布式并行，各节点独占算力。
    /// 所以"能不能并行"交给部署方按硬件决定，默认保持既有串行行为，**不改变任何现有验收口径**。
    pub concurrency: usize,
}

#[derive(Debug, Clone)]
pub struct ExecOutcome {
    pub status: TaskStatus,
    pub failed_steps: u32,
    pub reason: Option<String>,
}

#[derive(Debug, Clone)]
pub enum Verdict {
    Ok { tokens: u64, output: String },
    Retryable(String),
    Fatal(String),
}

/// 单步执行请求：只读视图。
///
/// 执行后端只看不改：所有状态记账（trace / results / usage / 指标）由治理层
/// `Executor` 统一完成，因此替换执行后端不会破坏审计与预算口径。
pub struct StepRequest<'a> {
    pub task: &'a TaskRecord,
    pub step: &'a PlanStep,
    pub spec: &'a AgentSpec,
    pub ctx: &'a AssembledContext,
    pub attempt: u32,
}

/// 单步执行结果：判定 + 需要由治理层补记的指标 / 轨迹。
#[derive(Debug, Clone)]
pub struct StepOutcome {
    pub verdict: Verdict,
    /// 本次执行需要自增的计数器（治理层统一打点，执行后端不直接依赖 Metrics）
    pub counters: Vec<&'static str>,
    /// 额外轨迹（如沙箱启动），由治理层按时间顺序落账
    pub extra_trace: Option<TraceEntry>,
}

impl StepOutcome {
    pub fn ok(tokens: u64, output: String) -> Self {
        Self {
            verdict: Verdict::Ok { tokens, output },
            counters: Vec::new(),
            extra_trace: None,
        }
    }

    pub fn retryable(message: impl Into<String>) -> Self {
        Self {
            verdict: Verdict::Retryable(message.into()),
            counters: Vec::new(),
            extra_trace: None,
        }
    }

    pub fn fatal(message: impl Into<String>) -> Self {
        Self {
            verdict: Verdict::Fatal(message.into()),
            counters: Vec::new(),
            extra_trace: None,
        }
    }

    pub fn counted(mut self, counter: &'static str) -> Self {
        self.counters.push(counter);
        self
    }

    pub fn with_trace(mut self, trace: TraceEntry) -> Self {
        self.extra_trace = Some(trace);
        self
    }
}

/// 步骤执行后端抽象。
///
/// 现状：默认实现 `SimRunner`（确定性模拟，供验收复现）。
/// Phase-2 接入真实模型 / 工具 / Python 编排 worker 时，只需实现本 trait；
/// 鉴权、预算降档、重试退避、熔断、审计、指标、飞轮沉淀整条治理链路无需改动。
pub trait StepRunner: Send + Sync {
    /// 后端标识，写入指标 `sa_executor_backend_total{backend="..."}`，便于观测灰度比例。
    fn backend(&self) -> &'static str;

    /// 执行单个步骤。实现方不得修改任务状态，只能返回判定与需要补记的信息。
    fn run_step(&self, req: &StepRequest<'_>) -> StepOutcome;
}

/// 默认后端：确定性模拟。
pub struct SimRunner;

/// 默认后端实例：零大小，可直接静态引用，测试无需注入。
pub static SIM_RUNNER: SimRunner = SimRunner;

/// 合规红线：**硬**——词面本身就指向违法或凭据窃取，不依赖上下文，命中即拒。
///
/// 为什么要分两级：旧实现只有一张表，而且只要是任务目标里出现就整段阻断。
/// 那张表混着"明文密码""身份证号"这类**中性词**——"写一个密码管理器""分析身份证号校验规则"
/// 都是正经需求，一前置就成了"正常需求被安全策略打回"，比不做安全更伤。
/// 所以：硬红线判意图（违法、窃取、越权、恶意软件），软红旗判语境（只交 guard 复核）。
const HARD_REDLINES: &[(&str, &str)] = &[
    ("盗取", "违法行为"),
    ("窃取", "违法行为"),
    ("拖库", "凭据窃取"),
    ("撞库", "凭据窃取"),
    ("绕过登录", "越权访问"),
    ("未经授权访问", "越权访问"),
    ("破解他人", "越权访问"),
    ("木马生成", "恶意软件"),
    ("勒索病毒", "恶意软件"),
    ("钓鱼页面", "欺诈"),
];

/// 合规红线：**软**——需要上下文判断的词，只交给 `guard-agent` 二审，不做入口拦截。
const SOFT_REDLINES: &[(&str, &str)] = &[
    ("身份证号", "个人敏感信息"),
    ("银行卡号", "个人敏感信息"),
    ("明文密码", "凭据泄露"),
];

/// 在已小写化的文本里查词表。返回命中的词与类别。
///
/// 抽成函数是为了让"入口查目标"与"出口查产物"共用同一套判定——
/// 两处各写一遍 `iter().find(|(w,_)| text.contains(w))`，
/// 改词表时极容易只改一处（SA-001 就是漏了产物这一路的后果）。
fn hit_redline<'a>(text_lower: &str, table: &'a [(&'a str, &'a str)]) -> Option<(&'a str, &'a str)> {
    table.iter().find(|(w, _)| text_lower.contains(w)).copied()
}

/// 单步执行需要由治理层"代为打点"的计数器。
///
/// 为什么不让执行线程直接调 `self.metrics.inc(...)`：那样并行时指标顺序不确定，
/// 更麻烦的是 `MetricsExporter` 的调用会散落在多个线程里，一旦某个后端的实现不是
/// 线程安全的，问题会在并发下随机出现。收集成值、回到调度线程统一落账，代价只有一个枚举。
enum StepCounter {
    Plain(&'static str),
    Labeled(&'static str, String),
}

/// 单步执行的全部效果（**不写 `TaskRecord`**）。
///
/// 这是并行调度的核心数据结构：并行线程只产出 `StepEffect`，不碰共享可变状态；
/// 调度线程按 `index` 升序把它归并进任务。于是"执行并行"与"结果确定"同时成立。
struct StepEffect {
    index: u8,
    name: String,
    traces: Vec<TraceEntry>,
    result: StepResult,
    tokens: u64,
    success: bool,
    /// 是否因越权被拒（参与"连续越权"熔断判定）
    denied: bool,
    counters: Vec<StepCounter>,
    /// 待落审计的值：(是否成功, 说明)
    audit: Option<(bool, String)>,
    /// 本步触发的终止信号（如重试退避期间被取消）
    abort: Option<(AbortKind, String)>,
}

impl StepEffect {
    fn with_audit(mut self, ok: bool, detail: String) -> Self {
        self.audit = Some((ok, detail));
        self
    }

    /// 构造一条"未进入执行后端就判失败"的效果（未注册 agent / 越权）。
    #[allow(clippy::too_many_arguments)]
    fn failed(
        step: &PlanStep,
        profile: ModelProfile,
        err: String,
        attempts: u32,
        traces: Vec<TraceEntry>,
        counters: Vec<StepCounter>,
        denied: bool,
        abort: Option<(AbortKind, String)>,
    ) -> Self {
        Self {
            index: step.index,
            name: step.name.clone(),
            traces,
            result: StepResult {
                index: step.index,
                agent_id: step.agent_id.clone(),
                name: step.name.clone(),
                success: false,
                model_profile: profile,
                tokens: 0,
                attempts,
                output: String::new(),
                error: Some(err),
            },
            tokens: 0,
            success: false,
            denied,
            counters,
            audit: None,
            abort,
        }
    }
}

/// 把线性计划切成**拓扑层**：同一层内没有任何依赖关系，可以并行执行。
///
/// 因为领域层已强制"依赖只能指向更早的 index"（见 `PlanStep::validate_dependencies`），
/// 这里一遍正序扫描就够，既不需要 Kahn 算法也不需要环检测——**结构上不可能有环**。
/// 把约束前置到领域层的收益就在这里：运行期调度代码短到没有出错路径。
///
/// 悬空依赖（指向计划中不存在的步骤）不在这里报错：调度层会把它判为
/// `invalid-dependency` 并跳过，让"计划写错"表现为一条可读的失败记录，
/// 而不是执行器内部 panic。
pub fn plan_layers(plan: &[PlanStep]) -> Vec<Vec<u8>> {
    use std::collections::HashMap;
    let mut depth_of: HashMap<u8, u8> = HashMap::new();
    let mut layers: Vec<Vec<u8>> = Vec::new();
    for step in plan {
        let mut depth = 0u8;
        for dep in &step.depends_on {
            if let Some(d) = depth_of.get(dep) {
                depth = depth.max(d.saturating_add(1));
            }
        }
        depth_of.insert(step.index, depth);
        let di = depth as usize;
        while layers.len() <= di {
            layers.push(Vec::new());
        }
        layers[di].push(step.index);
    }
    for layer in &mut layers {
        layer.sort_unstable();
    }
    layers
}

impl<'a> Executor<'a> {
    pub fn run(
        &self,
        task: &mut TaskRecord,
        user: &User,
        ctx: &AssembledContext,
        request_id: &str,
    ) -> ExecOutcome {
        // ---- 入口零步预检：goal 级硬红线 ----
        // 必须在拉起任何 runner 之前。旧实现把红线塞在 guard 步骤里，
        // 意味着"有没有拦到"取决于路由有没有排到 guard、以及主步骤是不是已经跑完；
        // 主步骤产出的东西，事后 guard 再怎么否决也收不回来。
        // 这里命中即整任务失败：不进 runner、不耗预算、不生成产物。
        if let Some((word, category)) = hit_redline(&task.goal.to_lowercase(), HARD_REDLINES) {
            let reason = format!("合规红线命中「{word}」（{category}），任务未启动即阻断，未消耗执行预算");
            self.metrics.inc("sa_compliance_blocks_total");
            self.metrics.inc("sa_tasks_started_total");
            self.metrics.inc("sa_tasks_failed_total");
            task.status = TaskStatus::Failed;
            task.updated_at = Utc::now();
            task.finished_at = Some(Utc::now());
            task.result_summary = reason.clone();
            task.trace.push(TraceEntry {
                at: Utc::now(),
                step_index: 0,
                agent_id: "governance".into(),
                phase: "compliance".into(),
                outcome: "blocked_at_entry".into(),
                detail: reason.clone(),
                attempt: 0,
                tokens: 0,
                duration_ms: None,
            });
            self.audit.record(
                &user.id,
                "task.compliance_blocked_at_entry",
                &task.id,
                false,
                &reason,
                request_id,
            );
            return ExecOutcome {
                status: TaskStatus::Failed,
                failed_steps: 0,
                reason: Some(reason),
            };
        }

        let started = std::time::Instant::now();
        task.status = TaskStatus::Running;
        task.updated_at = Utc::now();
        self.metrics.inc("sa_tasks_started_total");
        self.metrics
            .gauge_set("sa_context_tokens_used", "", ctx.used_tokens as f64);
        if ctx.truncated {
            self.metrics.inc("sa_context_truncations_total");
        }

        let plan = task.plan.clone();
        let runner: &dyn StepRunner = match self.runner {
            Some(custom) => custom,
            None => &SIM_RUNNER,
        };
        self.metrics.inc_by(
            "sa_executor_backend_total",
            &format!("backend=\"{}\"", runner.backend()),
            1,
        );
        let mut tokens_used: u64 = 0;
        let mut failed: u32 = 0;
        let mut denied_streak: u32 = 0;
        let mut abort: Option<(AbortKind, String)> = None;

        // ===================== 依赖图调度（多 agents 集群的编排层）=====================
        // 旧实现是 `for step in &plan` 的严格串行队列。现在按 `depends_on` 切成拓扑层，
        // 同层步骤在 `self.concurrency` 上限内**并行**执行，层与层之间是同步栅栏。
        //
        // 治理语义一条不减：鉴权 / 预算降档 / 红线闸门 / 重试退避 / 沙箱记账 / 熔断 /
        // 审计落盘 / 指标打点仍然**逐步骤持有**，全部在 `run_one_step` 内完成。
        // 唯一被重构的是"写回任务"这一步：并行线程里不能同时持有 `&mut TaskRecord`，
        // 所以先把效果收集成值对象（`StepEffect`），回到调度线程后按 `index` 升序
        // **稳定归并**——trace / results 的先后与串行执行完全一致（可复现），
        // 而真正的执行是并行的。
        let layers = plan_layers(&plan);
        let known: std::collections::HashSet<u8> = plan.iter().map(|s| s.index).collect();
        let by_index: std::collections::HashMap<u8, PlanStep> =
            plan.iter().map(|s| (s.index, s.clone())).collect();
        // 步骤 index → 是否成功。依赖判定只看这张表，不看 results 的顺序。
        let mut ok_map: std::collections::HashMap<u8, bool> = std::collections::HashMap::new();
        // 串行模式下"因取消而未起跑"的步骤 index（并行模式不设，取消在层边界生效）
        let mut cancel_hit: Option<u8> = None;

        for (depth, layer) in layers.iter().enumerate() {
            if abort.is_some() {
                break;
            }
            let first_index = layer.first().copied().unwrap_or(0);
            let first_agent = by_index
                .get(&first_index)
                .map(|s| s.agent_id.clone())
                .unwrap_or_default();

            // ---- 0. 取消检查（层边界）----
            // 与旧实现同样的口径：取消后**不再发起任何新步骤**（含鉴权、预算降档
            // 这些看起来无副作用的动作——它们都会写审计与指标，让一条已取消的任务
            // 仍然留下执行痕迹）。
            if crate::orchestration::cancel::is_cancelled(self.cancel) {
                self.metrics.inc("sa_tasks_cancelled_total");
                task.trace.push(TraceEntry {
                    at: Utc::now(),
                    step_index: first_index,
                    agent_id: first_agent,
                    phase: "cancel".into(),
                    outcome: "cancelled".into(),
                    detail: format!("收到取消请求，第 {} 层起未执行", depth),
                    attempt: 0,
                    tokens: 0,
                    duration_ms: None,
                });
                abort = Some((
                    AbortKind::Cancel,
                    format!("任务已被用户取消，第 {} 层起未执行", depth),
                ));
                break;
            }

            // ---- 1. 本层可执行集合：依赖校验 + 依赖失败跳过 ----
            let mut runnable: Vec<u8> = Vec::new();
            for idx in layer {
                let Some(step) = by_index.get(idx) else {
                    continue;
                };
                if let Some(msg) = step.validate_dependencies() {
                    self.record_step_failure(task, step, "invalid-dependency", &msg);
                    ok_map.insert(*idx, false);
                    failed += 1;
                    continue;
                }
                if let Some(bad) = step.depends_on.iter().find(|d| !known.contains(d)) {
                    let msg = format!("依赖的第 {} 步不在计划中，本步不执行", bad + 1);
                    self.record_step_failure(task, step, "invalid-dependency", &msg);
                    ok_map.insert(*idx, false);
                    failed += 1;
                    continue;
                }
                if let Some(bad) = step
                    .depends_on
                    .iter()
                    .find(|d| ok_map.get(d) != Some(&true))
                {
                    // 这行是并行编排最容易做错的地方：前置失败时**必须**停手。
                    // 否则下游拿着半截产物继续推理，产出看起来完整、依据却是空的
                    // —— 比直接失败危险得多。
                    let msg = format!(
                        "前置第 {} 步未成功，本步不执行（避免用半截产物继续推理）",
                        bad + 1
                    );
                    self.record_step_failure(task, step, "dependency-failed", &msg);
                    ok_map.insert(*idx, false);
                    failed += 1;
                    continue;
                }
                runnable.push(*idx);
            }
            if runnable.is_empty() {
                continue;
            }

            // ---- 2. 预算预扣（并行安全的关键）----
            // 串行版用"共享的 tokens_used + 本步 est"判预算；并行时多个步骤同时读同一个
            // 剩余额度会**集体放行**，一整个层的花费一起超支。所以在层开始前按 index
            // 顺序从额度池里逐个扣：扣不下就降档（与原逻辑同规则、同上限 2 次），
            // 降到底仍不够就整层停手。预扣让"层内并行"与"全局不超支"两件事同时成立。
            let mut pool = task.budget_tokens.saturating_sub(tokens_used);
            let mut quotas: std::collections::HashMap<u8, (ModelProfile, u64)> =
                std::collections::HashMap::new();
            for idx in &runnable {
                let Some(step) = by_index.get(idx) else {
                    continue;
                };
                let mut profile = step.model_profile;
                let mut est = step.est_tokens;
                let mut downgrades = 0;
                while est > pool {
                    match profile.downgrade() {
                        Some(next) if downgrades < 2 => {
                            profile = next;
                            est = ((est as f64) * 0.4).round() as u64;
                            downgrades += 1;
                            self.metrics.inc("sa_budget_downgrades_total");
                            task.trace.push(TraceEntry {
                                at: Utc::now(),
                                step_index: step.index,
                                agent_id: step.agent_id.clone(),
                                phase: "budget".into(),
                                outcome: "downgrade".into(),
                                detail: format!("预算不足，降档至 {}", profile.as_str()),
                                attempt: 0,
                                tokens: 0,
                                duration_ms: None,
                            });
                        }
                        _ => {
                            abort = Some((
                                AbortKind::Budget,
                                format!(
                                    "预算耗尽（已用 {tokens_used} / 上限 {}），剩余步骤未执行",
                                    task.budget_tokens
                                ),
                            ));
                            break;
                        }
                    }
                }
                if abort.is_some() {
                    break;
                }
                pool = pool.saturating_sub(est);
                quotas.insert(*idx, (profile, est));
            }
            if abort.is_some() {
                break;
            }

            // ---- 3. 层内执行（宽度 1 或并发上限 1 时退化为纯串行，零线程开销）----
            let effects: Vec<StepEffect> = {
                // 只读快照：进线程的只能是不可变借用（借用在本块结束，之后才能 &mut task）
                let snapshot: &TaskRecord = &*task;
                let parallelism = self.concurrency.clamp(1, 8).min(runnable.len());
                if parallelism <= 1 {
                    let mut out = Vec::with_capacity(runnable.len());
                    for idx in &runnable {
                        // 串行模式下每个步骤边界都要读取消旗子，与原实现逐字一致。
                        // 并行模式下这一步做不到（同层已同时起跑），取消的生效点落在
                        // 层边界——这是并行的固有代价，写在文档而不是藏起来。
                        if crate::orchestration::cancel::is_cancelled(self.cancel) {
                            cancel_hit = Some(*idx);
                            break;
                        }
                        let Some(step) = by_index.get(idx) else {
                            continue;
                        };
                        let Some((profile, quota)) = quotas.get(idx).copied() else {
                            continue;
                        };
                        out.push(self.run_one_step(
                            step, profile, quota, runner, snapshot, ctx, request_id, user,
                        ));
                    }
                    out
                } else {
                    let mut collected: Vec<StepEffect> = Vec::with_capacity(runnable.len());
                    let by_index_ref = &by_index;
                    let quotas_ref = &quotas;
                    // `std::thread::scope`：线程可以借用栈上的数据，不必把 TaskRecord
                    // clone 一份塞进线程——并行不该以"复制整条任务"为代价。
                    // 切组方式决定并发度：切成"最多 parallelism 个线程"，
                    // 每组 ceil(len/parallelism) 步。若按 chunks(parallelism) 切，
                    // 组数会变成 ceil(len/parallelism) 个、每组内串行 —— 步骤一少
                    // 就退化成单线程，并行白做（这个坑实测踩过一次）。
                    let group_size = (runnable.len() + parallelism - 1) / parallelism;
                    std::thread::scope(|scope| {
                        let mut handles = Vec::new();
                        for chunk in runnable.chunks(group_size.max(1)) {
                            let group: Vec<u8> = chunk.to_vec();
                            let fallback = group.clone();
                            let handle = scope.spawn(move || {
                                let mut out = Vec::with_capacity(group.len());
                                for idx in &group {
                                    let Some(step) = by_index_ref.get(idx) else {
                                        continue;
                                    };
                                    let Some((profile, quota)) = quotas_ref.get(idx).copied()
                                    else {
                                        continue;
                                    };
                                    out.push(self.run_one_step(
                                        step, profile, quota, runner, snapshot, ctx, request_id,
                                        user,
                                    ));
                                }
                                out
                            });
                            handles.push((fallback, handle));
                        }
                        for (fallback, handle) in handles {
                            match handle.join() {
                                Ok(v) => collected.extend(v),
                                // 某个 worker 线程 panic（执行后端内部 bug）：
                                // 不能让这条步骤"凭空消失"，否则它的下游会以为
                                // 依赖成功（`ok_map` 缺项 → 实际判失败，安全），
                                // 但本步既没 trace 也没 results，读起来像漏执行。
                                // 这里补一条明确的失败记录，故障可见。
                                Err(_) => {
                                    for idx in fallback {
                                        if let Some(step) = by_index.get(&idx) {
                                            collected.push(StepEffect {
                                                index: step.index,
                                                name: step.name.clone(),
                                                traces: vec![TraceEntry {
                                                    at: Utc::now(),
                                                    step_index: step.index,
                                                    agent_id: step.agent_id.clone(),
                                                    phase: "step".into(),
                                                    outcome: "failed".into(),
                                                    detail: "执行线程异常退出".into(),
                                                    attempt: 0,
                                                    tokens: 0,
                                                    duration_ms: None,
                                                }],
                                                result: StepResult {
                                                    index: step.index,
                                                    agent_id: step.agent_id.clone(),
                                                    name: step.name.clone(),
                                                    success: false,
                                                    model_profile: step.model_profile,
                                                    tokens: 0,
                                                    attempts: 0,
                                                    output: String::new(),
                                                    error: Some("执行线程异常退出".into()),
                                                },
                                                tokens: 0,
                                                success: false,
                                                denied: false,
                                                counters: vec![StepCounter::Plain(
                                                    "sa_task_step_failures_total",
                                                )],
                                                audit: None,
                                                abort: None,
                                            });
                                        }
                                    }
                                }
                            }
                        }
                    });
                    collected.sort_by_key(|e| e.index);
                    collected
                }
            };

            // ---- 4. 层末稳定归并（按 index 升序，与串行执行结果逐字节一致）----
            for eff in effects {
                task.trace.extend(eff.traces);
                if !eff.success {
                    failed += 1;
                }
                // 连续越权计数：原实现在单步内维护，并行下改为层末按 index 顺序重放，
                // 顺序确定，所以"连续 3 次越权"的判定与串行完全一致。
                if eff.denied {
                    denied_streak += 1;
                } else if eff.success {
                    denied_streak = 0;
                }
                if eff.success {
                    tokens_used = tokens_used.saturating_add(eff.tokens);
                }
                ok_map.insert(eff.index, eff.success);
                for counter in eff.counters {
                    match counter {
                        StepCounter::Plain(name) => self.metrics.inc(name),
                        StepCounter::Labeled(name, label) => {
                            self.metrics.inc_by(name, &label, 1)
                        }
                    }
                }
                if let Some((ok, detail)) = eff.audit {
                    self.audit.record(
                        &user.id,
                        "task.step",
                        &format!("{}/{}", task.id, eff.name),
                        ok,
                        &detail,
                        request_id,
                    );
                }
                task.results.push(eff.result);
                if let Some(signal) = eff.abort {
                    if abort.is_none() {
                        abort = Some(signal);
                    }
                }
            }

            if let Some(idx) = cancel_hit {
                if abort.is_none() {
                    self.metrics.inc("sa_tasks_cancelled_total");
                    task.trace.push(TraceEntry {
                        at: Utc::now(),
                        step_index: idx,
                        agent_id: by_index
                            .get(&idx)
                            .map(|s| s.agent_id.clone())
                            .unwrap_or_default(),
                        phase: "cancel".into(),
                        outcome: "cancelled".into(),
                        detail: format!("收到取消请求，第 {} 步起未执行", idx + 1),
                        attempt: 0,
                        tokens: 0,
                        duration_ms: None,
                    });
                    abort = Some((
                        AbortKind::Cancel,
                        format!("任务已被用户取消，第 {} 步起未执行", idx + 1),
                    ));
                }
            }

            // 熔断与越权熔断都放在层末判：同层已经起跑的步骤无法中途打断（这是并行的
            // 固有代价），但**下一层一定不会再起**。串行场景下与旧逻辑完全等价。
            if abort.is_none() && denied_streak >= MAX_DENIED_STREAK {
                abort = Some((
                    AbortKind::Permission,
                    format!("连续 {denied_streak} 次越权，任务终止"),
                ));
            }
            if abort.is_none() && failed >= self.max_failed_steps {
                abort = Some((
                    AbortKind::Circuit,
                    format!("熔断：累计 {failed} 个步骤失败，停止后续执行"),
                ));
            }
        }

        task.tokens_used = tokens_used;
        task.updated_at = Utc::now();
        task.finished_at = Some(task.updated_at);

        // ---- 5. 状态收敛 ----
        let total_steps = plan.len() as u32;
        let guard_ok = task
            .results
            .iter()
            .any(|r| r.agent_id == "guard-agent" && r.success);
        let status = match &abort {
            Some((AbortKind::Budget, _)) => TaskStatus::AbortedBudget,
            Some((AbortKind::Cancel, _)) => TaskStatus::Cancelled,
            Some((_, _)) => TaskStatus::Failed,
            None if failed == 0 => TaskStatus::Succeeded,
            None if failed >= total_steps => TaskStatus::Failed,
            None => TaskStatus::PartiallySucceeded,
        };

        // ---- 6. 一致性评分（正确性校验的可观测口径）----
        let mut score = task.agreement_score;
        score -= 0.22 * failed as f64;
        if ctx.truncated {
            score -= 0.05;
        }
        if guard_ok {
            score += 0.05;
        }
        if status == TaskStatus::AbortedBudget {
            score -= 0.1;
        }
        if status == TaskStatus::Cancelled {
            // 取消不是"做完了"：留一个明确的扣分，避免半截任务拿到接近满分的评分
            score -= 0.15;
        }
        task.agreement_score = (score.clamp(0.0, 0.99) * 100.0).round() / 100.0;

        // ---- 7. 结果汇总 ----
        task.result_summary = synthesize(task, ctx, status, &abort);
        task.status = status;
        task.updated_at = Utc::now();

        // ---- 8. 指标 ----
        let elapsed_ms = started.elapsed().as_millis() as f64;
        self.metrics.observe("sa_task_duration_ms", "", elapsed_ms);
        self.metrics
            .observe("sa_tokens_per_task", "", tokens_used as f64);
        self.metrics
            .gauge_set("sa_task_agreement_score", "", task.agreement_score);
        // 取消既不是成功也不是失败：单独计数，混进 failures_total 会让
        // "可用性"看起来被用户自己按下的取消拉低。
        if matches!(status, TaskStatus::Cancelled) {
            self.metrics.inc("sa_task_cancellations_total");
        } else if !matches!(status, TaskStatus::Succeeded | TaskStatus::PartiallySucceeded) {
            self.metrics.inc("sa_task_failures_total");
        }
        if matches!(abort, Some((AbortKind::Circuit, _))) {
            self.metrics.inc("sa_task_circuit_open_total");
        }

        // C25 防幻觉：把"证据缺口"变成真实打点，而不是只写在能力规格里。
        // 口径刻意保守——只统计执行链能客观判定的三类缺口，不做语义级猜测；
        // 语义复核由 guard-agent 的 hallucination-check 步负责，这里只考核果。
        //
        // 每次任务收尾都做一次一致性校验（`correctness_check` 能力承诺的
        // `sa_correctness_checks_total` 此前零落点）。计数记在**校验执行**之后、
        // 判定之前：这张表要回答的是"校验覆盖率"（校验次数 ÷ 建单数应为 1:1），
        // 若只在"不达标"时计数，等于把覆盖率指标变成了失败率指标，两个数字都不能用。
        self.metrics.inc("sa_correctness_checks_total");
        let mut hallucination_flags = 0u64;
        if ctx.truncated {
            // 上下文被裁剪：结论缺完整依据，必须记一笔
            hallucination_flags += 1;
        }
        // 每个失败步 = 一处没有执行证据的结论
        hallucination_flags += failed as u64;
        if task.agreement_score < 0.6 {
            // 整体一致性过低，交付前应人工复核
            hallucination_flags += 1;
        }
        if hallucination_flags > 0 {
            self.metrics
                .inc_by("sa_hallucination_flags_total", "", hallucination_flags);
        }

        // ---- 9. 数据飞轮：把可复用的经验写入共享记忆 ----
        if matches!(status, TaskStatus::Succeeded | TaskStatus::PartiallySucceeded) {
            self.record_flywheel(task, user);
        }

        ExecOutcome {
            status,
            failed_steps: failed,
            reason: abort.map(|(_, reason)| reason),
        }
    }

    /// 执行单个步骤：**只做计算，不写 `TaskRecord`**，返回可直接归并的效果对象。
    ///
    /// 与原串行实现逐条对齐，一步不减：
    /// agent 注册检查 → 角色 ∩ agent 双鉴权 → 合规红线闸门 → 重试退避（退避中同样
    /// 响应取消）→ 沙箱记账 → 结果判定。
    ///
    /// 两处刻意的差异：
    /// 1. **预算降档**上移到层前预扣了（并行下多个步骤同时读同一额度必然集体放行，
    ///    只有集中一次性扣减才能保证不超支），这里只接收最终档位；
    /// 2. **审计与指标**不在这里落盘，而是装进 `StepEffect` 由调度线程按 index 顺序
    ///    统一写——否则并行会让审计日志的先后取决于谁先跑完（同一份输入两次执行
    ///    审计顺序不同，等于不可复现）。
    #[allow(clippy::too_many_arguments)]
    fn run_one_step(
        &self,
        step: &PlanStep,
        profile: ModelProfile,
        _quota: u64,
        runner: &dyn StepRunner,
        task: &TaskRecord,
        ctx: &AssembledContext,
        _request_id: &str,
        user: &User,
    ) -> StepEffect {
        let mut traces: Vec<TraceEntry> = Vec::new();
        let mut counters: Vec<StepCounter> = Vec::new();
        // 整步计时起点（含重试与退避的总耗时）：失败路径要用它回答
        // "这一步到底卡了多久"，而不是让前端拿两条 trace 的 at 相减。
        let step_t0 = std::time::Instant::now();

        // ---- 1. agent 必须存在于注册表 ----
        let Some(spec) = agent::find(&step.agent_id) else {
            return StepEffect::failed(
                step,
                profile,
                "步骤引用了未注册的 agent".into(),
                0,
                vec![TraceEntry {
                    at: Utc::now(),
                    step_index: step.index,
                    agent_id: step.agent_id.clone(),
                    phase: "unregistered-agent".into(),
                    outcome: "failed".into(),
                    detail: "步骤引用了未注册的 agent".into(),
                    attempt: 0,
                    tokens: 0,
                    duration_ms: None,
                }],
                vec![StepCounter::Plain("sa_task_step_failures_total")],
                false,
                None,
            );
        };
        counters.push(StepCounter::Labeled(
            "sa_agent_dispatches_total",
            format!("agent=\"{}\"", spec.id),
        ));

        // ---- 2. 权力校验：用户角色 ∩ agent 自身权限 ----
        // CrossAgent 不是用户特权，而是"平台允许这个 agent 把活分下去"的编排动作：
        // 用户没有这张票（Member 已被收回），但 plan 里的第 0 步必然跨 agent。
        // 因此该权限只对 agent 自己的能力边界校验，不对用户角色校验。
        let role_ok = if step.permission == crate::security::rbac::Permission::CrossAgent {
            true
        } else {
            rbac::allows(user.role, step.permission)
        };
        let agent_ok = spec.permissions.contains(&step.permission);
        if !(role_ok && agent_ok) {
            let detail = format!(
                "越权：角色 {} {} 权限 {}；agent {} {}",
                user.role.as_str(),
                if role_ok { "具备" } else { "缺少" },
                step.permission.as_str(),
                spec.id,
                if agent_ok { "具备" } else { "缺少" }
            );
            return StepEffect::failed(
                step,
                profile,
                "权限不足，步骤被拒绝，未执行".into(),
                0,
                vec![TraceEntry {
                    at: Utc::now(),
                    step_index: step.index,
                    agent_id: step.agent_id.clone(),
                    phase: "authz-denied".into(),
                    outcome: "failed".into(),
                    detail: "权限不足，步骤被拒绝，未执行".into(),
                    attempt: 0,
                    tokens: 0,
                    duration_ms: None,
                }],
                vec![
                    StepCounter::Plain("sa_authz_denied_total"),
                    StepCounter::Plain("sa_task_step_failures_total"),
                ],
                true,
                None,
            )
            .with_audit(false, detail);
        }

        counters.push(StepCounter::Labeled(
            "sa_model_profile_used_total",
            format!("profile=\"{}\"", profile.as_str()),
        ));

        // ---- 3.9 合规红线前置闸门（治理层，与执行后端无关）----
        // 红线判定下沉到治理层的原因见文件顶部说明：一旦把 SA_RUNNER 换成 process，
        // 「命中红线即阻断」这条安全底线不能因为换部件而整段消失。
        // 硬红线已在 `Executor::run` 入口拦过（走到这里说明没命中），
        // 所以 guard 这一步只负责"需要上下文判断"的软红旗——它是一票否决的**二审**，
        // 不再是唯一闸门。
        let compliance: Option<String> = if spec.id == "guard-agent" {
            hit_redline(&task.goal.to_lowercase(), SOFT_REDLINES)
                .map(|(w, c)| format!("合规红线命中「{w}」（{c}），已阻断，不生成产物"))
        } else {
            None
        };

        // ---- 4. 执行 + 重试纠错 ----
        let max_attempts = 1 + self.retry_limit;
        let mut attempts = 0u32;
        let mut step_ok = false;
        let mut last_err: Option<String> = None;
        let mut step_tokens = 0u64;
        let mut output = String::new();
        let mut abort: Option<(AbortKind, String)> = None;

        while attempts < max_attempts {
            attempts += 1;
            // 单次尝试计时：耗时口径见 `duration_ms` 的字段注释（attempt ≠ step）
            let attempt_t0 = std::time::Instant::now();
            let mut outcome = {
                let req = StepRequest {
                    task,
                    step,
                    spec,
                    ctx,
                    attempt: attempts,
                };
                match &compliance {
                    // 命中红线：不进入任何执行后端，直接判死，且不消耗重试与预算
                    Some(msg) => {
                        StepOutcome::fatal(msg.clone()).counted("sa_compliance_blocks_total")
                    }
                    None => runner.run_step(&req),
                }
            };

            // 沙箱记账归治理层所有（与执行后端无关）。
            let backend_owns_sandbox_trace = outcome
                .extra_trace
                .as_ref()
                .map(|t| t.phase == "sandbox")
                .unwrap_or(false);
            if step.sandbox && !backend_owns_sandbox_trace {
                outcome = outcome.counted("sa_sandbox_runs_total").with_trace(TraceEntry {
                    at: Utc::now(),
                    step_index: step.index,
                    agent_id: spec.id.to_string(),
                    phase: "sandbox".into(),
                    outcome: "started".into(),
                    detail: format!("沙箱画像 {}", spec.sandbox_profile),
                    attempt: attempts,
                    tokens: 0,
                    duration_ms: None,
                });
            }

            for counter in &outcome.counters {
                counters.push(StepCounter::Plain(*counter));
            }
            if let Some(trace) = outcome.extra_trace.take() {
                traces.push(trace);
            }
            match outcome.verdict {
                Verdict::Ok { tokens, output: out } => {
                    // ---- 产物级硬红线扫描 ----
                    // 目标干净 ≠ 产出干净：模型会自己加戏，`SA_RUNNER=process` 跑的是脚本，
                    // 谁都不能保证它吐出来的东西和任务目标一致。旧的"只看 goal"等于只看完题，
                    // 不看答卷。命中即把产物当没生成过：不计成功、不写 output、不进重试
                    // （重试只会再产出一遍同样的东西）。
                    if let Some((word, category)) = hit_redline(&out.to_lowercase(), HARD_REDLINES) {
                        let msg =
                            format!("产物命中合规红线「{word}」（{category}），已丢弃产物并终止该步骤");
                        counters.push(StepCounter::Plain("sa_compliance_blocks_total"));
                        last_err = Some(msg.clone());
                        traces.push(TraceEntry {
                            at: Utc::now(),
                            step_index: step.index,
                            agent_id: step.agent_id.clone(),
                            phase: "compliance".into(),
                            outcome: "blocked".into(),
                            detail: msg,
                            attempt: attempts,
                            tokens,
                            duration_ms: Some(attempt_t0.elapsed().as_millis() as u64),
                        });
                        break;
                    }
                    step_tokens = tokens;
                    output = out;
                    step_ok = true;
                    traces.push(TraceEntry {
                        at: Utc::now(),
                        step_index: step.index,
                        agent_id: step.agent_id.clone(),
                        phase: "execute".into(),
                        outcome: "ok".into(),
                        detail: format!("第 {attempts} 次尝试成功"),
                        attempt: attempts,
                        tokens,
                        duration_ms: Some(attempt_t0.elapsed().as_millis() as u64),
                    });
                    break;
                }
                Verdict::Retryable(msg) => {
                    last_err = Some(msg.clone());
                    traces.push(TraceEntry {
                        at: Utc::now(),
                        step_index: step.index,
                        agent_id: step.agent_id.clone(),
                        phase: "execute".into(),
                        outcome: "retryable_error".into(),
                        detail: msg,
                        attempt: attempts,
                        tokens: 0,
                        duration_ms: Some(attempt_t0.elapsed().as_millis() as u64),
                    });
                    if attempts < max_attempts {
                        counters.push(StepCounter::Plain("sa_task_retries_total"));
                        counters.push(StepCounter::Plain("sa_tool_failures_total"));
                        // 退避等待正是最该响应取消的时刻：用户已经不想等了，
                        // 再睡完指数退避才理会，体验等于没取消。
                        if crate::orchestration::cancel::is_cancelled(self.cancel) {
                            abort = Some((
                                AbortKind::Cancel,
                                "任务在重试退避期间被取消".to_string(),
                            ));
                            break;
                        }
                        if self.backoff_ms > 0 {
                            // 指数退避：200ms、400ms、800ms……
                            let wait = self.backoff_ms * 2u64.pow(attempts - 1);
                            std::thread::sleep(std::time::Duration::from_millis(wait.min(2_000)));
                        }
                    }
                }
                Verdict::Fatal(msg) => {
                    last_err = Some(msg.clone());
                    traces.push(TraceEntry {
                        at: Utc::now(),
                        step_index: step.index,
                        agent_id: step.agent_id.clone(),
                        phase: "execute".into(),
                        outcome: "fatal_error".into(),
                        detail: msg,
                        attempt: attempts,
                        tokens: 0,
                        duration_ms: Some(attempt_t0.elapsed().as_millis() as u64),
                    });
                    break;
                }
            }
        }

        if step_ok {
            // 成功路径也要留一条 `phase=step` 的收尾记录：失败路径有、成功路径没有，
            // 会让"按 phase 分组统计"的前端在成功任务上少算一步。耗时记整步（含重试）。
            traces.push(TraceEntry {
                at: Utc::now(),
                step_index: step.index,
                agent_id: step.agent_id.clone(),
                phase: "step".into(),
                outcome: "ok".into(),
                detail: format!("完成，共 {attempts} 次尝试"),
                attempt: attempts,
                tokens: step_tokens,
                duration_ms: Some(step_t0.elapsed().as_millis() as u64),
            });
            counters.push(StepCounter::Labeled(
                "sa_task_steps_total",
                format!("outcome=\"ok\",agent=\"{}\"", spec.id),
            ));
            StepEffect {
                index: step.index,
                name: step.name.clone(),
                traces,
                result: StepResult {
                    index: step.index,
                    agent_id: step.agent_id.clone(),
                    name: step.name.clone(),
                    success: true,
                    model_profile: profile,
                    tokens: step_tokens,
                    attempts,
                    output,
                    error: None,
                },
                tokens: step_tokens,
                success: true,
                denied: false,
                counters,
                audit: Some((
                    true,
                    format!("agent={} attempts={attempts}", spec.id),
                )),
                abort,
            }
        } else {
            let err = last_err.unwrap_or_else(|| "未知错误".into());
            traces.push(TraceEntry {
                at: Utc::now(),
                step_index: step.index,
                agent_id: step.agent_id.clone(),
                phase: "step".into(),
                outcome: "failed".into(),
                detail: err.clone(),
                attempt: attempts,
                tokens: 0,
                duration_ms: Some(step_t0.elapsed().as_millis() as u64),
            });
            counters.push(StepCounter::Labeled(
                "sa_task_steps_total",
                format!("outcome=\"failed\",agent=\"{}\"", spec.id),
            ));
            counters.push(StepCounter::Plain("sa_task_step_failures_total"));
            StepEffect {
                index: step.index,
                name: step.name.clone(),
                traces,
                result: StepResult {
                    index: step.index,
                    agent_id: step.agent_id.clone(),
                    name: step.name.clone(),
                    success: false,
                    model_profile: profile,
                    tokens: 0,
                    attempts,
                    output: String::new(),
                    error: Some(err),
                },
                tokens: 0,
                success: false,
                denied: false,
                counters,
                audit: Some((false, format!("agent={} attempts={attempts}", spec.id))),
                abort,
            }
        }
    }

    fn record_step_failure(&self, task: &mut TaskRecord, step: &PlanStep, phase: &str, detail: &str) {
        task.trace.push(TraceEntry {
            at: Utc::now(),
            step_index: step.index,
            agent_id: step.agent_id.clone(),
            phase: phase.to_string(),
            outcome: "failed".into(),
            detail: detail.to_string(),
            attempt: 0,
            tokens: 0,
            duration_ms: None,
        });
        task.results.push(StepResult {
            index: step.index,
            agent_id: step.agent_id.clone(),
            name: step.name.clone(),
            success: false,
            model_profile: step.model_profile,
            tokens: 0,
            attempts: 0,
            output: String::new(),
            error: Some(detail.to_string()),
        });
        self.metrics.inc("sa_task_step_failures_total");
    }

    /// 步骤效果生成逻辑已抽离到 `SimRunner`（见文件下方 `impl StepRunner for SimRunner`），
    /// 治理层不再硬编码执行后端。

    fn record_flywheel(&self, task: &TaskRecord, user: &User) {
        // 模拟产出不是真实执行结果，一律不计入飞轮（三方审查 H2）：
        // 否则"什么都没做"也会沉淀经验记忆与 sa_flywheel_events_total，让飞轮数字虚高。
        let backend = match self.runner {
            Some(r) => r.backend(),
            None => SIM_RUNNER.backend(),
        };
        if backend == "sim" {
            self.metrics.inc("sa_flywheel_skipped_total");
            return;
        }
        let key = format!("task:{}", task.task_type.as_str());
        let now = Utc::now();
        let summary = format!(
            "{}｜{}｜agreement={}｜tokens={}",
            task.goal, task.status.as_str(), task.agreement_score, task.tokens_used
        );
        self.store.write(|db| {
            let existing = db
                .memories
                .values()
                .find(|m| m.owner_id == user.id && m.scope == MemoryScope::Shared && m.key == key)
                .map(|m| m.id.clone());
            match existing {
                Some(id) => {
                    if let Some(m) = db.memories.get_mut(&id) {
                        m.content = summary;
                        m.version += 1;
                        m.updated_at = now;
                        m.written_by = "super-agent".into();
                    }
                }
                None => {
                    let id = uuid::Uuid::new_v4().to_string();
                    db.memories.insert(
                        id.clone(),
                        MemoryEntry {
                            id,
                            scope: MemoryScope::Shared,
                            owner_id: user.id.clone(),
                            key,
                            content: summary,
                            tags: vec![
                                task.task_type.as_str().to_string(),
                                "飞轮".to_string(),
                            ],
                            version: 1,
                            written_by: "super-agent".into(),
                            created_at: now,
                            updated_at: now,
                            access_count: 0,
                            confidence: 0.6,
                        },
                    );
                }
            }
        });
        self.metrics.inc("sa_flywheel_events_total");
    }
}

/// 默认步骤执行后端：确定性模拟。
///
/// 设计约束：只在 `StepRequest` 上读，不持有 `Store` / `Metrics` / `AuditSink`，
/// 因此可以在测试与灰度中自由替换，而治理口径保持一致。
impl StepRunner for SimRunner {
    fn backend(&self) -> &'static str {
        "sim"
    }

    fn run_step(&self, req: &StepRequest<'_>) -> StepOutcome {
        let task = req.task;
        let step = req.step;
        let spec = req.spec;
        let attempt = req.attempt;
        let goal = task.goal.to_lowercase();
        let is_main = step.name.starts_with("main:");
        let is_guard = spec.id == "guard-agent";

        // 慢任务注入（只影响 sim 后端，用于**真实演练**异步执行与取消链路）：
        // goal 里带 "slow-step" 时每步固定睡一会儿，让异步任务有可观测的持续期。
        // 默认 300ms，可用 SA_SIM_SLOW_MS 覆盖（夹在 0..=5000）。不带该关键字则一秒都不多睡，
        // 既有测试与冒烟的时序完全不受影响。
        if goal.contains("slow-step") {
            let ms = std::env::var("SA_SIM_SLOW_MS")
                .ok()
                .and_then(|v| v.parse::<u64>().ok())
                .unwrap_or(300)
                .min(5_000);
            std::thread::sleep(std::time::Duration::from_millis(ms));
        }

        // 合规红线：只读 agent 复核，一票否决
        if is_guard {
            if let Some((word, category)) = hit_redline(&goal, SOFT_REDLINES) {
                return StepOutcome::fatal(format!(
                    "合规红线命中「{word}」（{category}），已阻断，不生成产物"
                ))
                .counted("sa_compliance_blocks_total");
            }
        }

        // 失败注入（仅用于验收与演练，不改变治理链路）
        if goal.contains("always-fail") && !is_guard && !step.name.starts_with("synthesis") {
            return StepOutcome::fatal("致命失败：依赖不可用，重试无意义");
        }
        if goal.contains("force-fail") && is_main {
            return StepOutcome::retryable("工具调用超时（模拟持续失败）");
        }
        if goal.contains("retry-once") && is_main && attempt < 2 {
            return StepOutcome::retryable("上游返回 503（模拟瞬时失败）");
        }

        let tokens = jitter(&task.id, step.index, attempt, step.est_tokens);
        let outcome = StepOutcome::ok(tokens, render_output(task, step, spec, req.ctx));

        // 注意：沙箱记账**不在这里**。沙箱是治理层决策（PlanStep.sandbox），
        // 由 Executor 统一落账，任何执行后端都必须得到同一份审计与指标。
        // 早期版本把这段写在这里，导致换成 process 后端后 sa_sandbox_runs_total
        // 与 sandbox trace 整段消失 —— 换部件即改口径，见 docs/71。
        outcome
    }
}

/// 确定性抖动：同一任务同一步骤每次估算稳定，避免测试不稳定。
fn jitter(task_id: &str, step_index: u8, attempt: u32, est: u64) -> u64 {
    let mut h: u64 = 1469598103934665603;
    for b in task_id.as_bytes().iter().chain(&[step_index, attempt as u8]) {
        h ^= *b as u64;
        h = h.wrapping_mul(1099511628211);
    }
    let factor = 0.85 + ((h % 31) as f64) / 100.0; // 0.85 - 1.15
    (est as f64 * factor).round() as u64
}

fn render_output(
    task: &TaskRecord,
    step: &PlanStep,
    spec: &AgentSpec,
    ctx: &AssembledContext,
) -> String {
    match step.name.as_str() {
        "intent_and_boundary" => format!(
            "目标确认：「{}」；任务类型 {}；计划 {} 步；上下文装配 {} 条记忆（{} tokens{}）。边界判定：全部步骤在本系统能力范围内。",
            task.goal,
            task.task_type.as_str(),
            task.plan.len(),
            ctx.memory_refs.len(),
            ctx.used_tokens,
            if ctx.truncated { "，已裁剪" } else { "" }
        ),
        n if n.starts_with("main:") => format!(
            "{} 交付：围绕「{}」产出{}方案草案。包含 3 个关键决策点、可执行步骤与验收口径；依据 {} 条共享记忆、{} 份专业文档、技能集 [{}]。",
            spec.name,
            task.goal,
            spec.domain,
            ctx.memory_refs.len(),
            spec.doc_refs.len(),
            spec.skills.join(", ")
        ),
        "evidence:research-agent" => "证据清单：多源检索结果按 A（一手）/B（权威转载）/C（线索）分级；无来源支撑的主张统一标注「未确认」，不进入结论。".to_string(),
        "compliance_review" => "合规复核通过：未命中红线清单；权限链路逐级校验无越权；产物不含凭据与个人敏感信息。".to_string(),
        "synthesis" => format!(
            "汇总完成：已收敛 {} 个步骤的产物，输出最终结论与未确认项；整体一致性 {:.2}。",
            task.results.len(),
            task.agreement_score
        ),
        other => format!("{} 完成步骤 {}。", spec.name, other),
    }
}

fn synthesize(
    task: &TaskRecord,
    ctx: &AssembledContext,
    status: TaskStatus,
    abort: &Option<(AbortKind, String)>,
) -> String {
    let mut out = String::new();
    out.push_str(&format!(
        "任务「{}」执行{}（类型 {}，共 {} 步）。\n",
        task.goal,
        match status {
            TaskStatus::Succeeded => "成功",
            TaskStatus::PartiallySucceeded => "部分成功",
            TaskStatus::Failed => "失败",
            TaskStatus::AbortedBudget => "因预算耗尽中止",
            TaskStatus::Cancelled => "已取消",
            _ => "已结束",
        },
        task.task_type.as_str(),
        task.plan.len()
    ));
    if let Some((_, reason)) = abort {
        out.push_str(&format!("终止原因：{reason}\n"));
    }
    out.push_str("执行明细：\n");
    for r in &task.results {
        let mark = if r.success { "✓" } else { "✗" };
        let line = if r.success {
            format!("- {mark} [{}] {}（{}，{} tokens）", r.agent_id, r.name, r.model_profile.as_str(), r.tokens)
        } else {
            format!(
                "- {mark} [{}] {}（失败：{}）",
                r.agent_id,
                r.name,
                r.error.clone().unwrap_or_else(|| "未知错误".into())
            )
        };
        out.push_str(&line);
        out.push('\n');
    }
    out.push_str(&format!(
        "成本：{} tokens；一致性评分 {:.2}；引用记忆 {} 条{}。",
        task.tokens_used,
        task.agreement_score,
        ctx.memory_refs.len(),
        if ctx.truncated { "（上下文已裁剪）" } else { "" }
    ));
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::obs::metrics::Metrics;
    use crate::domain::task::{ModelProfile, PlanStep, TaskType};
    use crate::orchestration::context::{assemble, AssembledContext};
    use crate::security::rbac::{Permission, Role};

    fn user(role: Role) -> User {
        let now = Utc::now();
        User {
            id: "u1".into(),
            username: "u1".into(),
            username_lower: "u1".into(),
            email: "u1@example.com".into(),
            email_lower: "u1@example.com".into(),
            display_name: "U1".into(),
            password_hash: "x".into(),
            role,
            status: crate::domain::user::UserStatus::Active,
            locale: "zh-CN".into(),
            timezone: "Asia/Shanghai".into(),
            created_at: now,
            updated_at: now,
            last_login_at: None,
            failed_login_count: 0,
            locked_until: None,
        }
    }

    fn step(index: u8, name: &str, agent: &str, perm: Permission, est: u64) -> PlanStep {
        PlanStep {
            index,
            name: name.into(),
            agent_id: agent.into(),
            model_profile: ModelProfile::Standard,
            permission: perm,
            est_tokens: est,
            sandbox: false,
            depends_on: Vec::new(),
        }
    }

    fn task(goal: &str, steps: Vec<PlanStep>, budget: u64) -> TaskRecord {
        let now = Utc::now();
        TaskRecord {
            id: "t1".into(),
            owner_id: "u1".into(),
            goal: goal.into(),
            task_type: TaskType::Writing,
            requested_model: None,
            status: TaskStatus::Queued,
            plan: steps,
            results: vec![],
            trace: vec![],
            result_summary: String::new(),
            tokens_used: 0,
            budget_tokens: budget,
            created_at: now,
            updated_at: now,
            finished_at: None,
            memory_refs: vec![],
            agreement_score: 0.7,
        }
    }

    fn executor<'a>(store: &'a Store, metrics: &'a Metrics, audit: &'a dyn AuditSink) -> Executor<'a> {
        Executor {
            store,
            metrics,
            audit,
            retry_limit: 2,
            max_failed_steps: MAX_FAILED_STEPS,
            backoff_ms: 0,
            runner: None,
            cancel: None,
            concurrency: 1,
        }
    }

    /// 按名字汇总计数（忽略标签），用于断言指标。
    fn counter(metrics: &Metrics, name: &str) -> u64 {
        metrics
            .snapshot()
            .counters
            .iter()
            .filter(|((k, _), _)| k == name)
            .map(|(_, v)| *v)
            .sum()
    }

    fn empty_ctx() -> AssembledContext {
        assemble(&Store::in_memory(), &user(Role::Owner), "无关")
    }

    #[test]
    fn happy_path_succeeds_and_writes_flywheel() {
        let store = Store::in_memory();
        let metrics = Metrics::new();
        let audit = crate::obs::audit::MemoryAuditSink::new(100);
        let mut t = task(
            "写一份周报文档",
            vec![
                step(0, "intent_and_boundary", "super-agent", Permission::CrossAgent, 100),
                step(1, "main:doc-agent", "doc-agent", Permission::Write, 300),
                step(2, "compliance_review", "guard-agent", Permission::Read, 100),
                step(3, "synthesis", "super-agent", Permission::Write, 200),
            ],
            10_000,
        );
        let outcome = executor(&store, &metrics, &audit).run(
            &mut t,
            &user(Role::Owner),
            &empty_ctx(),
            "req-test",
        );
        assert_eq!(outcome.status, TaskStatus::Succeeded);
        assert_eq!(outcome.failed_steps, 0);
        assert_eq!(t.results.len(), 4);
        assert!(t.tokens_used > 0);
        assert!(t.result_summary.contains("执行成功"));
        // 飞轮：默认 SimRunner 的产出不算真实执行，**不得**沉淀（三方审查 H2）
        store.read(|db| {
            assert!(
                !db.memories.values().any(|m| m.key.starts_with("task:")),
                "模拟产出不能写进飞轮"
            );
        });
        assert_eq!(counter(&metrics, "sa_flywheel_events_total"), 0, "模拟产出不能计飞轮事件");
        assert_eq!(counter(&metrics, "sa_flywheel_skipped_total"), 1, "模拟产出要记一次跳过，便于观测");
    }

    /// 真实后端（backend != sim）仍然照常沉淀飞轮——跳过只针对模拟。
    #[test]
    fn real_backend_still_writes_flywheel() {
        let store = Store::in_memory();
        let metrics = Metrics::new();
        let audit = crate::obs::audit::MemoryAuditSink::new(100);
        let runner = FixedRunner::new();
        let mut t = task(
            "写一份周报文档",
            vec![step(1, "main:doc-agent", "doc-agent", Permission::Write, 300)],
            10_000,
        );
        let outcome = executor_with(&store, &metrics, &audit, &runner).run(
            &mut t,
            &user(Role::Owner),
            &empty_ctx(),
            "req-test-real",
        );
        assert_eq!(outcome.status, TaskStatus::Succeeded);
        store.read(|db| {
            assert!(
                db.memories
                    .values()
                    .any(|m| m.key.starts_with("task:") && m.scope == MemoryScope::Shared),
                "真实后端必须沉淀飞轮"
            );
        });
        assert_eq!(counter(&metrics, "sa_flywheel_events_total"), 1);
        assert_eq!(counter(&metrics, "sa_flywheel_skipped_total"), 0);
    }

    #[test]
    fn retry_once_then_succeeds() {
        let store = Store::in_memory();
        let metrics = Metrics::new();
        let audit = crate::obs::audit::MemoryAuditSink::new(100);
        let mut t = task(
            "retry-once 写文档",
            vec![step(0, "main:doc-agent", "doc-agent", Permission::Write, 300)],
            10_000,
        );
        let outcome = executor(&store, &metrics, &audit).run(
            &mut t,
            &user(Role::Owner),
            &empty_ctx(),
            "req-test",
        );
        assert_eq!(outcome.status, TaskStatus::Succeeded);
        assert_eq!(t.results[0].attempts, 2, "应重试一次后成功");
        assert!(t.trace.iter().any(|e| e.outcome == "retryable_error"));
    }

    #[test]
    fn persistent_failure_is_bounded() {
        let store = Store::in_memory();
        let metrics = Metrics::new();
        let audit = crate::obs::audit::MemoryAuditSink::new(100);
        let mut t = task(
            "force-fail 写文档",
            vec![
                step(0, "main:doc-agent", "doc-agent", Permission::Write, 300),
                step(1, "synthesis", "super-agent", Permission::Write, 200),
            ],
            10_000,
        );
        let outcome = executor(&store, &metrics, &audit).run(
            &mut t,
            &user(Role::Owner),
            &empty_ctx(),
            "req-test",
        );
        assert_eq!(outcome.status, TaskStatus::PartiallySucceeded);
        assert_eq!(t.results[0].attempts, 3, "首次 + 重试 2 次，不得无限重试");
    }

    #[test]
    fn compliance_redline_blocks() {
        let store = Store::in_memory();
        let metrics = Metrics::new();
        let audit = crate::obs::audit::MemoryAuditSink::new(100);
        let mut t = task(
            "帮我提取某人的身份证号",
            vec![
                step(0, "main:doc-agent", "doc-agent", Permission::Write, 300),
                step(1, "compliance_review", "guard-agent", Permission::Read, 100),
            ],
            10_000,
        );
        executor(&store, &metrics, &audit).run(&mut t, &user(Role::Owner), &empty_ctx(), "req");
        assert!(t
            .results
            .iter()
            .any(|r| r.agent_id == "guard-agent" && !r.success));
        assert!(t.result_summary.contains("合规红线"));
    }

    #[test]
    fn guard_agent_cannot_write() {
        let store = Store::in_memory();
        let metrics = Metrics::new();
        let audit = crate::obs::audit::MemoryAuditSink::new(100);
        // guard-agent 只读，用 Write 权限的步骤必须被拒
        let mut t = task(
            "写文档",
            vec![step(0, "bad:guard-write", "guard-agent", Permission::Write, 100)],
            10_000,
        );
        executor(&store, &metrics, &audit).run(&mut t, &user(Role::Owner), &empty_ctx(), "req");
        assert!(!t.results[0].success);
        assert!(t.results[0].error.as_ref().unwrap().contains("权限不足"));
        assert_eq!(audit.denied_count(), 1);
    }

    #[test]
    fn budget_exhaustion_aborts() {
        let store = Store::in_memory();
        let metrics = Metrics::new();
        let audit = crate::obs::audit::MemoryAuditSink::new(100);
        let mut t = task(
            "写一份很长的年度报告文档",
            vec![
                step(0, "main:doc-agent", "doc-agent", Permission::Write, 5_000),
                step(1, "synthesis", "super-agent", Permission::Write, 5_000),
            ],
            1_500,
        );
        let outcome = executor(&store, &metrics, &audit).run(
            &mut t,
            &user(Role::Owner),
            &empty_ctx(),
            "req-test",
        );
        assert_eq!(outcome.status, TaskStatus::AbortedBudget);
        assert!(outcome.reason.unwrap().contains("预算"));
    }

    #[test]
    fn member_cannot_write_system_memory_step() {
        let store = Store::in_memory();
        let metrics = Metrics::new();
        let audit = crate::obs::audit::MemoryAuditSink::new(100);
        let mut t = task(
            "写文档",
            vec![step(
                0,
                "system:memory",
                "super-agent",
                Permission::MemoryWriteSystem,
                100,
            )],
            10_000,
        );
        executor(&store, &metrics, &audit).run(
            &mut t,
            &user(Role::Member),
            &empty_ctx(),
            "req-test",
        );
        assert!(!t.results[0].success);
        assert!(metrics.render().contains("sa_authz_denied_total"));
    }

    /// 可控执行后端：用于证明 trait 抽象真的可替换，而不是把逻辑换个名字。
    struct FixedRunner {
        calls: std::sync::atomic::AtomicU32,
    }

    impl FixedRunner {
        fn new() -> Self {
            Self {
                calls: std::sync::atomic::AtomicU32::new(0),
            }
        }

        fn calls(&self) -> u32 {
            self.calls.load(std::sync::atomic::Ordering::SeqCst)
        }
    }

    impl StepRunner for FixedRunner {
        fn backend(&self) -> &'static str {
            "fixed-test"
        }

        fn run_step(&self, req: &StepRequest<'_>) -> StepOutcome {
            self.calls.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            StepOutcome::ok(7, format!("custom:{}:{}", req.spec.id, req.step.name))
                .counted("sa_custom_runner_steps_total")
        }
    }

    /// 带取消旗子的构造助手：旗子与本地的 store/metrics 同生命周期，
    /// 这样测试里能直接观察"取消前后"的行为差异。
    fn executor_with_flag<'a>(
        store: &'a Store,
        metrics: &'a Metrics,
        audit: &'a dyn AuditSink,
        runner: &'a dyn StepRunner,
        cancel: &'a std::sync::atomic::AtomicBool,
    ) -> Executor<'a> {
        Executor {
            store,
            metrics,
            audit,
            retry_limit: 2,
            max_failed_steps: MAX_FAILED_STEPS,
            backoff_ms: 0,
            runner: Some(runner),
            cancel: Some(cancel),
            concurrency: 1,
        }
    }

    fn executor_with<'a>(
        store: &'a Store,
        metrics: &'a Metrics,
        audit: &'a dyn AuditSink,
        runner: &'a dyn StepRunner,
    ) -> Executor<'a> {
        Executor {
            store,
            metrics,
            audit,
            retry_limit: 2,
            max_failed_steps: MAX_FAILED_STEPS,
            backoff_ms: 0,
            runner: Some(runner),
            cancel: None,
            concurrency: 1,
        }
    }

    /// 步骤执行后端替身：记录调用次数，并可让每步"耗时"（用于观察取消边界）。
    struct CountingRunner {
        calls: std::sync::atomic::AtomicU32,
        cancel_after: u32,
        flag: Option<std::sync::Arc<std::sync::atomic::AtomicBool>>,
    }

    impl StepRunner for CountingRunner {
        fn backend(&self) -> &'static str {
            "counting"
        }

        fn run_step(&self, req: &StepRequest<'_>) -> StepOutcome {
            let n = self
                .calls
                .fetch_add(1, std::sync::atomic::Ordering::SeqCst)
                + 1;
            // 模拟"用户在该步执行期间按下取消"：旗子在第 cancel_after 步里被立起
            if n == self.cancel_after {
                if let Some(f) = &self.flag {
                    f.store(true, std::sync::atomic::Ordering::SeqCst);
                }
            }
            StepOutcome::ok(req.step.est_tokens, format!("step-{}", req.step.index))
        }
    }

    fn three_step_task() -> TaskRecord {
        task(
            "逐步执行以观察取消边界",
            vec![
                step(0, "检索", "research-agent", Permission::Read, 100),
                step(1, "撰写", "code-agent", Permission::Write, 100),
                step(2, "复核", "doc-agent", Permission::Read, 100),
            ],
            10_000,
        )
    }

    /// 慢步注入必须真的改变耗时。
    ///
    /// 这条测试守的是一个**真实缺陷**：默认执行是零耗时的模拟器，异步任务提交后
    /// 20ms 内就跑到 succeeded，于是 `/tasks/{id}/cancel` 永远只会返回 409 ——
    /// 取消接口"存在但不可用"。加了 slow-step 注入后，异步链路才有可观测的持续期。
    #[test]
    fn slow_step_injection_makes_cancellation_observable() {
        let store = Store::in_memory();
        let metrics = Metrics::new();
        let audit = crate::obs::audit::MemoryAuditSink::new(100);
        let runner = SimRunner;
        let ex = executor_with(&store, &metrics, &audit, &runner);

        let mut fast = task(
            "普通任务",
            vec![step(0, "main:a", "code-agent", Permission::Read, 100)],
            10_000,
        );
        let t0 = std::time::Instant::now();
        let _ = ex.run(&mut fast, &user(Role::Owner), &empty_ctx(), "req-fast");
        let fast_ms = t0.elapsed().as_millis();

        let mut slow = task(
            "slow-step 任务",
            vec![step(0, "main:a", "code-agent", Permission::Read, 100)],
            10_000,
        );
        let t1 = std::time::Instant::now();
        let _ = ex.run(&mut slow, &user(Role::Owner), &empty_ctx(), "req-slow");
        let slow_ms = t1.elapsed().as_millis();

        assert!(slow_ms >= 250, "慢步任务至少睡 300ms，实际 {slow_ms}ms");
        assert!(
            slow_ms > fast_ms,
            "慢步任务必须显著慢于普通任务（{slow_ms}ms vs {fast_ms}ms）"
        );
    }

    /// 取消已立旗 → 一步都不跑，状态必须是 Cancelled（不是 Failed）。
    #[test]
    fn pre_cancelled_task_runs_zero_steps() {
        let store = Store::in_memory();
        let metrics = Metrics::new();
        let audit = crate::obs::audit::MemoryAuditSink::new(100);
        let flag = std::sync::atomic::AtomicBool::new(true);
        let mut t = three_step_task();
        let runner = CountingRunner {
            calls: std::sync::atomic::AtomicU32::new(0),
            cancel_after: u32::MAX,
            flag: None,
        };
        let ex = executor_with_flag(&store, &metrics, &audit, &runner, &flag);
        let out = ex.run(&mut t, &user(Role::Owner), &empty_ctx(), "req-cancel");

        assert_eq!(out.status, TaskStatus::Cancelled, "必须是 Cancelled 而非 Failed");
        assert_eq!(
            runner.calls.load(std::sync::atomic::Ordering::SeqCst),
            0,
            "已取消的任务不得发起任何步骤"
        );
        assert!(t.results.is_empty(), "没有执行的步骤不应产生结果");
        assert!(
            t.trace.iter().any(|e| e.phase == "cancel"),
            "取消必须留下可追查的轨迹"
        );
        assert!(out.reason.unwrap_or_default().contains("取消"));
        assert!(t.result_summary.contains("已取消"), "摘要措辞必须与状态一致");
    }

    /// 执行中途被取消 → 已完成的步骤保留，后续步骤不再执行。
    #[test]
    fn mid_run_cancel_stops_remaining_steps_but_keeps_finished_ones() {
        let store = Store::in_memory();
        let metrics = Metrics::new();
        let audit = crate::obs::audit::MemoryAuditSink::new(100);
        let flag = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let mut t = three_step_task();
        let runner = CountingRunner {
            calls: std::sync::atomic::AtomicU32::new(0),
            cancel_after: 1, // 第 1 步执行期间用户按下取消
            flag: Some(std::sync::Arc::clone(&flag)),
        };
        let ex = executor_with_flag(&store, &metrics, &audit, &runner, flag.as_ref());
        let out = ex.run(&mut t, &user(Role::Owner), &empty_ctx(), "req-cancel-mid");

        assert_eq!(out.status, TaskStatus::Cancelled);
        assert_eq!(
            runner.calls.load(std::sync::atomic::Ordering::SeqCst),
            1,
            "只能跑完已开始的那一步，第 2、3 步不得启动"
        );
        assert_eq!(t.results.len(), 1, "已完成的步骤必须保留，不能整条抹掉");
        assert!(t.results[0].success);
        assert_eq!(t.plan.len(), 3);
    }

    /// 取消不得被算作失败：指标口径分开，否则可用性会被"用户自己按的取消"拉低。
    #[test]
    fn cancellation_is_not_counted_as_failure() {
        let store = Store::in_memory();
        let metrics = Metrics::new();
        let audit = crate::obs::audit::MemoryAuditSink::new(100);
        let flag = std::sync::atomic::AtomicBool::new(true);
        let mut t = three_step_task();
        let runner = CountingRunner {
            calls: std::sync::atomic::AtomicU32::new(0),
            cancel_after: u32::MAX,
            flag: None,
        };
        let ex = executor_with_flag(&store, &metrics, &audit, &runner, &flag);
        ex.run(&mut t, &user(Role::Owner), &empty_ctx(), "req-cancel-metrics");

        let rendered = metrics.render();
        assert!(
            rendered.contains("sa_tasks_cancelled_total"),
            "取消次数必须单独可观测"
        );
        assert!(
            !rendered.contains("sa_task_failures_total"),
            "取消不得写进失败计数"
        );
    }

    /// 没有旗子（同步接口）时行为与改造前完全一致：三步全跑、成功收尾。
    #[test]
    fn without_cancel_flag_behaviour_is_unchanged() {
        let store = Store::in_memory();
        let metrics = Metrics::new();
        let audit = crate::obs::audit::MemoryAuditSink::new(100);
        let mut t = three_step_task();
        let runner = CountingRunner {
            calls: std::sync::atomic::AtomicU32::new(0),
            cancel_after: u32::MAX,
            flag: None,
        };
        let ex = executor_with(&store, &metrics, &audit, &runner);
        let out = ex.run(&mut t, &user(Role::Owner), &empty_ctx(), "req-sync");
        // 关键不是"成功"而是"没有因为取消改造而改变原有路径"：
        // 三步全部执行、没有取消痕迹、状态与改造前一致（复核环节可能只给部分成功）。
        assert!(
            !matches!(out.status, TaskStatus::Cancelled | TaskStatus::Failed),
            "未取消的任务不得变成 Cancelled/Failed，实际 {:?}",
            out.status
        );
        assert_eq!(runner.calls.load(std::sync::atomic::Ordering::SeqCst), 3);
        assert!(
            !t.trace.iter().any(|e| e.phase == "cancel"),
            "没取消就不该有取消轨迹"
        );
    }

    #[test]
    fn custom_step_runner_replaces_backend_and_keeps_governance() {
        let store = Store::in_memory();
        let metrics = Metrics::new();
        let audit = crate::obs::audit::MemoryAuditSink::new(100);
        let runner = FixedRunner::new();
        let mut t = task(
            "写一份周报文档",
            vec![
                step(0, "main:doc-agent", "doc-agent", Permission::Write, 300),
                step(1, "compliance_review", "guard-agent", Permission::Read, 100),
                step(2, "synthesis", "super-agent", Permission::Write, 200),
            ],
            10_000,
        );
        let outcome = executor_with(&store, &metrics, &audit, &runner).run(
            &mut t,
            &user(Role::Owner),
            &empty_ctx(),
            "req-runner",
        );

        assert_eq!(outcome.status, TaskStatus::Succeeded);
        assert_eq!(runner.calls(), 3, "三个步骤各调用一次自定义后端");
        assert!(
            t.results.iter().all(|r| r.output.starts_with("custom:")),
            "步骤产物必须来自自定义后端"
        );
        assert_eq!(t.tokens_used, 21, "预算口径仍由治理层统一累计（7 × 3）");
        assert!(t.trace.iter().any(|e| e.phase == "execute" && e.outcome == "ok"));
        assert!(metrics.render().contains("backend=\"fixed-test\""));
        assert!(metrics.render().contains("sa_custom_runner_steps_total"));
        store.read(|db| {
            assert!(db
                .memories
                .values()
                .any(|m| m.key.starts_with("task:") && m.scope == MemoryScope::Shared));
        });
    }

    #[test]
    fn authz_denial_precedes_step_runner() {
        let store = Store::in_memory();
        let metrics = Metrics::new();
        let audit = crate::obs::audit::MemoryAuditSink::new(100);
        let runner = FixedRunner::new();
        let mut t = task(
            "写一份周报文档",
            vec![step(
                0,
                "system:memory",
                "super-agent",
                Permission::MemoryWriteSystem,
                100,
            )],
            10_000,
        );
        let outcome = executor_with(&store, &metrics, &audit, &runner).run(
            &mut t,
            &user(Role::Member),
            &empty_ctx(),
            "req-runner-denied",
        );

        assert_eq!(outcome.status, TaskStatus::Failed);
        assert_eq!(runner.calls(), 0, "越权必须在治理层拦截，执行后端不得被调用");
        assert!(t.trace.iter().any(|e| e.phase == "authz-denied"));
        assert_eq!(audit.denied_count(), 1);
    }

    #[test]
    fn sandbox_accounting_is_governance_owned_not_backend_owned() {
        // 回归：沙箱记账曾经只写在 SimRunner 里，换成 process 后端后
        // sa_sandbox_runs_total 与 sandbox trace 会整段消失（换部件即改口径）。
        // 现在由治理层统一落账：任何后端都必须得到同一份审计与指标。
        let store = Store::in_memory();
        let metrics = Metrics::new();
        let audit = crate::obs::audit::MemoryAuditSink::new(100);
        let runner = FixedRunner::new();

        let mut sb = step(0, "main:doc-agent", "doc-agent", Permission::Write, 300);
        sb.sandbox = true;
        let mut t = task("写一份周报文档", vec![sb], 10_000);

        let outcome = executor_with(&store, &metrics, &audit, &runner).run(
            &mut t,
            &user(Role::Owner),
            &empty_ctx(),
            "req-sandbox-custom",
        );

        assert_eq!(outcome.status, TaskStatus::Succeeded);
        let sandbox_traces = t.trace.iter().filter(|e| e.phase == "sandbox").count();
        assert_eq!(
            sandbox_traces, 1,
            "自定义执行后端不记沙箱，治理层必须补记且只补一条，实际 {sandbox_traces}"
        );
        assert!(
            metrics.render().contains("sa_sandbox_runs_total 1")
                || metrics
                    .render()
                    .contains("sa_sandbox_runs_total"),
            "沙箱指标必须与后端无关地打点"
        );
    }

    #[test]
    fn sandbox_trace_not_double_counted_with_builtin_runner() {
        // 内置 SimRunner 与治理层不得重复记账（1 步沙箱 = 1 条 trace + 1 次计数）。
        let store = Store::in_memory();
        let metrics = Metrics::new();
        let audit = crate::obs::audit::MemoryAuditSink::new(100);

        let mut sb = step(0, "main:doc-agent", "doc-agent", Permission::Write, 300);
        sb.sandbox = true;
        let mut t = task("写一份周报文档", vec![sb], 10_000);

        let outcome = executor(&store, &metrics, &audit).run(
            &mut t,
            &user(Role::Owner),
            &empty_ctx(),
            "req-sandbox-sim",
        );
        assert_eq!(outcome.status, TaskStatus::Succeeded);
        assert_eq!(
            t.trace.iter().filter(|e| e.phase == "sandbox").count(),
            1,
            "内置后端与治理层重复记账会导致口径翻倍"
        );
    }

    // ===================== 多 agents 集群：依赖图调度 =====================

    /// 并行观测替身：记录"同时在跑"的峰值，并让每步睡一会儿放大重叠窗口。
    /// 只有真实的线程重叠才会让峰值升到 2 —— 靠 sleeps 造假是做不到的。
    #[derive(Default)]
    struct ConcurrencyProbe {
        current: std::sync::atomic::AtomicUsize,
        peak: std::sync::atomic::AtomicUsize,
    }

    impl StepRunner for ConcurrencyProbe {
        fn backend(&self) -> &'static str {
            "probe"
        }

        fn run_step(&self, req: &StepRequest<'_>) -> StepOutcome {
            let now = self
                .current
                .fetch_add(1, std::sync::atomic::Ordering::SeqCst)
                + 1;
            self.peak.fetch_max(now, std::sync::atomic::Ordering::SeqCst);
            std::thread::sleep(std::time::Duration::from_millis(80));
            self.current
                .fetch_sub(1, std::sync::atomic::Ordering::SeqCst);
            StepOutcome::ok(10, format!("done:{}", req.step.name))
        }
    }

    fn dep_step(
        index: u8,
        name: &str,
        agent: &str,
        perm: Permission,
        est: u64,
        deps: Vec<u8>,
    ) -> PlanStep {
        let mut s = step(index, name, agent, perm, est);
        s.depends_on = deps;
        s
    }

    #[test]
    fn plan_layers_fanout_lands_in_same_layer() {
        // 0 intent ─┬─> 1 main ─┬─> 3 review ─> 4 synthesis
        //           └─> 2 evidence ┘
        let plan = vec![
            dep_step(0, "intent_and_boundary", "super-agent", Permission::CrossAgent, 100, vec![]),
            dep_step(1, "main:doc-agent", "doc-agent", Permission::Write, 300, vec![0]),
            dep_step(2, "evidence:research-agent", "research-agent", Permission::Read, 300, vec![0]),
            dep_step(3, "compliance_review", "guard-agent", Permission::Read, 100, vec![1, 2]),
            dep_step(4, "synthesis", "super-agent", Permission::Write, 200, vec![3]),
        ];
        assert_eq!(
            plan_layers(&plan),
            vec![vec![0], vec![1, 2], vec![3], vec![4]],
            "同一父节点的两个子步骤必须落在同一层，否则并行形同虚设"
        );
    }

    #[test]
    fn plan_layers_independent_steps_share_one_layer() {
        let plan = vec![
            dep_step(0, "a", "doc-agent", Permission::Write, 100, vec![]),
            dep_step(1, "b", "doc-agent", Permission::Write, 100, vec![]),
        ];
        assert_eq!(plan_layers(&plan), vec![vec![0, 1]]);
    }

    #[test]
    fn validate_dependencies_rejects_self_and_backward() {
        let mut s = dep_step(1, "b", "doc-agent", Permission::Write, 100, vec![1]);
        assert!(s.validate_dependencies().is_some(), "依赖自身必须被拒");
        s.depends_on = vec![2];
        assert!(
            s.validate_dependencies().is_some(),
            "后向依赖会让执行器等一个永远不来的步骤，必须被拒"
        );
        s.depends_on = vec![0];
        assert!(s.validate_dependencies().is_none(), "前向依赖合法");
    }

    #[test]
    fn same_layer_steps_run_concurrently_and_merge_in_order() {
        let store = Store::in_memory();
        let metrics = Metrics::new();
        let audit = crate::obs::audit::MemoryAuditSink::new(100);
        let probe = ConcurrencyProbe::default();
        let mut ex = executor_with(&store, &metrics, &audit, &probe);
        ex.concurrency = 4;

        let mut t = task(
            "并行扇出",
            vec![
                dep_step(0, "evidence:research-agent", "research-agent", Permission::Read, 100, vec![]),
                dep_step(1, "main:doc-agent", "doc-agent", Permission::Write, 100, vec![]),
            ],
            10_000,
        );
        let outcome = ex.run(&mut t, &user(Role::Owner), &empty_ctx(), "req-parallel");
        assert_eq!(outcome.status, TaskStatus::Succeeded);
        assert_eq!(
            probe.peak.load(std::sync::atomic::Ordering::SeqCst),
            2,
            "同层两步必须真的同时在跑（峰值应到 2）"
        );
        assert_eq!(t.results.len(), 2);
        assert_eq!(t.results[0].index, 0, "结果必须按 index 升序归并");
        assert_eq!(t.results[1].index, 1);
        // 只取 `phase="execute"`：整步收尾的那条 `phase="step"/outcome="ok"`
        // 是后来补的（成功路径也要有 step 级记录），算进来就会得到 4 条。
        let ok_positions: Vec<usize> = t
            .trace
            .iter()
            .enumerate()
            .filter(|(_, e)| e.phase == "execute" && e.outcome == "ok")
            .map(|(i, _)| i)
            .collect();
        assert_eq!(ok_positions.len(), 2);
        assert!(
            ok_positions[0] < ok_positions[1],
            "trace 顺序必须与串行执行一致（可复现），不能由谁先跑完决定"
        );
        // 每个步骤都必须有一条 step 级收尾记录，且耗时字段有值——
        // 缺了它，前端只能拿相邻 trace 的 at 相减，并行时必然算出负数或串味。
        for idx in [0u8, 1u8] {
            let finals = t
                .trace
                .iter()
                .filter(|e| e.step_index == idx && e.phase == "step" && e.outcome == "ok")
                .count();
            assert_eq!(finals, 1, "步骤 {idx} 应恰好一条 step/ok 收尾记录");
            let cost = t
                .trace
                .iter()
                .find(|e| e.step_index == idx && e.phase == "step")
                .and_then(|e| e.duration_ms);
            assert!(cost.is_some(), "步骤 {idx} 的 step 记录必须带 duration_ms");
        }
    }

    #[test]
    fn concurrency_one_stays_serial() {
        let store = Store::in_memory();
        let metrics = Metrics::new();
        let audit = crate::obs::audit::MemoryAuditSink::new(100);
        let probe = ConcurrencyProbe::default();
        // 默认 concurrency = 1：并行能力默认关闭，既有验收口径不受影响
        let ex = executor_with(&store, &metrics, &audit, &probe);
        let mut t = task(
            "串行对照",
            vec![
                dep_step(0, "a", "doc-agent", Permission::Write, 100, vec![]),
                dep_step(1, "b", "research-agent", Permission::Read, 100, vec![]),
            ],
            10_000,
        );
        let outcome = ex.run(&mut t, &user(Role::Owner), &empty_ctx(), "req-serial");
        assert_eq!(outcome.status, TaskStatus::Succeeded);
        assert_eq!(
            probe.peak.load(std::sync::atomic::Ordering::SeqCst),
            1,
            "concurrency=1 时必须严格串行"
        );
    }

    #[test]
    fn dependency_failure_skips_downstream() {
        // goal 含 force-fail → SimRunner 必失败；下游依赖它，必须停手。
        let store = Store::in_memory();
        let metrics = Metrics::new();
        let audit = crate::obs::audit::MemoryAuditSink::new(100);
        let mut t = task(
            "force-fail 依赖传导",
            vec![
                dep_step(0, "main:doc-agent", "doc-agent", Permission::Write, 300, vec![]),
                dep_step(1, "synthesis", "super-agent", Permission::Write, 200, vec![0]),
            ],
            10_000,
        );
        let outcome = executor(&store, &metrics, &audit).run(
            &mut t,
            &user(Role::Owner),
            &empty_ctx(),
            "req-dep-fail",
        );
        assert_ne!(outcome.status, TaskStatus::Succeeded);
        assert_eq!(t.results.len(), 2, "跳过的步骤也要留痕，不能凭空消失");
        assert!(!t.results[0].success);
        assert!(
            !t.results[1].success,
            "下游不得拿着半截产物继续产出"
        );
        assert_eq!(t.results[1].attempts, 0, "被跳过的步骤不应发起执行");
        assert!(
            t.trace.iter().any(|e| e.phase == "dependency-failed"),
            "必须写明跳过原因是前置失败"
        );
    }

    // ===================== SA-001：合规红线覆盖 =====================

    /// 产物"带毒"的 runner：目标干净、答卷不干净，模拟模型自行加戏或脚本乱来。
    struct DirtyProductRunner;

    impl StepRunner for DirtyProductRunner {
        fn backend(&self) -> &'static str {
            "dirty-product-test"
        }

        fn run_step(&self, _req: &StepRequest<'_>) -> StepOutcome {
            StepOutcome::ok(5, "步骤一：整理资料；顺手把用户表拖库导出".to_string())
        }
    }

    /// 非 guard agent 的目标命中硬红线 → runner 一次都不许被调用。
    #[test]
    fn hard_redline_blocks_before_runner_runs() {
        let store = Store::in_memory();
        let metrics = Metrics::new();
        let audit = crate::obs::audit::MemoryAuditSink::new(100);
        let runner = FixedRunner::new();
        let ex = executor_with(&store, &metrics, &audit, &runner);
        // 故意只排 code-agent：旧实现只拦 guard，这种排法会一路跑到底。
        let mut t = task(
            "帮我窃取同行的客户名单",
            vec![step(0, "main:a", "code-agent", Permission::Read, 100)],
            10_000,
        );
        let out = ex.run(&mut t, &user(Role::Owner), &empty_ctx(), "req-rl-entry");

        assert_eq!(out.status, TaskStatus::Failed, "命中硬红线必须整任务失败");
        assert_eq!(runner.calls(), 0, "红线预检必须在任何 runner 调用之前生效");
        assert!(t.results.is_empty(), "被拦下的任务不允许留下任何产物");
        assert!(
            out.reason.as_deref().unwrap_or_default().contains("合规红线"),
            "失败原因要能看出是红线拦的：{:?}",
            out.reason
        );
        assert!(
            t.trace
                .iter()
                .any(|x| x.phase == "compliance" && x.outcome == "blocked_at_entry"),
            "必须留下 compliance 轨迹，否则运维看不到“拦了、为什么拦”"
        );
    }

    /// 合法需求不许被安全词表误杀：中性词不能当红线。
    #[test]
    fn neutral_goal_is_not_blocked_by_redlines() {
        let store = Store::in_memory();
        let metrics = Metrics::new();
        let audit = crate::obs::audit::MemoryAuditSink::new(100);
        let runner = FixedRunner::new();
        let ex = executor_with(&store, &metrics, &audit, &runner);
        let mut t = task(
            "写一个密码管理器，并说明身份证号脱敏应该怎么做",
            vec![step(0, "main:a", "code-agent", Permission::Read, 100)],
            10_000,
        );
        let out = ex.run(&mut t, &user(Role::Owner), &empty_ctx(), "req-rl-neutral");

        assert!(
            runner.calls() > 0,
            "合法需求必须真的进执行，而不是被安全词表打回：{:?}",
            out.reason
        );
        assert_ne!(out.status, TaskStatus::Failed, "合法需求不能被判失败");
    }

    /// 目标干净但产物带硬红线 → 该步 fatal，产物丢弃、不落库。
    #[test]
    fn redline_product_turns_step_fatal_and_drops_output() {
        let store = Store::in_memory();
        let metrics = Metrics::new();
        let audit = crate::obs::audit::MemoryAuditSink::new(100);
        let runner = DirtyProductRunner;
        let ex = executor_with(&store, &metrics, &audit, &runner);
        let mut t = task(
            "整理一份公开资料摘要",
            vec![step(0, "main:a", "code-agent", Permission::Read, 100)],
            10_000,
        );
        let out = ex.run(&mut t, &user(Role::Owner), &empty_ctx(), "req-rl-product");

        assert_ne!(
            out.status,
            TaskStatus::Succeeded,
            "产物带红线不能算成功：{:?}",
            out.reason
        );
        // 失败步骤仍会留一条 results 记录（带 error，便于前端展示进度），
        // 但里面**不能有产物正文**——被拦下的东西一个字都不许落库。
        assert!(
            t.results.iter().all(|r| !r.output.contains("拖库")),
            "产物命中红线后不允许把产物内容落库：{:?}",
            t.results
        );
        assert!(
            t.trace
                .iter()
                .any(|x| x.phase == "compliance" && x.outcome == "blocked"),
            "产物被拦必须留 compliance 轨迹"
        );
    }

}
