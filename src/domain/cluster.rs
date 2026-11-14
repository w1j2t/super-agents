//! 集群视图（领域层）：这个节点"现在扛着什么"的只读快照。
//!
//! # 为什么单节点也要有 `nodes: []` 数组
//!
//! 当前部署是"一进程全包"（HTTP + 编排 + 执行 + 存储同在一个进程），节点数恒为 1。
//! 但**协议形状要提前定死**：前端按 `nodes[]` 渲染节点卡片，将来接入第 2、第 3 个节点
//! 时只需往数组里多填元素，不必改前端、不必改 schema、不必发版本。
//! 反过来若现在偷懒返回扁平字段，真正的多节点那天就得做一次破坏性协议变更——
//! 这类"当时省一行、将来推翻一次契约"的债，比多写十行注释贵得多。
//!
//! 因此这里刻意区分两件事：
//! - `nodes`：**每节点**的事实（容量、占用、在线订阅数），条数随集群规模变；
//! - `totals`：**整个集群**的汇总，便于前端不遍历也能直接显示"集群在跑几个任务"。
//!
//! # 多节点从哪来
//!
//! 节点清单 = **本地实时快照**（1 条，永远 `source="local"`、`stale=false`）
//! + **共享目录里的 peer 心跳**（N 条，`source="peer"`，超过 TTL 未更新的标 `stale=true`）。
//!
//! 这里有一个必须分清的口径：`tasks` 是**各节点自己的任务分布**，
//! 它要求"每个节点一个独立 data_dir"。若两个节点指向同一个 data_dir，
//! 两边读到的就是同一张任务表，汇总会把任务数算两遍。
//! 这不是能靠代码兜住的（分不清"重复"和"真的各有一份"），
//! 所以写进约定：**共享的只有 `SA_CLUSTER_DIR`，数据目录必须各用各的**。
//!
//! 离线节点仍会出现在 `nodes` 里（`stale=true`），但**不参与** `totals` 求和：
//! 它最后一次心跳里的 `async_running` 是过去的值，混进汇总会让"集群此刻在跑几个任务"
//! 越等越大。要看"掉了几台"，读 `totals.offline_nodes`。
//!
//! # 为什么不把可插拔部件的 `active` 也塞进来
//!
//! `/api/v1/observability/components` 已经是槽位的唯一事实来源。
//! 集群视图再抄一份，只会多出一条需要同步维护的口径——将来换件时两处说法不一致，
//! 排查的人要先猜哪份是对的。这里只报"数量"不做冗余，具体装了什么去 components 查。
//!
//! # `async_capacity` 的含义
//!
//! 它是 `SA_ASYNC_MAX_RUNNING` 的当前生效值（默认 8），即**同时执行**的异步任务上限；
//! 超限的提交会被直接拒（429）而不是排队。所以
//! `async_running / async_capacity` 就是"这个节点还剩几个人能进来"，
//! 也是将来做跨节点分派时唯一需要的准入量。

use crate::domain::agent::AGENTS;
use crate::domain::task::{TaskRecord, TaskStatus};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

/// 单节点角色。当前只有一种：一个进程同时承担 API、编排、执行、存储。
///
/// 将来拆成"调度节点 + 执行节点"时，在这里加 `dispatcher` / `worker` 两个值，
/// 并把 [`ClusterNode::role`] 改成按实例实际能力填写——字段形状不变。
pub const ROLE_ALL_IN_ONE: &str = "all-in-one";

/// 节点数据来源：本进程自己采集的实时快照。
pub const SOURCE_LOCAL: &str = "local";
/// 节点数据来源：从共享目录读到的心跳文件。
pub const SOURCE_PEER: &str = "peer";

