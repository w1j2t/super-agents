//! 进程入口：加载配置 → 打开快照存储 → 装配状态 → 启动 HTTP 服务 → 优雅退出并落盘。

use clap::Parser;
use personal_super_agents::cli::{self, Cli, Cmd};
use personal_super_agents::{api, config::Config, state::AppState, store::Store};
use std::time::Duration;
use tracing_subscriber::EnvFilter;

/// 分派而不是"全部解析后都走 serve"：
/// 门禁脚本一律以 `[exe]` 无参启动，`None` 必须等同于 `serve`，
/// 否则依赖"起进程再打端口"的那些门禁会集体变红。
#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let cli = Cli::parse();
    match cli.cmd.unwrap_or(Cmd::Serve) {
        Cmd::Serve => serve().await,
        Cmd::Doctor(args) => {
            // 结论走退出码（0/1/2），与门禁同一套四分法：脚本能直接判。
            // 这里不初始化 tracing：doctor 的 --json 输出会被日志行污染。
            std::process::exit(cli::doctor::run(&args));
        }
        // 下面这几个都是"一次性本地动作"：跑完就把退出码交出去。
        // 它们用的是阻塞式 TcpStream（见 cli/http.rs），跑在 tokio 的 worker 线程上会占住一个，
        // 但这些命令不做任何并发，占住也无所谓；换来的是不必把 CLI 也写成 async。
        Cmd::Login(args) => std::process::exit(cli::auth::login(&args)),
        Cmd::Logout(args) => std::process::exit(cli::auth::logout(&args)),
        Cmd::Submit(args) => std::process::exit(cli::task::submit(&args.conn, &args)),
        Cmd::Ls(args) => std::process::exit(cli::task::ls(&args.conn, &args)),
        Cmd::Show(args) => std::process::exit(cli::task::show(&args.conn, &args)),
    }
}

