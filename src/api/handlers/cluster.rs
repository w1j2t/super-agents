//! 集群视图：`GET /api/v1/cluster`。
//!
//! 这个 handler 只做"取数 + 拼装"，没有任何业务判断：
//! 分布统计算在 [`crate::domain::cluster::ClusterTasks`]（有单测），
//! 汇总装配算在 [`crate::domain::cluster::ClusterView`]（也有单测）。
//! 之所以把两者分开，是因为之前踩过——统计逻辑写在 handler 里，就只能靠起服务验证，
//! 而"总数对不上明细"这类错误在 HTTP 层极难断言。
//!
//! # 为什么不按用户过滤
//!
//! 这是**节点**的事实（进程启动多久、异步槽位占了几格、几个前端连着 SSE），
//! 不是某个用户的数据。因此不发 `owner_id` 维度，也统一要求 `admin` 权限——
//! 普通用户能看到"别人提交的任务把队列占满了"既无用又多一层信息泄露面。
//! 需要看自己任务进度的用户走 `GET /tasks/{id}` 与 SSE，那两条路径本来就按 owner 隔离。

use crate::api::extract::AuthUser;
use crate::domain::cluster::ClusterView;
use crate::error::ApiError;
use crate::security::rbac::Permission;
use crate::state::SharedState;
use axum::extract::State;
use axum::Json;

/// `GET /api/v1/cluster` — 当前集群有哪些节点、各扛着什么。
///
/// 鉴权：`admin`。返回的 `nodes` 形状为多节点预留：
/// 单机部署时只有自己这一条（`source="local"`、`stale=false`）；
/// 多机共享心跳目录时额外带上 peer（失联的会标 `stale=true`，且不计入 `totals`）。
pub async fn overview(
    State(state): State<SharedState>,
    user: AuthUser,
) -> Result<Json<ClusterView>, ApiError> {
    user.require(Permission::Admin)?;
    // 取数只留这一个入口：本地实时快照 + 共享目录里的 peer 心跳，一次凑齐。
    //
    // 这里曾经自带一份采集逻辑（自己数任务、自己算 uptime、自己拼 ClusterNode），
    // 与后台心跳写入用的另一份采集各自演化，很快就出现了"接口报的和心跳写的不是一回事"。
    // 那类不一致最难查：两边都"能跑"，只是账对不上。现在结构上只剩一条取数路径，
    // 想不一致都做不到——这也正是 `services::cluster_view` 存在的唯一理由。
    Ok(Json(crate::services::cluster_view::view(&state)))
}