/// 一个集群节点的事实快照。
#[derive(Debug, Serialize)]
pub struct ClusterNode {
    /// 节点标识：`SA_NODE_ID` 优先，缺省为 `<主机名>-<pid>`。
    /// 带 pid 是有意的：同一台机器上并行跑两个进程（调试 + 正式）时，
    /// 若只报主机名，两个节点在视图里会重合，分不清谁在扛任务。
    pub node_id: String,
    pub role: &'static str,
    /// 进程启动时刻（UTC）。与 `uptime_seconds` 并存不是冗余：
    /// 看板要显示"从几点开始跑的"，而算时间差容易踩时区/时钟漂移，直接给两个真值最省事。
    pub started_at: DateTime<Utc>,
    pub uptime_seconds: u64,
    /// 单任务内可并行的步骤数上限（`SA_STEP_CONCURRENCY`，默认 1 = 全串行）。
    /// 这是"一个 agent 集群任务能吃几核"的直接答案。
    pub step_concurrency: usize,
    /// 正在执行的异步任务数。
    pub async_running: usize,
    /// 异步任务并发上限（`SA_ASYNC_MAX_RUNNING`）。
    pub async_capacity: usize,
    /// 当前盯着这个节点的 SSE 订阅者数（一个开着的任务详情页算 1 个）。
    pub sse_subscribers: usize,
    /// 因订阅者缓冲满而丢弃的事件累计数（正常空转时为 0）。
    pub events_dropped: u64,
    /// 本节点可调度的 agent 数量。
    pub agents_total: usize,
    /// 本节点自己的任务分布。
    ///
    /// 与 `totals.tasks` 的区别是"一个人的账"和"全家人的账"：
    /// 前端要画"哪台机器在扛活"必须看这个；只看汇总会平均掉负载倾斜。
    pub tasks: ClusterTasks,
    /// `local` = 本进程实时采集；`peer` = 来自共享目录的心跳文件。
    /// 前端用它决定要不要在卡片上打"外部节点"标记。
    pub source: &'static str,
    /// 这份数据的采集时刻（UTC）。`peer` 节点上它就是"最后一次心跳时间"，
    /// 看板据此显示"3 秒前"而不是让人误以为是刚刚问到的。
    pub updated_at: DateTime<Utc>,
    /// 心跳已过期（超过 `SA_CLUSTER_TTL_SECS` 未更新）。
    /// 本地节点恒为 false——自己不需要靠心跳证明自己活着。
    pub stale: bool,
}

/// 任务按状态的分布。字段刻意逐状态列全，而不是给一个 `map<String, u64>`：
/// 前者在 OpenAPI / TS 里是强类型（前端写错字段名当场报错），
/// 后者要等到运行期才会发现少了一个键。
#[derive(Debug, Default, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ClusterTasks {
    pub total: u64,
    pub queued: u64,
    pub running: u64,
    pub succeeded: u64,
    /// 部分成功（有步骤失败但未触发整体失败）。独立计列的理由：
    /// 它既不是成功也不是失败，混进任何一边都会让"失败率"这个指标失真。
    pub partially_succeeded: u64,
    pub failed: u64,
    pub cancelled: u64,
    /// 预算耗尽而中止的任务数（独立计列：它不是失败，也不是成功）
    pub aborted_budget: u64,
}

impl ClusterTasks {
    /// 从任务快照统计。传入的是**全部可见任务**，调用方负责取数口径。
    ///
    /// 为什么用 `match` 穷举而不是 `if/else` 链：`TaskStatus` 将来加状态时，
    /// 这里会直接编译失败，逼着人补统计。漏统计一个状态的表现是
    /// "总数对不上明细"，那种 bug 通常要等到有人手算才发现。
    pub fn count<'a, I: IntoIterator<Item = &'a TaskRecord>>(tasks: I) -> Self {
        let mut out = Self::default();
        for t in tasks {
            out.total += 1;
            match t.status {
                TaskStatus::Queued => out.queued += 1,
                TaskStatus::Running => out.running += 1,
                TaskStatus::Succeeded => out.succeeded += 1,
                TaskStatus::PartiallySucceeded => out.partially_succeeded += 1,
                TaskStatus::Failed => out.failed += 1,
                TaskStatus::Cancelled => out.cancelled += 1,
                TaskStatus::AbortedBudget => out.aborted_budget += 1,
            }
        }
        out
    }
}

