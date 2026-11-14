//! 集群视图装配：把"本地实时快照 + 共享目录里的 peer 心跳"拼成一份响应。
//!
//! handler 与后台心跳都用这里，是为了保证**同一份采集口径**。
//! 早前踩过：`/cluster` 里的 `async_running` 从取消登记簿取、
//! 心跳却从任务表状态字段数——两处差一两个数，看板上"节点 A 说 2 个在跑、
//! 汇总说 3 个"这种对不上的账，排查要花掉一晚上。
//! 现在只有一个采集函数，两处必然一致。

use crate::domain::cluster::{
    local_agents_total, ClusterNode, ClusterTasks, ClusterView, NodeSnapshot,
};
use crate::state::AppState;
use chrono::Utc;

/// 采集本地节点的实时快照。
///
/// 任务分布是在 store 的读锁内直接数的，不把任务表 clone 出来：
/// 个人部署任务量小，但"顺手少一次全表拷贝"不需要任何代价。
pub fn local_snapshot(state: &AppState) -> NodeSnapshot {
    let tasks = state.store.read(|db| ClusterTasks::count(db.tasks.values()));
    let now = Utc::now();
    NodeSnapshot {
        node_id: state.config.node_id.clone(),
        role: crate::domain::cluster::ROLE_ALL_IN_ONE.into(),
        started_at: state.started_at,
        // 时长用同一只时钟求差，并夹到 0：系统时间被手动调整过时会出现负数，
        // 看板上冒出"-3 秒前启动"的鬼话比不显示更糟。
        uptime_seconds: (now - state.started_at).num_seconds().max(0) as u64,
        step_concurrency: state.config.step_concurrency,
        // 在跑的异步任务数取自取消登记簿：它是"有执行体"的唯一真值来源，
        // 不读任务表的状态字段（那里可能因一次未落盘的中间态而暂时滞后）。
        async_running: state.cancellations.active(),
        async_capacity: state.config.async_max_running,
        sse_subscribers: state.events.subscribers(),
        events_dropped: state.events.dropped(),
        agents_total: local_agents_total(),
        tasks,
        updated_at: now,
    }
}

/// 本地节点的视图形态（`source="local"`，永不 stale）。
pub fn local_node(state: &AppState) -> ClusterNode {
    local_snapshot(state).into_local_node()
}

/// `GET /api/v1/cluster` 的完整视图：本地节点排第一，peer 按 `node_id` 排序。
///
/// 本地排第一是有意的：前端第一张卡片通常是"本机"，
/// 让它随 peer 一起排序会导致刷新一次换一次位置。
pub fn view(state: &AppState) -> ClusterView {
    let mut nodes = Vec::with_capacity(4);
    nodes.push(local_node(state));
    nodes.extend(state.cluster.peers());
    ClusterView::new(nodes)
}

/// 写一次心跳。后台循环每 `SA_CLUSTER_HEARTBEAT_SECS` 调一次。
///
/// 失败只记 warn 不重试：心跳是"尽力而为的信号"，下一轮自然补上。
/// 在这里重试或报错，只会把一次磁盘抖动放大成日志洪水。
pub fn heartbeat_once(state: &AppState) {
    if !state.cluster.enabled() {
        return;
    }
    let snap = local_snapshot(state);
    if let Err(e) = state.cluster.heartbeat(&snap) {
        tracing::warn!(error = %e, dir = %state.cluster.dir().display(), "集群心跳写入失败");
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Config;
    use crate::store::Store;
    use std::sync::Arc;

    fn state() -> Arc<AppState> {
        let dir = std::env::temp_dir().join(format!(
            "sa-cluster-view-{}-{}",
            std::process::id(),
            Utc::now().timestamp_nanos_opt().unwrap_or(0)
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let config = Config::for_test(dir.clone());
        let store = Store::with_kind(config.storage, config.snapshot_path()).unwrap();
        Arc::new(AppState::new(config, store).unwrap())
    }

    #[test]
    fn view_always_contains_local_node_first() {
        let st = state();
        let v = view(&st);
        assert_eq!(v.nodes.len(), 1, "测试配置关闭心跳，只应有本地节点");
        assert_eq!(v.nodes[0].source, "local");
        assert_eq!(v.nodes[0].node_id, st.config.node_id);
        assert_eq!(v.totals.nodes, 1);
        assert_eq!(v.totals.offline_nodes, 0);
        // 汇总与本地明细必须自洽（这正是早前两处口径不一的病根）
        assert_eq!(v.totals.tasks, v.nodes[0].tasks);
        assert_eq!(v.totals.async_running, st.cancellations.active());
    }

    #[test]
    fn heartbeat_disabled_writes_nothing() {
        let st = state();
        heartbeat_once(&st);
        assert!(
            !st.cluster.self_file().exists(),
            "SA_CLUSTER=off 时不该在磁盘留下任何痕迹"
        );
    }
}
