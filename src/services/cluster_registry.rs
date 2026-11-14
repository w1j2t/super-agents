//! 多节点心跳：把"这个节点还活着、正扛着什么"写进共享目录，也把别人的读回来。
//!
//! # 为什么是共享目录，而不是节点之间 HTTP 互探
//!
//! 个人部署的现实是"两三台机器 + 一块同步盘/NAS/局域网共享"。共享目录方案：
//! - **零新依赖**：不引 http client、不引服务发现 crate，一个 `read_dir` 就够；
//! - **单向失败**：A 读不到 B 的文件只影响"少看见一个节点"，
//!   不会因为 B 卡住而把 A 的 `/cluster` 请求一起挂住（HTTP 互探的典型事故）；
//! - **可人工核查**：出问题时打开目录看一眼文件时间戳，比抓包快。
//!
//! 代价写明白：它要求"目录对所有节点可见"，跨网段/跨机房不适用。
//! 真要跨机房，换的是本模块的实现，`/cluster` 的响应形状不动——
//! 这正是把行为收在这一个文件里的原因。
//!
//! # 文件生命周期
//!
//! | 时刻 | 动作 |
//! |---|---|
//! | 每 `SA_CLUSTER_HEARTBEAT_SECS`（默认 5s） | 原子覆盖写 `<node_id>.json` |
//! | 每次 `/cluster` 或 `/metrics` 被读 | 读取所有 `*.json`，按 TTL 判 stale |
//! | 超过 `6 × TTL` 未更新 | 视为遗物，物理删除（防止目录无限堆积）|
//! | 进程正常退出 | 删除自己的文件（让别人立刻看见"我走了"，不必等 TTL）|
//!
//! 最后一行是刻意做的：TTL 是兜底手段，不该成为常态延迟。
//! 一次 Ctrl+C 之后看板仍显示"该节点在线 20 秒"，会让人怀疑数据到底准不准。

use crate::domain::cluster::{ClusterNode, NodeSnapshot};
use chrono::Utc;
use std::io;
use std::path::{Path, PathBuf};
use std::time::Duration;

/// 心跳文件的过期倍数：`TTL × 这个值` 之后直接删除文件。
///
/// 取 6 而不是 2：多留几个周期，是为了让"刚掉线的节点"还能在 `nodes` 里
/// 以 `stale=true` 的形式存在一段时间，看板能显示"某某节点 8 秒前失联"。
/// 留得太短，表现是"节点一闪就消失"，反而查不到线索。
pub const PRUNE_TTL_FACTOR: i64 = 6;

/// 心跳文件名长度上限（不含 `.json`）。太长在 Windows 上会撞 260 字符路径限制。
const MAX_NAME_LEN: usize = 96;

pub struct ClusterRegistry {
    dir: PathBuf,
    node_id: String,
    ttl: Duration,
    enabled: bool,
}

impl ClusterRegistry {
    pub fn new(dir: PathBuf, node_id: String, ttl: Duration, enabled: bool) -> Self {
        Self {
            dir,
            node_id,
            ttl,
            enabled,
        }
    }

    /// 从配置装配。关闭时钟返回的实例所有方法都是空操作——
    /// 调用方不需要到处 `if enabled`，那是把判断散到五个地方等着漏。
    pub fn from_config(config: &crate::config::Config) -> Self {
        Self::new(
            config.cluster_dir.clone(),
            config.node_id.clone(),
            Duration::from_secs(config.cluster_ttl_secs.max(1)),
            config.cluster_enabled,
        )
    }

    pub fn enabled(&self) -> bool {
        self.enabled
    }

    pub fn dir(&self) -> &Path {
        &self.dir
    }

    pub fn node_id(&self) -> &str {
        &self.node_id
    }

    pub fn ttl(&self) -> Duration {
        self.ttl
    }

    /// 自己的心跳文件名。`node_id` 可能含 `/`、`:`、空格等不能进文件名的字符
    /// （缺省值就是 `主机名-pid`，主机名带点带空格都正常），所以统一做一次替换。
    pub fn self_file(&self) -> PathBuf {
        self.dir.join(format!("{}.json", sanitize(&self.node_id)))
    }

    /// 写一次心跳（原子覆盖）。
    ///
    /// 先写 `.tmp` 再 rename：读侧可能在任何时刻撞上写侧。
    /// 直接覆盖目标文件的话，读侧有一瞬间会读到"半截 JSON"，
    /// 表现为"某个节点时有时无"这种最难查的间歇性故障。
    pub fn heartbeat(&self, snap: &NodeSnapshot) -> io::Result<()> {
        if !self.enabled {
            return Ok(());
        }
        std::fs::create_dir_all(&self.dir)?;
        let target = self.self_file();
        let tmp = self
            .dir
            .join(format!("{}.tmp", sanitize(&self.node_id)));
        let body = serde_json::to_vec_pretty(snap)
            .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
        std::fs::write(&tmp, body)?;
        std::fs::rename(&tmp, &target)?;
        Ok(())
    }