impl ClusterTasks {
    /// 逐字段累加（跨节点汇总用）。
    ///
    /// 写成一个方法而不是让调用方手写 8 行 `+=`：加状态时只改这里，
    /// 调用点不会漏掉新字段——漏字段的表现是"汇总比明细少，且少得没规律"。
    pub fn add(&mut self, other: &ClusterTasks) {
        self.total += other.total;
        self.queued += other.queued;
        self.running += other.running;
        self.succeeded += other.succeeded;
        self.partially_succeeded += other.partially_succeeded;
        self.failed += other.failed;
        self.cancelled += other.cancelled;
        self.aborted_budget += other.aborted_budget;
    }
}

/// 集群级汇总：不遍历 `nodes` 也能直接显示的总账。
#[derive(Debug, Serialize)]
pub struct ClusterTotals {
    /// 集群里的节点总数（含已离线的）。
    pub nodes: usize,
    /// 其中心跳过期（`stale`）的节点数。
    ///
    /// 单独给一个数，是为了让"集群缩水了"这件事一眼可见：
    /// 只报 `nodes` 的话，2 台里死了 1 台仍然是 2，看板上看不出异常。
    pub offline_nodes: usize,
    /// 全集群 agent 数（当前每节点相同；多节点后由各节点自报，此处求和）
    pub agents_total: usize,
    pub async_running: usize,
    pub async_capacity: usize,
    pub sse_subscribers: usize,
    pub tasks: ClusterTasks,
}

/// `GET /api/v1/cluster` 的响应体。
#[derive(Debug, Serialize)]
pub struct ClusterView {
    pub nodes: Vec<ClusterNode>,
    pub totals: ClusterTotals,
}

impl ClusterView {
    /// 由各节点快照装配汇总。
    ///
    /// 汇总口径：**只算在线节点**（`stale=false`）。离线节点的负载是它最后一次心跳时的
    /// 陈旧值，算进去会让"集群正在跑几个任务"永远不下降——那种数字比没有更糟。
    pub fn new(nodes: Vec<ClusterNode>) -> Self {
        let offline_nodes = nodes.iter().filter(|n| n.stale).count();
        let live = || nodes.iter().filter(|n| !n.stale);
        let mut tasks = ClusterTasks::default();
        for n in live() {
            tasks.add(&n.tasks);
        }
        let totals = ClusterTotals {
            nodes: nodes.len(),
            offline_nodes,
            agents_total: live().map(|n| n.agents_total).sum(),
            async_running: live().map(|n| n.async_running).sum(),
            async_capacity: live().map(|n| n.async_capacity).sum(),
            sse_subscribers: live().map(|n| n.sse_subscribers).sum(),
            tasks,
        };
        Self { nodes, totals }
    }
}

/// 心跳文件的落盘形状（内部类型，不进 API schema）。
///
/// 为什么不直接把 [`ClusterNode`] 序列化：它带 `&'static str`（`role` / `source`），
/// 那是"编译期就固定的常量"，天然无法反序列化。硬改成 `String` 只为了让内部文件能读，
/// 会把 API 类型的语义一起稀释掉。所以内部快照独立一个全 owned 的结构，
/// 边界只有 [`NodeSnapshot::to_node`] 一处转换。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NodeSnapshot {
    pub node_id: String,
    pub role: String,
    pub started_at: DateTime<Utc>,
    pub uptime_seconds: u64,
    pub step_concurrency: usize,
    pub async_running: usize,
    pub async_capacity: usize,
    pub sse_subscribers: usize,
    pub events_dropped: u64,
    pub agents_total: usize,
    pub tasks: ClusterTasks,
    /// 写这份心跳的时刻。TTL 判定、看板上的"几秒前"全看它。
    pub updated_at: DateTime<Utc>,
}

impl NodeSnapshot {
    /// 落成本地视图（`source="local"`，永不 stale）。
    pub fn into_local_node(self) -> ClusterNode {
        ClusterNode {
            node_id: self.node_id,
            role: ROLE_ALL_IN_ONE,
            started_at: self.started_at,
            uptime_seconds: self.uptime_seconds,
            step_concurrency: self.step_concurrency,
            async_running: self.async_running,
            async_capacity: self.async_capacity,
            sse_subscribers: self.sse_subscribers,
            events_dropped: self.events_dropped,
            agents_total: self.agents_total,
            tasks: self.tasks,
            source: SOURCE_LOCAL,
            updated_at: self.updated_at,
            stale: false,
        }
    }

