//! 任务类型、模型档位、编排计划与执行轨迹的数据结构。

use crate::security::rbac::Permission;
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TaskType {
    Coding,
    Writing,
    Research,
    Planning,
    Ideation,
    Collaboration,
}

impl TaskType {
    pub fn as_str(&self) -> &'static str {
        match self {
            TaskType::Coding => "coding",
            TaskType::Writing => "writing",
            TaskType::Research => "research",
            TaskType::Planning => "planning",
            TaskType::Ideation => "ideation",
            TaskType::Collaboration => "collaboration",
        }
    }

    pub const ALL: [&'static str; 6] = [
        "coding",
        "writing",
        "research",
        "planning",
        "ideation",
        "collaboration",
    ];

    pub fn parse(s: &str) -> Option<TaskType> {
        match s.to_ascii_lowercase().as_str() {
            "coding" | "code" => Some(TaskType::Coding),
            "writing" | "doc" => Some(TaskType::Writing),
            "research" | "search" => Some(TaskType::Research),
            "planning" | "plan" => Some(TaskType::Planning),
            "ideation" | "idea" => Some(TaskType::Ideation),
            "collaboration" | "collab" => Some(TaskType::Collaboration),
            _ => None,
        }
    }
}

/// 模型档位：成本与能力的显式权衡，避免"一律用最贵模型"。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ModelProfile {
    Fast,
    Standard,
    Deep,
}

