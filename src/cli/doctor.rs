//! `doctor`：只读自检。
//!
//! ## 核心设计约束：**不能复用启动路径**
//!
//! 最需要诊断的时刻，恰恰是"服务起不来"的时候。任何"先装配 `AppState` 再检查"的实现，
//! 在那种场景下都会跟着一起起不来——工具和它要诊断的东西一起死了，等于没有工具。
//! 所以这里**刻意不构造 `AppState`**，只做三类事：
//!   1. 读环境变量（与 `Config::from_env` 同一入口，保证口径不漂移）；
//!   2. 探测文件系统可写性（创建临时文件后立刻删除）；
//!   3. 探测端口/地址可达性（`TcpListener::bind` 试占 + 最小 HTTP 探活）。
//!
//! ## 已知边界（写在代码里而不是文档里，因为看代码的人比看文档的人多）
//!
//! - 槽位一律读**配置**而不是读实例的 `kind()`。两者在正常情况下必然一致；
//!   不一致（配置写 file、实例装成 memory）属于启动告警的职责，那时服务已经在跑了，
//!   该看的是启动日志，不是 `doctor`。
//! - 不检查磁盘剩余空间：标准库没有跨平台接口，为一个提示引入新 crate 不划算。

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream, ToSocketAddrs};
use std::path::Path;
use std::time::Duration;

use super::DoctorArgs;
use crate::config::Config;

#[derive(Debug, PartialEq, Eq, Clone, Copy)]
pub enum Level {
    Ok,
    Warn,
    Skip,
    Error,
}

impl Level {
    fn tag(self) -> &'static str {
        match self {
            Level::Ok => "OK  ",
            Level::Warn => "WARN",
            Level::Skip => "SKIP",
            Level::Error => "ERR ",
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Level::Ok => "ok",
            Level::Warn => "warn",
            Level::Skip => "skip",
            Level::Error => "error",
        }
    }
}

#[derive(Debug)]
pub struct Check {
    pub name: &'static str,
    pub level: Level,
    pub detail: String,
    pub hint: Option<String>,
}

impl Check {
    fn new(name: &'static str, level: Level, detail: impl Into<String>) -> Self {
        Check {
            name,
            level,
            detail: detail.into(),
            hint: None,
        }
    }

    fn hint(mut self, hint: impl Into<String>) -> Self {
        self.hint = Some(hint.into());
        self
    }
}

/// 入口：返回进程退出码（0 通过 / 1 有 WARN 且 --strict / 2 有 ERROR）。
pub fn run(args: &DoctorArgs) -> i32 {
    let checks = collect();
    let err = checks.iter().filter(|c| c.level == Level::Error).count();
    let warn = checks.iter().filter(|c| c.level == Level::Warn).count();

    let code = if err > 0 {
        2
    } else if warn > 0 && args.strict {
        1
    } else {
        0
    };

    if args.json {
        print_json(&checks, code);
    } else {
        print_text(&checks, code, args.strict);
    }
    code
}

fn collect() -> Vec<Check> {
    let mut out = Vec::new();

    // 配置是后面所有检查的前置。它失败时不假装能继续：那会产出十行"看起来还好"的
    // SKIP，反而盖住了真正的原因。
    let cfg = match Config::from_env() {
        Ok(c) => {
            out.push(
                Check::new(
                    "配置加载",
                    Level::Ok,
                    format!("env={} bind={} data_dir={}", c.env, c.bind_addr, c.data_dir.display()),
                )
                .hint("改动任何 SA_* 后都要重跑本命令；配置是启动时一次性读取的"),
            );
            Some(c)
        }
        Err(e) => {
            out.push(
                Check::new("配置加载", Level::Error, e.to_string())
                    .hint("这是启动会失败的直接原因，先修它；其余检查项已跳过"),
            );
            None
        }
    };

    let Some(cfg) = cfg else {
        for name in [
            "数据目录", "存储槽位", "审计槽位", "限流槽位", "指标槽位", "执行槽位",
            "监听端口", "模型端点", "集群心跳",
        ] {
            out.push(Check::new(name, Level::Skip, "配置未加载，无法检查"));
        }
        return out;
    };

    out.push(check_data_dir(&cfg));
    out.push(check_storage(&cfg));
    out.push(check_audit(&cfg));
    out.push(check_limiter(&cfg));
    out.push(check_metrics(&cfg));
    out.push(check_runner(&cfg));
    out.push(check_port(&cfg));
    out.push(check_llm());
    out.push(check_cluster(&cfg));

    out
}