/// 原来的进程入口逻辑，一行未改地搬进来（只去掉 `#[tokio::main]`）。
async fn serve() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .with_target(false)
        .init();

    let config = Config::from_env()?;
    std::fs::create_dir_all(&config.data_dir)?;

    let bind = config.bind_addr.clone();
    let snapshot_interval = config.snapshot_interval_secs.max(1);
    let store = Store::with_kind(config.storage, config.snapshot_path())?;
    if store.kind().is_durable() {
        tracing::info!(storage = %store.kind().as_str(), path = %store.path().display(), "存储后端就绪");
    } else {
        tracing::warn!(
            storage = %store.kind().as_str(),
            "当前为纯内存后端：进程退出即清空数据，仅用于压测/演示"
        );
    }
    let runner_kind = config.runner;
    let state = AppState::new(config, store)
        .map_err(|e| -> Box<dyn std::error::Error> { e.into() })?
        .shared();
    tracing::info!(runner = %runner_kind.as_str(), "执行后端就绪");

    // 部件降级必须在启动时就出声。
    // 审计不落盘、限流不生效、指标不采集，都属于"静默变弱"：程序照常跑，
    // 但出事的时候既没证据、也拦不住、还查不出原因。用户不知道就会误以为有保障。
    // 判断一律问运行中的实例要 kind()，不看配置——配置说 file、实例装的却是 memory 的情况必须能被发现。
    match state.audit.kind() {
        "file" => tracing::info!(audit = %state.audit.kind(), "审计出口就绪"),
        other => tracing::warn!(
            audit = %other,
            "审计不落盘：进程退出即丢失，事后无法回溯；需要留证请设 SA_AUDIT=file"
        ),
    }
    // 审计自检：`kind()` 只说明"装的是谁"，证明不了"真写得进去"。
    // 磁盘满、目录权限被改、网络盘掉线都会让 append 静默失败——因为 record() 按契约
    // 不返回错误。所以启动时真写一条 boot 记录再读回来，写不进就拒启：
    // 宁可起不来，也不要带着"看起来有审计"跑一整周。
    state.audit.probe().map_err(|e| -> Box<dyn std::error::Error> {
        format!("审计启动自检失败，拒绝启动：{e}").into()
    })?;
    tracing::info!("审计启动自检通过：boot 记录已写入并回读确认");

    match state.limiter.kind() {
        "noop" => tracing::warn!(
            limiter = "noop",
            "限流未生效：提交风控形同虚设；需要限流请设 SA_LIMITER=memory"
        ),
        other => tracing::info!(limiter = %other, "限流后端就绪"),
    }
    match state.metrics.kind() {
        "noop" => tracing::warn!(
            metrics = "noop",
            "指标未采集：/metrics 只有空壳；需要观测请设 SA_METRICS_EXPORT=prometheus"
        ),
        other => tracing::info!(metrics = %other, "指标出口就绪"),
    }

    // 后台快照：有变更才写盘，避免空转 IO
    let flusher = {
        let state = state.clone();
        tokio::spawn(async move {
            let mut ticker = tokio::time::interval(Duration::from_secs(snapshot_interval));
            ticker.tick().await;
            loop {
                ticker.tick().await;
                match state.store.flush_if_dirty() {
                    Ok(true) => tracing::debug!("snapshot flushed"),
                    Ok(false) => {}
                    Err(err) => tracing::error!(error = %err, "snapshot flush failed"),
                }
            }
        })
    };

    // 写后落盘窗口：变更后 ≤ flush_interval_ms 必须落盘。
    // 只靠「15 秒一次的全量快照」意味着进程被强杀时最多丢 15 秒写入 —— 对账号/任务数据不可接受。
    let write_through = {
        let state = state.clone();
        let every = Duration::from_millis(state.config.flush_interval_ms.max(50));
        tokio::spawn(async move {
            let mut ticker = tokio::time::interval(every);
            loop {
                ticker.tick().await;
                if let Err(err) = state.store.flush_if_dirty() {
                    tracing::error!(error = %err, "write-through flush failed");
                }
            }
        })
    };

    // 集群心跳：每 `SA_CLUSTER_HEARTBEAT_SECS` 把"我还活着、正扛着什么"写进共享目录。
    //
    // 为什么必须是后台循环，而不是"有请求时顺手写一下"：`/cluster` 可能几小时没人看，
    // 那段时间本节点在别人眼里就该是 stale。心跳的频率必须与"有没有人看"无关，
    // 否则看板会把"没人访问的存活节点"判成离线。
    //
    // `interval` 的第一次 tick 立即返回，所以进程一起来就有一份心跳，
    // 不必等满一个周期才被兄弟节点发现——重启后"消失 5 秒"是很容易被误判成故障的。
    //
    // 关闭时（`SA_CLUSTER=off`）不挂任务也不留空转循环：宁可没有这个循环，
    // 也不要留一个每 5 秒醒一次、每次什么都不做的线程。
    let heartbeat = {
        let state = state.clone();
        if state.cluster.enabled() {
            tracing::info!(
                dir = %state.cluster.dir().display(),
                node_id = %state.cluster.node_id(),
                every_secs = state.config.cluster_heartbeat_secs,
                ttl_secs = state.config.cluster_ttl_secs,
                "集群心跳就绪（共享目录模式：多节点把同一块目录挂到各机器即可）"
            );
            Some(tokio::spawn(async move {
                let every = Duration::from_secs(state.config.cluster_heartbeat_secs.max(1));
                let mut ticker = tokio::time::interval(every);
                loop {
                    ticker.tick().await;
                    personal_super_agents::services::cluster_view::heartbeat_once(&state);
                }
            }))
        } else {
            tracing::info!("集群心跳已关闭：/cluster 只报本节点（SA_CLUSTER=off）");
            None
        }
    };

    let app = api::router(state.clone());
    let listener = tokio::net::TcpListener::bind(&bind).await?;
    tracing::info!(
        addr = %listener.local_addr()?,
        env = %state.config.env,
        storage = %state.store.kind().as_str(),
        runner = %runner_kind.as_str(),
        "personal super agents backend ready"
    );

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;

    flusher.abort();
    write_through.abort();
    if let Some(h) = heartbeat {
        h.abort();
    }
    // 删掉自己的心跳文件：正常退出要让兄弟节点**立刻**看见"我走了"，
    // 而不是挂满一个 TTL 才从看板上变红。TTL 是崩溃/断网时的兜底，
    // 不该成为正常关机的常态延迟。
    state.cluster.clear_self();
    state.store.flush()?;
    tracing::info!("state persisted, bye");
    Ok(())
}

/// 优雅退出信号：Ctrl+C / Windows 的 CTRL_BREAK、CTRL_CLOSE、CTRL_SHUTDOWN（注销、关机）/ Unix 的 SIGTERM。
///
/// 只监听 Ctrl+C 是常见的半成品：容器 `docker stop`、systemd、K8s 发的都是 SIGTERM，
/// 只等 Ctrl+C 的进程会被等满超时后 SIGKILL 强杀，**最后一段写入全部丢失**。
async fn shutdown_signal() {
    let ctrl_c = async {
        let _ = tokio::signal::ctrl_c().await;
    };

    #[cfg(windows)]
    let other = async {
        use tokio::signal::windows;
        let (mut brk, mut cls, mut sdn) = match (
            windows::ctrl_break(),
            windows::ctrl_close(),
            windows::ctrl_shutdown(),
        ) {
            (Ok(b), Ok(c), Ok(s)) => (b, c, s),
            _ => return,
        };
        tokio::select! {
            _ = brk.recv() => {},
            _ = cls.recv() => {},
            _ = sdn.recv() => {},
        }
    };

    #[cfg(unix)]
    let other = async {
        use tokio::signal::unix::{signal, SignalKind};
        match signal(SignalKind::terminate()) {
            Ok(mut term) => {
                term.recv().await;
            }
            Err(_) => std::future::pending::<()>().await,
        }
    };

    tokio::select! {
        _ = ctrl_c => {},
        _ = other => {},
    }
    tracing::info!("shutdown signal received");
}
