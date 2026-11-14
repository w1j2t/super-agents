//! 任务状态事件（领域层）。
//!
//! # 为什么把事件放在 domain 而不是总线模块
//!
//! `TaskEvent` 描述的是"任务状态发生变化"这件事本身，属于**领域词汇**，
//! 与"用 SSE 还是 WebSocket 推"、"用内存总线还是 Redis"完全无关。
//! 把它放在 `domain` 有三个直接好处：
//!
//! 1. 契约生成器扫描 `domain/`，事件形状会自动进入 `openapi.json` 与前端 TS 类型，
//!    前端不必手抄一份 JSON 结构（手抄必漂移）；
//! 2. 总线实现（`orchestration::events`）可以整块替换，领域事件定义不动；
//! 3. 快照投影规则（截断长度）只有一处，不会出现"列表接口截 200、事件流截 80"的割裂。
//!
//! # 语义
//!
//! - `seq = 0` 是**快照帧**：客户端刚连上时服务端补发的"当前状态"，不是广播事件；
//! - `seq >= 1` 是广播事件，进程内单调递增，客户端据此判断有无漏帧。

use crate::domain::task::{TaskRecord, TaskStatus};
use chrono::{DateTime, Utc};
use serde::Serialize;

/// 摘要字段的字符上限：事件是"通知"，不该比它描述的实体还大。
pub const SUMMARY_LIMIT: usize = 200;

/// 一条任务状态事件。
///
/// 字段刻意做成**自足快照**：前端收到就能直接渲染一行进度，
/// 不必再回查 `GET /tasks/{id}`（弱网下少一次往返，也少一次竞态）。
#[derive(Debug, Clone, Serialize)]
pub struct TaskEvent {
    /// 进程内单调递增序号；`0` 专指连接时的快照帧
    pub seq: u64,
    pub task_id: String,
    pub status: TaskStatus,
    /// 任务最后一次更新时间（= 事件产生时间）
    pub at: DateTime<Utc>,
    pub finished_at: Option<DateTime<Utc>>,
    /// 结果摘要（截断到 [`SUMMARY_LIMIT`] 字符）
    pub summary: String,
}

impl TaskEvent {
    /// 连接瞬间的**快照帧**：`seq = 0`，表示"这不是广播事件，而是当前状态"。
    ///
    /// 有了它，前端连上就能立刻画出当前状态，不必先请求详情再订阅，
    /// 也就没有"查完详情、订阅之前状态又变了"的那段窗口。
    pub fn snapshot(task: &TaskRecord) -> Self {
        Self::build(task, 0)
    }

    /// 广播帧。`seq` 由总线分配，领域层不关心怎么分配。
    pub fn build(task: &TaskRecord, seq: u64) -> Self {
        Self {
            seq,
            task_id: task.id.clone(),
            status: task.status,
            at: task.updated_at,
            finished_at: task.finished_at,
            summary: task.result_summary.chars().take(SUMMARY_LIMIT).collect(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::task::{ModelProfile, TaskType};

    fn task(status: TaskStatus) -> TaskRecord {
        let now = Utc::now();
        TaskRecord {
            id: "t1".into(),
            owner_id: "u1".into(),
            goal: "验证事件形状".into(),
            task_type: TaskType::Coding,
            requested_model: Some(ModelProfile::Standard),
            status,
            plan: Vec::new(),
            results: Vec::new(),
            trace: Vec::new(),
            result_summary: "汉".repeat(500), // 多字节字符也要按"字符"截断，不能切碎
            tokens_used: 0,
            budget_tokens: 1000,
            created_at: now,
            updated_at: now,
            finished_at: None,
            memory_refs: Vec::new(),
            agreement_score: 0.9,
        }
    }

    #[test]
    fn snapshot_is_seq_zero_so_frontend_can_tell_it_from_broadcasts() {
        let snap = TaskEvent::snapshot(&task(TaskStatus::Running));
        assert_eq!((snap.seq, snap.status), (0, TaskStatus::Running));
        assert_eq!(snap.task_id, "t1");
    }

    #[test]
    fn summary_is_truncated_by_chars_not_bytes() {
        let ev = TaskEvent::build(&task(TaskStatus::Succeeded), 7);
        assert_eq!(ev.seq, 7);
        assert_eq!(ev.summary.chars().count(), SUMMARY_LIMIT);
        // 按字符截断：不会出现半个汉字导致前端 JSON 乱码
        assert!(ev.summary.chars().all(|c| c == '汉'));
    }

    #[test]
    fn event_serializes_with_snake_case_fields() {
        let json = serde_json::to_value(TaskEvent::snapshot(&task(TaskStatus::Queued))).unwrap();
        // 前端按这些键名取值；改名属于破坏性契约变更，必须在测试里被拦住
        for key in ["seq", "task_id", "status", "at", "finished_at", "summary"] {
            assert!(json.get(key).is_some(), "缺少字段 {key}");
        }
        assert_eq!(json["status"], "queued");
    }
}