/// 目录可写性：真写一个临时文件再删，不信"目录存在"这种弱证据。
///
/// `metadata().permissions().readonly()` 在 Windows 上对"目录"基本没有信息量，
/// 而"能创建文件"才是后续快照/审计真正需要的能力。
fn writable(dir: &Path) -> Result<(), String> {
    if let Err(e) = std::fs::create_dir_all(dir) {
        return Err(format!("无法创建：{e}"));
    }
    let probe = dir.join(".doctor-probe");
    let mut f = std::fs::File::create(&probe).map_err(|e| format!("不可写：{e}"))?;
    let wrote = f.write_all(b"ok").and_then(|_| f.flush());
    drop(f);
    let _ = std::fs::remove_file(&probe);
    wrote.map_err(|e| format!("写入失败：{e}"))
}

fn check_data_dir(cfg: &Config) -> Check {
    match writable(&cfg.data_dir) {
        Ok(()) => Check::new("数据目录", Level::Ok, format!("{} 可写", cfg.data_dir.display())),
        Err(e) => Check::new("数据目录", Level::Error, format!("{} {e}", cfg.data_dir.display()))
            .hint("快照与审计都落在这里；不可写时启动会直接失败"),
    }
}

fn check_storage(cfg: &Config) -> Check {
    let kind = format!("{:?}", cfg.storage).to_lowercase();
    let snap = cfg.snapshot_path();
    let exists = snap.exists();
    let size = std::fs::metadata(&snap).map(|m| m.len()).unwrap_or(0);
    let detail = format!(
        "storage={kind} snapshot={}{}",
        snap.display(),
        if exists {
            format!("（已存在，{size} 字节）")
        } else {
            "（尚未生成，首次写入时创建）".to_string()
        }
    );
    match kind.as_str() {
        "memory" => Check::new("存储槽位", Level::Warn, detail)
            .hint("纯内存后端：进程退出即清空。压测/演示可以，日常请设 SA_STORAGE=json"),
        _ => Check::new("存储槽位", Level::Ok, detail),
    }
}

fn check_audit(cfg: &Config) -> Check {
    let kind = format!("{:?}", cfg.audit).to_lowercase();
    match kind.as_str() {
        "file" => {
            let path = cfg
                .audit_file
                .clone()
                .unwrap_or_else(|| cfg.data_dir.join("audit.jsonl"));
            let parent = path.parent().unwrap_or(&cfg.data_dir);
            let detail = match writable(parent) {
                Ok(()) => format!("audit=file path={}", path.display()),
                Err(e) => return Check::new("审计槽位", Level::Error,
                    format!("audit=file 但目录不可写：{} {e}", parent.display()))
                    .hint("审计写不进去时 record() 不返回错误，会静默丢失；必须修"),
            };
            Check::new("审计槽位", Level::Ok, detail)
        }
        _ => Check::new(
            "审计槽位",
            Level::Warn,
            format!("audit={kind}（有界内存，不落盘）"),
        )
        .hint("事后无法回查谁在什么时候做了什么；需要留证请设 SA_AUDIT=file"),
    }
}

fn check_limiter(cfg: &Config) -> Check {
    let kind = format!("{:?}", cfg.limiter).to_lowercase();
    if kind == "noop" {
        Check::new("限流槽位", Level::Warn, "limiter=noop（一律放行）")
            .hint("提交风控形同虚设；需要限流请设 SA_LIMITER=memory")
    } else {
        Check::new("限流槽位", Level::Ok, format!("limiter={kind}"))
    }
}

fn check_metrics(cfg: &Config) -> Check {
    let kind = format!("{:?}", cfg.metrics_export).to_lowercase();
    if kind == "noop" {
        Check::new("指标槽位", Level::Warn, "metrics=noop（/metrics 只有空壳）")
            .hint("需要观测请设 SA_METRICS_EXPORT=prometheus")
    } else {
        Check::new("指标槽位", Level::Ok, format!("metrics={kind}"))
    }
}