    /// 落成 peer 视图。`stale` 由调用方按 TTL 判定后传入（这里不读时钟，
    /// 保证同一批 peer 用同一个"现在"，不会因为循环里时间流逝而前后不一致）。
    pub fn into_peer_node(self, stale: bool) -> ClusterNode {
        ClusterNode {
            node_id: self.node_id,
            role: ROLE_ALL_IN_ONE,
            started_at: self.started_at,
            uptime_seconds: self.uptime_seconds,
            step_concurrency: self.step_concurrency,
            async_running: self.async_running,
            async_capacity: self.async_capacity,
            sse_subscribers: self.sse_subscribers,
            events_dropped: self.events_dropped,
            agents_total: self.agents_total,
            tasks: self.tasks,
            source: SOURCE_PEER,
            updated_at: self.updated_at,
            stale,
        }
    }
}

/// 本节点可调度的 agent 数量。
///
/// 单独抽成函数而不是让 handler 直接读 `AGENTS.len()`：将来"哪台机器装了哪几个 agent"
/// 变成真事（不同节点上 worker 脚本不一样）时，只需改这一个函数，
/// handler 与 schema 都不动。
pub fn local_agents_total() -> usize {
    AGENTS.len()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::task::{ModelProfile, TaskType};

    fn task(status: TaskStatus) -> TaskRecord {
        let now = Utc::now();
        TaskRecord {
            id: format!("t-{status:?}"),
            owner_id: "u1".into(),
            goal: "集群统计校验".into(),
            task_type: TaskType::Coding,
            requested_model: Some(ModelProfile::Standard),
            status,
            plan: Vec::new(),
            results: Vec::new(),
            trace: Vec::new(),
            result_summary: String::new(),
            tokens_used: 0,
            budget_tokens: 1000,
            created_at: now,
            updated_at: now,
            finished_at: None,
            memory_refs: Vec::new(),
            agreement_score: 0.0,
        }
    }

    fn node() -> ClusterNode {
        ClusterNode {
            node_id: "n1".into(),
            role: ROLE_ALL_IN_ONE,
            started_at: Utc::now(),
            uptime_seconds: 42,
            step_concurrency: 2,
            async_running: 3,
            async_capacity: 8,
            sse_subscribers: 1,
            events_dropped: 0,
            agents_total: local_agents_total(),
            tasks: ClusterTasks::count(std::iter::empty()),
            source: SOURCE_LOCAL,
            updated_at: Utc::now(),
            stale: false,
        }
    }

    fn snapshot(node_id: &str, running: u64) -> NodeSnapshot {
        let mut tasks = ClusterTasks::default();
        tasks.total = running;
        tasks.running = running;
        NodeSnapshot {
            node_id: node_id.into(),
            role: ROLE_ALL_IN_ONE.into(),
            started_at: Utc::now(),
            uptime_seconds: 10,
            step_concurrency: 1,
            async_running: running as usize,
            async_capacity: 8,
            sse_subscribers: 0,
            events_dropped: 0,
            agents_total: local_agents_total(),
            tasks,
            updated_at: Utc::now(),
        }
    }

    #[test]
    fn counts_every_status_and_total_adds_up() {
        let tasks = vec![
            task(TaskStatus::Queued),
            task(TaskStatus::Running),
            task(TaskStatus::Succeeded),
            task(TaskStatus::Succeeded),
            task(TaskStatus::PartiallySucceeded),
            task(TaskStatus::Failed),
            task(TaskStatus::Cancelled),
            task(TaskStatus::AbortedBudget),
        ];
        let c = ClusterTasks::count(tasks.iter());
        assert_eq!(c.total, 8);
        assert_eq!(c.queued, 1);
        assert_eq!(c.running, 1);
        assert_eq!(c.succeeded, 2);
        assert_eq!(c.partially_succeeded, 1);
        assert_eq!(c.failed, 1);
        assert_eq!(c.cancelled, 1);
        assert_eq!(c.aborted_budget, 1);
        // 明细必须能对上总数，否则看板上会永远差几个人
        assert_eq!(
            c.queued
                + c.running
                + c.succeeded
                + c.partially_succeeded
                + c.failed
                + c.cancelled
                + c.aborted_budget,
            c.total
        );
    }

    #[test]
    fn empty_input_yields_all_zero() {
        let c = ClusterTasks::count(std::iter::empty());
        assert_eq!(c, ClusterTasks::default());
    }

    #[test]
    fn totals_sum_across_nodes_not_just_first() {
        let mut second = node();
        second.node_id = "n2".into();
        second.async_running = 5;
        second.async_capacity = 8;
        second.sse_subscribers = 2;
        let view = ClusterView::new(vec![node(), second]);
        assert_eq!(view.totals.nodes, 2);
        assert_eq!(view.totals.async_running, 8, "跨节点必须求和");
        assert_eq!(view.totals.async_capacity, 16);
        assert_eq!(view.totals.sse_subscribers, 3);
        assert_eq!(
            view.totals.agents_total,
            local_agents_total() * 2,
            "agent 数也按节点累加"
        );
    }

    #[test]
    fn single_node_view_keeps_array_shape() {
        // 单节点也必须返回数组（前端按 nodes[] 渲染，形状不能随部署规模变）
        let view = ClusterView::new(vec![node()]);
        assert_eq!(view.nodes.len(), 1);
        assert_eq!(view.nodes[0].role, ROLE_ALL_IN_ONE);
        assert_eq!(view.nodes[0].source, SOURCE_LOCAL);
        assert!(!view.nodes[0].stale, "本地节点永远不会 stale");
        assert_eq!(view.totals.offline_nodes, 0);
    }

    #[test]
    fn tasks_add_is_field_wise_so_totals_match_detail() {
        let a = snapshot("a", 2);
        let b = snapshot("b", 3);
        let mut sum = ClusterTasks::default();
        sum.add(&a.tasks);
        sum.add(&b.tasks);
        assert_eq!(sum.total, 5);
        assert_eq!(sum.running, 5);
        // 求和后明细仍必须能对上总数（漏字段的典型症状就是这里差 1）
        assert_eq!(
            sum.queued
                + sum.running
                + sum.succeeded
                + sum.partially_succeeded
                + sum.failed
                + sum.cancelled
                + sum.aborted_budget,
            sum.total
        );
    }

    #[test]
    fn stale_nodes_show_up_but_do_not_pollute_totals() {
        // 两台在线 + 一台心跳过期：过期那台要能被看见（知道掉了谁），
        // 但它的 running 不能算进"集群此刻在跑几个"
        let live = snapshot("a", 2).into_peer_node(false);
        let stale = snapshot("b", 7).into_peer_node(true);
        let view = ClusterView::new(vec![live, stale]);
        assert_eq!(view.nodes.len(), 2, "离线节点仍要出现在列表里");
        assert_eq!(view.totals.offline_nodes, 1);
        assert_eq!(view.totals.async_running, 2, "只算在线节点");
        assert_eq!(view.totals.tasks.total, 2);
        assert_eq!(view.totals.tasks.running, 2);
    }

    #[test]
    fn snapshot_roundtrips_through_json() {
        // 心跳文件是跨进程协议：序列化能过、反序列化过不了的话，
        // 表现是"目录里明明有文件，/cluster 却始终只有自己"。
        let snap = snapshot("host-a-1234", 4);
        let json = serde_json::to_string(&snap).unwrap();
        let back: NodeSnapshot = serde_json::from_str(&json).unwrap();
        assert_eq!(back.node_id, "host-a-1234");
        assert_eq!(back.tasks.running, 4);
        assert_eq!(back.agents_total, local_agents_total());
        let node = back.into_peer_node(false);
        assert_eq!(node.source, SOURCE_PEER);
        assert_eq!(node.role, ROLE_ALL_IN_ONE, "role 必须是常量而不是从文件信来的字符串");
    }
}