    /// 读回所有 peer 的节点视图（已含 stale 判定与遗物清理）。
    ///
    /// 返回顺序按 `node_id` 排序：不排序的话 `read_dir` 的顺序随文件系统变，
    /// `/cluster` 的节点列表会在两次抓取之间跳来跳去，前端做 diff 会误判"节点换了"。
    pub fn peers(&self) -> Vec<ClusterNode> {
        if !self.enabled {
            return Vec::new();
        }
        let now = Utc::now();
        let entries = match std::fs::read_dir(&self.dir) {
            Ok(e) => e,
            // 目录还不存在（本进程刚起、首次心跳未写）= 没有 peer，不是错误。
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Vec::new(),
            Err(e) => {
                tracing::warn!(dir = %self.dir.display(), error = %e, "集群心跳目录不可读，按无 peer 处理");
                return Vec::new();
            }
        };

        let self_name = self.self_file().file_name().and_then(|s| s.to_str()).map(str::to_string);
        let mut out = Vec::new();
        for entry in entries.flatten() {
            let name = entry.file_name();
            let Some(name) = name.to_str() else { continue };
            // 只认 `<something>.json`：把 `.tmp`、编辑器备份、`.DS_Store` 一并挡在外面。
            if !name.ends_with(".json") || name.starts_with('.') {
                continue;
            }
            if Some(name.to_string()) == self_name {
                continue;
            }
            let path = entry.path();
            let parsed = std::fs::read(&path)
                .ok()
                // `fs::read` 产出的是 `Vec<u8>`，而 `from_slice` 收 `&[u8]`：
                // 这里借一次就够，不必 `as_ref()` 之后再借（多一步类型体操少一分可读性）。
                .and_then(|raw| serde_json::from_slice::<NodeSnapshot>(&raw).ok());
            let Some(snap) = parsed else {
                // 读得到但解析不了：可能是别人正在 rename 前的 tmp（不该是 .json），
                // 也可能是版本不一致。静默跳过，但留一条 debug 便于排查"为什么少一个节点"。
                tracing::debug!(file = %path.display(), "心跳文件无法解析，跳过");
                continue;
            };
            // 顺手清遗物：只删"文件名合规 + 内容能解析 + 确实过老"的那种，
            // 绝不对未知文件动手——这个目录可能被用户拿来放别的东西。
            if is_expired(&snap, now, self.ttl, PRUNE_TTL_FACTOR) {
                match std::fs::remove_file(&path) {
                    Ok(()) => tracing::debug!(file = %path.display(), "清理过期心跳文件"),
                    Err(e) => tracing::debug!(file = %path.display(), error = %e, "清理过期心跳失败"),
                }
                continue;
            }
            // 先算 stale（要借 `snap`），再把它消耗掉转成对外的节点视图。
            // 挤成一个表达式的话，`into_peer_node` 的 move 与 `is_stale` 的借用在同一次求值里打架——
            // 拆成两步不是为了绕过编译器的脾气，而是让"判活"和"转换"这两件事各归各位。
            let stale = is_stale(&snap, now, self.ttl);
            out.push(snap.into_peer_node(stale));
        }
        out.sort_by(|a, b| a.node_id.cmp(&b.node_id));
        out
    }

    /// 删除自己的心跳文件（正常退出时调用）。
    ///
    /// 失败只记日志：这属于"加速别人发现我走了"，删不掉最多等 TTL，
    /// 不该让退出流程因此报错。
    pub fn clear_self(&self) {
        if !self.enabled {
            return;
        }
        let path = self.self_file();
        if path.exists() {
            if let Err(e) = std::fs::remove_file(&path) {
                tracing::warn!(file = %path.display(), error = %e, "退出时清除心跳文件失败");
            }
        }
    }
}

/// 超过 TTL 未更新即视为离线。
///
/// `updated_at` 在未来（别人的钟快了）算 0 岁：宁可当它在线，也不要因为一次
/// NTP 校正就把整个节点判死——那会让 `/cluster` 在时钟同步窗口里集体跳红。
pub fn is_stale(snap: &NodeSnapshot, now: chrono::DateTime<Utc>, ttl: Duration) -> bool {
    age_secs(snap, now) > ttl.as_secs() as i64
}

/// 是否该被物理删除。
pub fn is_expired(
    snap: &NodeSnapshot,
    now: chrono::DateTime<Utc>,
    ttl: Duration,
    factor: i64,
) -> bool {
    age_secs(snap, now) > ttl.as_secs() as i64 * factor
}

fn age_secs(snap: &NodeSnapshot, now: chrono::DateTime<Utc>) -> i64 {
    (now - snap.updated_at).num_seconds().max(0)
}