fn check_runner(cfg: &Config) -> Check {
    let kind = format!("{:?}", cfg.runner).to_lowercase();
    if kind != "process" {
        return Check::new(
            "执行槽位",
            Level::Ok,
            format!("runner={kind}（确定性模拟，零外部依赖）"),
        )
        .hint("要真实调用脚本请设 SA_RUNNER=process 并配 SA_RUNNER_DIR");
    }
    match cfg.runner_dir.as_deref() {
        None => Check::new("执行槽位", Level::Error, "runner=process 但未设 SA_RUNNER_DIR")
            .hint("没有脚本目录，真实执行会一步都跑不起来"),
        Some(dir) if !dir.is_dir() => Check::new(
            "执行槽位",
            Level::Error,
            format!("runner=process 但目录不存在：{}", dir.display()),
        ),
        Some(dir) => Check::new(
            "执行槽位",
            Level::Ok,
            format!("runner=process dir={}", dir.display()),
        ),
    }
}

/// 端口三种状态。区分"被自己占"和"被别人占"是有意义的：
/// 前者说明服务已经在跑（可以直接用），后者才是真冲突。
enum PortState {
    Free,
    Ours(u16),
    Foreign(String),
}

fn probe_port(addr: &str) -> PortState {
    // 先试绑：能绑上就是空闲，立刻释放，不改变任何状态。
    if let Ok(l) = TcpListener::bind(addr) {
        drop(l);
        return PortState::Free;
    }
    // 绑不上：先确认是不是本服务。/healthz 免鉴权，是最轻的活体判据。
    match http_status(addr, "/healthz") {
        Some(code) if code == 200 => PortState::Ours(code),
        Some(code) => PortState::Foreign(format!("端口已占用，/healthz 返回 {code}")),
        None => PortState::Foreign("端口已占用且不响应 HTTP".into()),
    }
}

/// 最小 HTTP/1.1 GET：只为拿状态码。
///
/// 不引 HTTP 客户端 crate 的理由：`doctor` 必须在"依赖都没装好"的环境里也能跑，
/// 而这里只需要读响应首行。请求头写全 `Connection: close`，让服务端主动断开，
/// 免得读到 keep-alive 的超时才返回。
fn http_status(addr: &str, path: &str) -> Option<u16> {
    let sock = addr.to_socket_addrs().ok()?.next()?;
    let mut stream = TcpStream::connect_timeout(&sock, Duration::from_millis(800)).ok()?;
    let _ = stream.set_read_timeout(Some(Duration::from_millis(1500)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(1500)));
    let req = format!("GET {path} HTTP/1.1\r\nHost: {addr}\r\nConnection: close\r\n\r\n");
    stream.write_all(req.as_bytes()).ok()?;
    let mut buf = [0u8; 256];
    let n = stream.read(&mut buf).ok()?;
    let head = String::from_utf8_lossy(&buf[..n]);
    let line = head.lines().next()?;
    // "HTTP/1.1 200 OK" → 200
    line.split_whitespace().nth(1)?.parse().ok()
}

fn check_port(cfg: &Config) -> Check {
    match probe_port(&cfg.bind_addr) {
        PortState::Free => {
            Check::new("监听端口", Level::Ok, format!("{} 空闲，可以直接启动", cfg.bind_addr))
        }
        PortState::Ours(code) => Check::new(
            "监听端口",
            Level::Skip,
            format!("{} 上已有本服务在跑（/healthz {code}）", cfg.bind_addr),
        )
        .hint("要再起一个实例请改 SA_BIND；要重启请先停掉现有进程"),
        PortState::Foreign(why) => {
            Check::new("监听端口", Level::Error, format!("{} {why}", cfg.bind_addr))
                .hint("启动会 bind 失败；换端口（SA_BIND）或先关掉占用者")
        }
    }
}

fn check_llm() -> Check {
    let base = std::env::var("SA_LLM_BASE").unwrap_or_default();
    let model = std::env::var("SA_LLM_MODEL").unwrap_or_default();
    if base.trim().is_empty() && model.trim().is_empty() {
        return Check::new("模型端点", Level::Skip, "未配置 SA_LLM_BASE / SA_LLM_MODEL")
            .hint("不配则不外发模型调用；本地模型示例：SA_LLM_BASE=http://127.0.0.1:11434");
    }
    if base.trim().is_empty() || model.trim().is_empty() {
        // 半配是最危险的状态：看着"配了模型"，实际每次调用都退化成占位。
        return Check::new(
            "模型端点",
            Level::Error,
            format!("只配了一半（base={:?} model={:?}）", base, model),
        )
        .hint("SA_LLM_BASE 与 SA_LLM_MODEL 必须同时给，缺一不外发");
    }
    // 从 URL 里抠 host:port：容忍带不带 scheme、带不带路径。
    let host_port = base
        .trim_start_matches("http://")
        .trim_start_matches("https://")
        .split('/')
        .next()
        .unwrap_or("")
        .to_string();
    let host_port = if host_port.contains(':') {
        host_port
    } else {
        format!("{host_port}:80")
    };
    match host_port.to_socket_addrs().ok().and_then(|mut a| a.next()) {
        Some(sock) => match TcpStream::connect_timeout(&sock, Duration::from_millis(500)) {
            Ok(_) => Check::new(
                "模型端点",
                Level::Ok,
                format!("model={model} endpoint={base} 可达"),
            )
            .hint("这里只验 TCP 可达，不验模型是否已拉取；真跑一次请用门禁 18"),
            Err(e) => Check::new(
                "模型端点",
                Level::Warn,
                format!("model={model} endpoint={base} 连不上：{e}"),
            )
            .hint("本地模型服务没起时，相关断言会退化成占位输出"),
        },
        None => Check::new("模型端点", Level::Warn, format!("endpoint 无法解析：{base}"))
            .hint("检查 SA_LLM_BASE 是否写成了 host:port 形式"),
    }
}