impl ModelProfile {
    pub fn as_str(&self) -> &'static str {
        match self {
            ModelProfile::Fast => "fast",
            ModelProfile::Standard => "standard",
            ModelProfile::Deep => "deep",
        }
    }

    pub fn cost_multiplier(&self) -> f64 {
        match self {
            ModelProfile::Fast => 1.0,
            ModelProfile::Standard => 3.0,
            ModelProfile::Deep => 9.0,
        }
    }

    /// 预算不足时逐级降档；Fast 已是地板。
    pub fn downgrade(&self) -> Option<ModelProfile> {
        match self {
            ModelProfile::Deep => Some(ModelProfile::Standard),
            ModelProfile::Standard => Some(ModelProfile::Fast),
            ModelProfile::Fast => None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TaskStatus {
    Queued,
    Running,
    Succeeded,
    Failed,
    PartiallySucceeded,
    AbortedBudget,
    Cancelled,
}

impl TaskStatus {
    pub fn as_str(&self) -> &'static str {
        match self {
            TaskStatus::Queued => "queued",
            TaskStatus::Running => "running",
            TaskStatus::Succeeded => "succeeded",
            TaskStatus::Failed => "failed",
            TaskStatus::PartiallySucceeded => "partially_succeeded",
            TaskStatus::AbortedBudget => "aborted_budget",
            TaskStatus::Cancelled => "cancelled",
        }
    }

    pub fn is_terminal(&self) -> bool {
        !matches!(self, TaskStatus::Queued | TaskStatus::Running)
    }

    /// 全部状态的字符串形式。错误提示与前端筛选项都从这里取，
    /// 避免"文案手抄枚举"导致的漂移（例如把 aborted_quota 写成不存在的状态）。
    pub const ALL: [&'static str; 7] = [
        "queued",
        "running",
        "succeeded",
        "failed",
        "partially_succeeded",
        "aborted_budget",
        "cancelled",
    ];

    pub fn parse(s: &str) -> Option<TaskStatus> {
        match s.trim().to_ascii_lowercase().as_str() {
            "queued" => Some(TaskStatus::Queued),
            "running" => Some(TaskStatus::Running),
            "succeeded" | "success" => Some(TaskStatus::Succeeded),
            "failed" => Some(TaskStatus::Failed),
            "partially_succeeded" | "partial" => Some(TaskStatus::PartiallySucceeded),
            "aborted_budget" => Some(TaskStatus::AbortedBudget),
            "cancelled" | "canceled" => Some(TaskStatus::Cancelled),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PlanStep {
    pub index: u8,
    pub name: String,
    pub agent_id: String,
    pub model_profile: ModelProfile,
    /// 该步骤需要的权限，执行前逐项校验（权力分配的落点）
    pub permission: Permission,
    pub est_tokens: u64,
    pub sandbox: bool,
    /// 依赖的前置步骤 `index` 列表（空 = 无依赖，可与同层其它步骤**并行**执行）。
    ///
    /// 这是"多 agents 集群"的编排契约：执行器按 `depends_on` 把计划切成**拓扑层**，
    /// 同层步骤并发跑（并发上限见 `Config::step_concurrency`），层与层之间是同步栅栏。
    /// 语义有三条硬约束，写在这里而不是只写在 executor 里，是因为它们属于领域规则：
    /// 1. **依赖失败即跳过**：任一前置步骤失败/被拒，本步骤记为
    ///    `dependency_failed` 且不执行——不能拿半截产物继续往下算；
    /// 2. **依赖必须指向更早的 index**：只允许前向依赖，天然无环，不需要运行期拓扑排序兜底；
    /// 3. **空依赖 = 与同层步骤并行**：老计划（不填该字段）反序列化后为空，
    ///    仍能正常执行，只是退化为"每层一步"的顺序语义。
    #[serde(default)]
    pub depends_on: Vec<u8>,
}

impl PlanStep {
    /// 依赖校验：返回错误原因，`None` 表示合法。
    ///
    /// 「只允许前向依赖」这条约束值得单独立一个函数并被测试钉住：
    /// 一旦允许回指（或自指），并行执行就可能出现"等一个永远不会跑的步骤"，
    /// 表现为任务**卡死**而不是失败——这类问题比报错难查一个数量级。
    pub fn validate_dependencies(&self) -> Option<String> {
        if self.depends_on.iter().any(|d| *d == self.index) {
            return Some(format!("第 {} 步依赖自身", self.index));
        }
        if let Some(bad) = self.depends_on.iter().find(|d| **d > self.index) {
            return Some(format!(
                "第 {} 步依赖了更靠后的第 {} 步（只允许前向依赖）",
                self.index, bad
            ));
        }
        None
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TraceEntry {
    /// 该条 trace 所描述事件的时刻：`outcome=started` 是**开始**时刻，
    /// `ok/failed/retryable_error` 等是**结束**时刻。
    ///
    /// 早期版本的 `at` 一律写"写日志的此刻"，于是步骤 trace 全是完成时刻，
    /// 前端拿它当起点画时间轴会把并行步骤画成串行。现在语义写死在这里，
    /// 要"这一步花了多久"请读 `duration_ms`，不要再拿相邻 `at` 相减。
    pub at: DateTime<Utc>,
    pub step_index: u8,
    pub agent_id: String,
    pub phase: String,
    pub outcome: String,
    pub detail: String,
    pub attempt: u32,
    pub tokens: u64,
    /// 该事件的耗时（毫秒）。仅对"有明确起止"的事件有值：
    /// `execute` 记**单次尝试**耗时，`step` 记**含重试与退避**的整步耗时；
    /// `started`、`budget`、`cancel`、`authz-denied` 这类瞬时事件为 `null`。
    ///
    /// 为什么不用相邻 `at` 相减：并行执行时两条 trace 的时间窗会重叠，
    /// 相减出来的"耗时"会给出负数或把等待别人的时间算进自己头上。
    pub duration_ms: Option<u64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StepResult {
    pub index: u8,
    pub agent_id: String,
    pub name: String,
    pub success: bool,
    pub model_profile: ModelProfile,
    pub tokens: u64,
    pub attempts: u32,
    pub output: String,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TaskRecord {
    pub id: String,
    pub owner_id: String,
    pub goal: String,
    pub task_type: TaskType,
    pub requested_model: Option<ModelProfile>,
    pub status: TaskStatus,
    pub plan: Vec<PlanStep>,
    pub results: Vec<StepResult>,
    pub trace: Vec<TraceEntry>,
    pub result_summary: String,
    pub tokens_used: u64,
    pub budget_tokens: u64,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
    pub finished_at: Option<DateTime<Utc>>,
    pub memory_refs: Vec<String>,
    pub agreement_score: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct TaskView {
    pub id: String,
    pub goal: String,
    pub task_type: TaskType,
    pub status: TaskStatus,
    pub plan: Vec<PlanStep>,
    pub results: Vec<StepResult>,
    pub result_summary: String,
    pub tokens_used: u64,
    pub budget_tokens: u64,
    /// 实际生效的模型档位（显式指定优先，否则取计划首步），供前端展示「按任务模型调度」结果
    pub model_profile: ModelProfile,
    pub agreement_score: f64,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
    pub finished_at: Option<DateTime<Utc>>,
}

impl From<&TaskRecord> for TaskView {
    fn from(t: &TaskRecord) -> Self {
        TaskView {
            id: t.id.clone(),
            goal: t.goal.clone(),
            task_type: t.task_type,
            status: t.status,
            plan: t.plan.clone(),
            results: t.results.clone(),
            result_summary: t.result_summary.clone(),
            tokens_used: t.tokens_used,
            budget_tokens: t.budget_tokens,
            model_profile: t
                .requested_model
                .or_else(|| t.plan.first().map(|s| s.model_profile))
                .unwrap_or(ModelProfile::Standard),
            agreement_score: t.agreement_score,
            created_at: t.created_at,
            updated_at: t.updated_at,
            finished_at: t.finished_at,
        }
    }
}