/// 把 `node_id` 变成安全的文件名片段。
///
/// 规则：保留 `[A-Za-z0-9._-]`，其余一律换成 `_`，并截断到 96 字符。
/// 截断可能让两个超长 id 撞名——但那两台的 `node_id` 本身也几乎不可能同时出现，
/// 且撞名的后果是"少看见一个节点"，比"路径超长导致心跳永远写失败"轻得多。
pub fn sanitize(node_id: &str) -> String {
    let mut out = String::with_capacity(node_id.len());
    for ch in node_id.chars() {
        if ch.is_ascii_alphanumeric() || matches!(ch, '.' | '_' | '-') {
            out.push(ch);
        } else {
            out.push('_');
        }
        if out.len() >= MAX_NAME_LEN {
            break;
        }
    }
    if out.is_empty() {
        // 空 node_id 不可能（Config 有兜底），但这里不能返回空串：
        // `".json"` 是隐藏文件，会被自己的读侧过滤掉，表现为"心跳写了但没人看见"。
        out.push_str("node");
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::cluster::{ClusterTasks, ROLE_ALL_IN_ONE};

    fn snapshot(node_id: &str, age_secs: i64) -> NodeSnapshot {
        NodeSnapshot {
            node_id: node_id.into(),
            role: ROLE_ALL_IN_ONE.into(),
            started_at: Utc::now(),
            uptime_seconds: 1,
            step_concurrency: 1,
            async_running: 1,
            async_capacity: 8,
            sse_subscribers: 0,
            events_dropped: 0,
            agents_total: 10,
            tasks: ClusterTasks::default(),
            updated_at: Utc::now() - chrono::Duration::seconds(age_secs),
        }
    }

    fn temp_dir(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "sa-cluster-{tag}-{}-{}",
            std::process::id(),
            Utc::now().timestamp_nanos_opt().unwrap_or(0)
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn sanitize_replaces_path_hostile_chars() {
        assert_eq!(sanitize("DESKTOP-ABC 1234"), "DESKTOP-ABC_1234");
        assert_eq!(sanitize("a/b:c"), "a_b_c");
        assert_eq!(sanitize(""), "node");
        let long = "x".repeat(300);
        assert_eq!(sanitize(&long).len(), MAX_NAME_LEN);
    }

    #[test]
    fn heartbeat_then_peers_sees_it_and_skips_self() {
        let dir = temp_dir("hb");
        let me = ClusterRegistry::new(dir.clone(), "me-1".into(), Duration::from_secs(20), true);
        let other = ClusterRegistry::new(dir.clone(), "other-2".into(), Duration::from_secs(20), true);
        other.heartbeat(&snapshot("other-2", 0)).unwrap();

        let seen = me.peers();
        assert_eq!(seen.len(), 1, "自己写的心跳不该被自己当 peer");
        assert_eq!(seen[0].node_id, "other-2");
        assert!(!seen[0].stale);

        // 自己写一份，仍然只看见别人
        me.heartbeat(&snapshot("me-1", 0)).unwrap();
        let seen = me.peers();
        assert_eq!(seen.len(), 1);
        assert!(me.self_file().exists());

        me.clear_self();
        assert!(!me.self_file().exists(), "正常退出要留下'我走了'的信号");
    }

    #[test]
    fn stale_after_ttl_and_pruned_after_six_ttl() {
        let dir = temp_dir("ttl");
        let reg = ClusterRegistry::new(dir.clone(), "me".into(), Duration::from_secs(20), true);

        let fresh = ClusterRegistry::new(dir.clone(), "fresh".into(), Duration::from_secs(20), true);
        fresh.heartbeat(&snapshot("fresh", 0)).unwrap();
        let stale = ClusterRegistry::new(dir.clone(), "old".into(), Duration::from_secs(20), true);
        stale.heartbeat(&snapshot("old", 30)).unwrap();
        let relic = ClusterRegistry::new(dir.clone(), "relic".into(), Duration::from_secs(20), true);
        relic.heartbeat(&snapshot("relic", 5 * 60)).unwrap();

        let seen = reg.peers();
        assert_eq!(seen.len(), 2, "过期但未到清理线的仍要可见");
        assert_eq!(seen[0].node_id, "fresh");
        assert!(!seen[0].stale);
        assert_eq!(seen[1].node_id, "old");
        assert!(seen[1].stale, "超过 TTL 必须标 stale");
        assert!(!relic.self_file().exists(), "超过 6×TTL 的遗物要被清掉");
    }

    #[test]
    fn malformed_file_is_ignored_not_fatal() {
        let dir = temp_dir("bad");
        std::fs::write(dir.join("broken.json"), b"{not json").unwrap();
        std::fs::write(dir.join("notes.txt"), b"hello").unwrap();
        let reg = ClusterRegistry::new(dir, "me".into(), Duration::from_secs(20), true);
        // 一个坏文件不能让整个集群视图变成 500：那会把"某个节点写坏了"
        // 升级成"所有人都看不见集群"。
        assert!(reg.peers().is_empty());
    }

    #[test]
    fn disabled_registry_never_touches_disk() {
        let dir = temp_dir("off");
        let reg = ClusterRegistry::new(dir.clone(), "me".into(), Duration::from_secs(20), false);
        reg.heartbeat(&snapshot("me", 0)).unwrap();
        assert!(!reg.self_file().exists());
        assert!(reg.peers().is_empty());
    }
}