fn check_cluster(cfg: &Config) -> Check {
    if !cfg.cluster_enabled {
        return Check::new("集群心跳", Level::Skip, "已关闭（SA_CLUSTER=off）")
            .hint("/cluster 只报本节点");
    }
    match writable(&cfg.cluster_dir) {
        Ok(()) => {
            let peers = std::fs::read_dir(&cfg.cluster_dir)
                .map(|rd| {
                    rd.filter_map(|e| e.ok())
                        .filter(|e| e.path().extension().is_some_and(|x| x == "json"))
                        .count()
                })
                .unwrap_or(0);
            Check::new(
                "集群心跳",
                Level::Ok,
                format!(
                    "node_id={} dir={} 可写，目录内 {} 个心跳文件",
                    cfg.node_id,
                    cfg.cluster_dir.display(),
                    peers
                ),
            )
            .hint("心跳文件数包含自己；多节点要确认看到的是同一个共享目录")
        }
        Err(e) => Check::new(
            "集群心跳",
            Level::Error,
            format!("dir={} {e}", cfg.cluster_dir.display()),
        )
        .hint("心跳写不进去时本节点对兄弟节点等于不存在"),
    }
}

fn print_text(checks: &[Check], code: i32, strict: bool) {
    println!("personal-super-agents doctor  v{}", env!("CARGO_PKG_VERSION"));
    println!("{}", "=".repeat(78));
    for c in checks {
        println!("[{}] {:<10} {}", c.level.tag(), c.name, c.detail);
        if let Some(h) = &c.hint {
            println!("       └─ {h}");
        }
    }
    let count = |lv: Level| checks.iter().filter(|c| c.level == lv).count();
    println!("{}", "=".repeat(78));
    println!(
        "合计 {} 项 | OK {} | WARN {} | SKIP {} | ERR {}",
        checks.len(),
        count(Level::Ok),
        count(Level::Warn),
        count(Level::Skip),
        count(Level::Error)
    );
    let tail = match code {
        0 if count(Level::Warn) > 0 => format!(
            "结论：可以启动，但有 {} 条建议（{}）",
            count(Level::Warn),
            if strict { "--strict 下已记为失败" } else { "加 --strict 可让它们失败" }
        ),
        0 => "结论：可以启动，未发现问题".to_string(),
        1 => format!("结论：--strict 下有 {} 条 WARN 被当作失败", count(Level::Warn)),
        _ => "结论：存在阻断启动的问题，先修 ERR 项".to_string(),
    };
    println!("{tail}");
}

fn print_json(checks: &[Check], code: i32) {
    let items: Vec<serde_json::Value> = checks
        .iter()
        .map(|c| {
            serde_json::json!({
                "name": c.name,
                "level": c.level.as_str(),
                "detail": c.detail,
                "hint": c.hint,
            })
        })
        .collect();
    let count = |lv: Level| checks.iter().filter(|c| c.level == lv).count();
    let doc = serde_json::json!({
        "version": env!("CARGO_PKG_VERSION"),
        "exit_code": code,
        "checks": items,
        "summary": {
            "total": checks.len(),
            "ok": count(Level::Ok),
            "warn": count(Level::Warn),
            "skip": count(Level::Skip),
            "error": count(Level::Error),
        }
    });
    // 手写一次 pretty：doctor 不引任何新依赖，serde_json 已在核心依赖里。
    println!("{}", serde_json::to_string_pretty(&doc).unwrap_or_else(|_| "{}".into()));
}
