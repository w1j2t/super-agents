//! 运行时配置：全部通过环境变量注入，启动即校验，禁止硬编码密钥。

use crate::obs::audit::AuditKind;
use crate::obs::metrics::MetricsKind;
use crate::orchestration::runners::RunnerKind;
use crate::security::ratelimit::LimiterKind;
use crate::store::backends::StorageKind;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone)]
pub struct Config {
    pub bind_addr: String,
    /// 是否采信反向代理传来的 `X-Forwarded-For` / `X-Real-IP`（`SA_TRUST_PROXY`，默认**否**）。
    ///
    /// 默认不信任的原因：这两个头由客户端自由填写。直接采信首值，等于把"限流按 IP 分桶"
    /// 变成"按客户端自报的字符串分桶"——换个头就换一个桶，配额形同虚设；还能反过来伪造
    /// 别人的 IP，把锁定/限流记到受害者头上。只有当本进程确实挂在可信反代后面
    /// （且反代已经用 `X-Forwarded-For` 覆盖而非追加）时才该打开。
    pub trust_proxy: bool,
    pub data_dir: PathBuf,
    pub jwt_secret: Vec<u8>,
    pub access_ttl_secs: i64,
    pub refresh_ttl_secs: i64,
    pub pbkdf2_iterations: u32,
    pub snapshot_interval_secs: u64,
    /// 写后落盘窗口（毫秒）：变更发生后最长多久必须落盘。越小越不易丢数据，越大越省 IO。
    pub flush_interval_ms: u64,
    /// 写穿开关（`SA_FLUSH_ON_WRITE`，默认开）：关键数据（任务、记忆）一落库就立刻刷盘，
    /// 不等 500ms 的定时窗口。个人单用户系统写入频率极低，这点 IO 换"进程被杀不丢刚提交的任务"很划算。
    pub flush_on_write: bool,
    pub max_body_bytes: usize,
    pub cors_allowed_origins: Vec<String>,
    pub allow_registration: bool,
    /// 是否允许在**非回环监听**下保持注册开放（`SA_ALLOW_REGISTRATION_REMOTE`，默认否）。
    ///
    /// 三件事单独看都合理，叠起来就是提权链：绑定 `0.0.0.0`（局域网可达）+ 注册默认开放 +
    /// 首个注册用户直接拿到 `owner`。等于谁先扫到这个端口，谁就成为最高权限。
    /// 默认值不改（个人单机自举就是靠"第一个注册的人是 owner"），但要求放开之前
    /// 显式承认一次：不显式设置本开关又绑了非回环地址，启动直接报错，不给"静默危险默认"。
    pub allow_remote_registration: bool,
    pub task_retry_limit: u32,
    /// 单任务失败步骤熔断阈值（`SA_MAX_FAILED_STEPS`，默认派生自
    /// `executor::MAX_FAILED_STEPS`，夹在 1..=32）。
    ///
    /// 为什么必须是配置：常量写 3、装配处写 2 的双源，会让"测试验的是失败 3 步熔断、
    /// 线上实际 2 步就熔断"。这种差异不会让任何断言变红，却让测试结论与生产行为对不上。
    /// 默认值只允许从常量派生，不准在两处各写一个数字。
    pub max_failed_steps: u32,
    /// 同一拓扑层内允许并行的步骤数（`SA_STEP_CONCURRENCY`，默认 1 = 严格串行）。
    ///
    /// 上限夹在 1..=8：再高也没有意义（单任务计划本身只有 2~3 路扇出），
    /// 只会让误配置把机器一次性拖死。
    pub step_concurrency: usize,
    pub task_budget_tokens: u64,
    /// 每用户每小时的提交配额（`SA_TASK_SUBMIT_QUOTA_PER_HOUR`，默认 60）。
    /// 写成配置而不是写死常量：个人用时批量导入很容易超 60，调大不该改代码重编译。
    pub task_submit_quota_per_hour: u64,
    /// 后台异步任务并发上限（`SA_ASYNC_MAX_RUNNING`，默认 8）
    pub async_max_running: usize,
    /// Prometheus 抓取专用静态令牌（设置后 /metrics 可用它免登录抓取；留空则仅管理员令牌可读）
    pub metrics_token: Option<String>,
    /// 存储后端（可插拔部件）：json = 快照持久化，memory = 纯内存
    pub storage: StorageKind,
    /// 执行后端（可插拔部件）：sim = 确定性模拟，process = 真实调用脚本
    pub runner: RunnerKind,
    /// 真实执行后端使用的脚本目录（SA_RUNNER_DIR）
    pub runner_dir: Option<PathBuf>,
    /// 审计出口（可插拔部件）：memory = 有界内存，file = 内存 + JSONL 落盘
    pub audit: AuditKind,
    /// 文件审计出口的落盘路径（SA_AUDIT_FILE，默认 <data_dir>/audit.jsonl）
    pub audit_file: Option<PathBuf>,
    /// 审计队列容量（SA_AUDIT_CAPACITY，默认 2000）
    pub audit_capacity: usize,
    /// 单步执行硬超时（秒）
    pub runner_timeout_secs: u64,
    /// 指标导出部件（可插拔部件）：prometheus = 文本，json = JSON 快照，noop = 关闭
    pub metrics_export: MetricsKind,
    /// 限流部件（可插拔部件）：memory = 进程内令牌桶，noop = 一律放行
    pub limiter: LimiterKind,
    pub env: String,
    /// 集群心跳开关（`SA_CLUSTER`，默认开）。
    ///
    /// 关掉后本进程**不写也不读**心跳文件，`/cluster` 只报自己一个节点——
    /// 这是"单机跑测试/压测不希望留下目录垃圾"时的唯一开关，
    /// 不是"多节点功能降级"（多节点靠共享目录，不靠这个开关）。
    pub cluster_enabled: bool,
    /// 集群心跳目录（`SA_CLUSTER_DIR`，缺省 `<data_dir>/cluster`）。
    ///
    /// **这是多节点之间唯一的共享物**：每台机器把自己的心跳写成
    /// `<cluster_dir>/<node_id>.json`，别人读这一份来知道"还有谁活着"。
    /// 为什么用共享目录而不是 HTTP 互探：个人部署的现实是"两三台机器 + 一个同步盘/NAS"，
    /// 共享目录零依赖、零新 crate、断网也不会互相拖死；真要跨机房再换实现，
    /// 换的只是本模块，`/cluster` 的响应形状不动。
    pub cluster_dir: PathBuf,
    /// 心跳超时（`SA_CLUSTER_TTL_SECS`，默认 20）：
    /// 超过这么久没更新的 peer 标记为 `stale`，不再计入集群汇总负载。
    pub cluster_ttl_secs: u64,
    /// 心跳周期（`SA_CLUSTER_HEARTBEAT_SECS`，默认 5）。
    /// 必须显著小于 TTL（默认 4 倍余量），否则一次慢盘抖动就会把自己判死。
    pub cluster_heartbeat_secs: u64,
    /// 集群节点标识（`SA_NODE_ID`，缺省 `<主机名>-<pid>`）。
    ///
    /// 为什么现在就加：单节点部署看着用不上，但它是"这条任务/这段审计是哪个进程写的"
    /// 的唯一锚点。等真上第二个节点才补，历史数据就没法回溯归属了。
    pub node_id: String,
    /// 阈值告警策略（阈值 / 最小样本量 / 去重窗口 / 风暴阈值）。
    ///
    /// 为什么单独成结构体而不是摊平成 9 个字段：告警是**一整条能力**，
    /// 它的参数要一起被门禁覆盖、一起被文档列出；摊平后很容易出现
    /// "新增了一条规则却忘了加对应阈值"，而编译器抓不到这种错。
    ///
    /// 取值来自 `SA_ALERT_*` 系列环境变量，非法值回落到默认并 warn（不让监控拖死业务）。
    pub alert: crate::obs::alert::AlertPolicy,
}

#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    #[error("环境变量 {0} 非法: {1}")]
    Invalid(&'static str, String),
    #[error("数据目录不可用: {0}")]
    DataDir(String),
}

impl Config {
    pub fn from_env() -> Result<Self, ConfigError> {
        let data_dir = PathBuf::from(std::env::var("SA_DATA_DIR").unwrap_or_else(|_| "data".into()));
        std::fs::create_dir_all(&data_dir)
            .map_err(|e| ConfigError::DataDir(format!("{}: {e}", data_dir.display())))?;

        // 环境名提到最前：有些默认值必须随环境走（例如审计出口），
        // 晚一步解析就会先落到错误默认值上，再想纠正只能靠"覆盖"而不是"默认"。
        let env_name = std::env::var("SA_ENV").unwrap_or_else(|_| "dev".into());

        let jwt_secret = match std::env::var("SA_JWT_SECRET") {
            Ok(s) if s.len() >= 32 => s.into_bytes(),
            Ok(_) => {
                return Err(ConfigError::Invalid(
                    "SA_JWT_SECRET",
                    "长度必须 >= 32 字符".into(),
                ))
            }
            Err(_) => load_or_create_secret(&data_dir)?,
        };

        // 心跳目录必须在 `data_dir` 被 move 进结构体**之前**算出来：
        // 它的缺省值就是 `<data_dir>/cluster`，属于"依赖 data_dir、但本身要先定型"的派生值。
        // 先 clone 一份 PathBuf 再 move 也能编过，但那是为了让编译器闭嘴多复制一次路径；
        // 调整顺序零成本，语义也更直白——派生值先算，所有权后交。
        let cluster_dir = parse_cluster_dir(&data_dir);

        // 监听面与注册面的交叉校验（见 `allow_remote_registration` 字段注释）：
        // 单独看每项配置都"合理"，只有放一起才是漏洞。放在这里而不是 handler 里，
        // 是因为启动失败（一句明确的报错）远好于"跑起来之后被人抢注 owner"。
        let bind_addr = std::env::var("SA_BIND").unwrap_or_else(|_| "127.0.0.1:8088".into());
        // FIX-002：默认**关闭**注册。原实现默认开放，且首个注册用户直接是 owner，
        // 任何能连上端口的进程抢在用户之前注册即成为最高权限（与 CORS 默认放开、
        // 快照损坏后空库自启动叠加就是零成本接管）。自举路径见 `services::auth::register`：
        // 库为空且无恢复标记时仍允许创建第一个账号，所以首装依然开箱可用。
        let allow_registration = parse_bool("SA_ALLOW_REGISTRATION", false)?;
        let allow_remote_registration = parse_bool("SA_ALLOW_REGISTRATION_REMOTE", false)?;
        if allow_registration && !allow_remote_registration && !bind_is_loopback(&bind_addr) {
            return Err(ConfigError::Invalid(
                "SA_BIND",
                format!(
                    "{bind_addr} 不是回环地址：本实例会对本机以外的网络可见，而注册是开着的、\
                    首个注册用户直接是 owner。二选一后再启动：\
                     ① 只在本机用 → 关掉注册（SA_ALLOW_REGISTRATION 默认就是关的）或去掉 SA_BIND（默认 127.0.0.1:8088）；\
                     ② 确实要在局域网/公网提供服务 → 显式设 SA_ALLOW_REGISTRATION_REMOTE=1"
                ),
            ));
        }

        Ok(Config {
            bind_addr,
            trust_proxy: parse_bool("SA_TRUST_PROXY", false)?,
            data_dir,
            jwt_secret,
            access_ttl_secs: parse_i64("SA_ACCESS_TTL_SECS", 1800)?,
            refresh_ttl_secs: parse_i64("SA_REFRESH_TTL_SECS", 604_800)?,
            pbkdf2_iterations: parse_u32("SA_PBKDF2_ITERATIONS", 600_000)?,
            snapshot_interval_secs: parse_u64("SA_SNAPSHOT_INTERVAL_SECS", 15)?,
            flush_interval_ms: parse_u64("SA_FLUSH_INTERVAL_MS", 500)?.clamp(50, 60_000),
            flush_on_write: parse_bool("SA_FLUSH_ON_WRITE", true)?,
            task_submit_quota_per_hour: parse_u64("SA_TASK_SUBMIT_QUOTA_PER_HOUR", 60)?
                .clamp(1, 100_000),
            async_max_running: parse_u64("SA_ASYNC_MAX_RUNNING", 8)?.clamp(1, 256) as usize,
            max_body_bytes: parse_usize("SA_MAX_BODY_BYTES", 256 * 1024)?,
            cors_allowed_origins: std::env::var("SA_CORS_ORIGINS")
                .unwrap_or_default()
                .split(',')
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .collect(),
            // 用上面校验过的同一个值，不再二次解析：校验的和生效的必须是同一个布尔，
            // 否则两处解析策略一变，就会出现"校验时是开的、生效时是关的"。
            allow_registration,
            allow_remote_registration,
            task_retry_limit: parse_u32("SA_TASK_RETRY_LIMIT", 2)?,
            max_failed_steps: parse_u32(
                "SA_MAX_FAILED_STEPS",
                crate::orchestration::executor::MAX_FAILED_STEPS,
            )?
            .clamp(1, 32),
            step_concurrency: parse_u32("SA_STEP_CONCURRENCY", 1)?.clamp(1, 8) as usize,
            task_budget_tokens: parse_u64("SA_TASK_BUDGET_TOKENS", 60_000)?,
            metrics_token: parse_metrics_token()?,
            storage: parse_storage()?,
            runner: parse_runner()?,
            runner_dir: parse_runner_dir(),
            audit: parse_audit(&env_name)?,
            audit_file: parse_audit_file(),
            audit_capacity: parse_usize("SA_AUDIT_CAPACITY", 2_000)?,
            runner_timeout_secs: parse_u64("SA_RUNNER_TIMEOUT_SECS", 20)?,
            metrics_export: parse_metrics_export()?,
            limiter: parse_limiter()?,
            node_id: parse_node_id(),
            // 默认关闭：进程内限流/锁定在单机是完整的，一旦默认开局就是集群，
        // 限流配额会被节点数线性放大（三方审查 H3）。集群要显式 SA_CLUSTER=1 加入。
        cluster_enabled: parse_bool("SA_CLUSTER", false)?,
            cluster_dir,
            cluster_ttl_secs: parse_u64("SA_CLUSTER_TTL_SECS", 20)?.clamp(5, 3_600),
            cluster_heartbeat_secs: parse_u64("SA_CLUSTER_HEARTBEAT_SECS", 5)?.clamp(1, 600),
            alert: crate::obs::alert::AlertPolicy::from_env(),
            env: env_name,
        })
    }

    /// 测试专用：小迭代次数（保证测试快速）+ 临时目录。
    pub fn for_test(data_dir: PathBuf) -> Self {
        Config {
            bind_addr: "127.0.0.1:0".into(),
            trust_proxy: false,
            data_dir,
            jwt_secret: b"test-secret-key-with-enough-entropy-0123456789".to_vec(),
            access_ttl_secs: 900,
            refresh_ttl_secs: 3600,
            pbkdf2_iterations: 1_000,
            snapshot_interval_secs: 3600,
            flush_interval_ms: 500,
            flush_on_write: true,
            task_submit_quota_per_hour: 60,
            async_max_running: 8,
            max_body_bytes: 256 * 1024,
            cors_allowed_origins: vec![],
            allow_registration: true,
            allow_remote_registration: false,
            task_retry_limit: 2,
            max_failed_steps: crate::orchestration::executor::MAX_FAILED_STEPS,
            step_concurrency: 1,
            task_budget_tokens: 60_000,
            metrics_token: None,
            storage: StorageKind::Json,
            runner: RunnerKind::Sim,
            runner_dir: None,
            audit: AuditKind::Memory,
            audit_file: None,
            audit_capacity: 2_000,
            runner_timeout_secs: 20,
            metrics_export: MetricsKind::Prometheus,
            limiter: LimiterKind::Memory,
            node_id: "test-node".into(),
            // 测试默认关闭集群心跳：单测会并行跑几十个实例，
            // 若都往同一个缺省目录写心跳，彼此会看见对方、断言随机器负载飘。
            // 需要测多节点的用例显式打开并给独立目录。
            cluster_enabled: false,
            cluster_dir: data_dir_for_cluster(),
            cluster_ttl_secs: 20,
            cluster_heartbeat_secs: 5,
            // 测试用默认策略（5 分钟去重、阈值按默认）：单测要验的是"逻辑对不对"，
            // 不是"阈值调得准不准"，所以不读环境变量，避免宿主机的 SA_ALERT_* 污染断言。
            alert: crate::obs::alert::AlertPolicy::default(),
            env: "test".into(),
        }
    }

    pub fn snapshot_path(&self) -> PathBuf {
        self.data_dir.join("db.json")
    }
}

fn load_or_create_secret(data_dir: &Path) -> Result<Vec<u8>, ConfigError> {
    let path = data_dir.join("secret.key");
    if let Ok(existing) = std::fs::read(&path) {
        if existing.len() >= 32 {
            warn_if_secret_too_open(&path);
            return Ok(existing);
        }
    }
    let mut buf = [0u8; 48];
    getrandom::getrandom(&mut buf)
        .map_err(|e| ConfigError::Invalid("SA_JWT_SECRET", format!("熵源不可用: {e}")))?;
    std::fs::write(&path, buf)
        .map_err(|e| ConfigError::DataDir(format!("{}: {e}", path.display())))?;
    // FIX-008：密钥文件按默认方式写出来就是"同机任何账户可读"，读走它等于能离线
    // 签出任意用户（含 owner）的 access token，服务端没有任何二次验证能拦。
    // 所以落盘后立刻收紧权限；收紧失败不拒启（别把新装机卡死），但必须留告警。
    tighten_secret_permissions(&path);
    tracing::warn!(
        path = %path.display(),
        "未提供 SA_JWT_SECRET，已生成随机密钥并持久化；生产环境请显式注入"
    );
    Ok(buf.to_vec())
}

/// FIX-008：把 `secret.key` 收紧成"仅当前用户可读"。
///
/// 未注入 `SA_JWT_SECRET` 时程序会自己生成 48 字节密钥并落盘；Windows 默认继承
/// 父目录 ACL、Unix 默认按 umask 放开。同机其他账户拿到这个文件即可伪造签名，
/// 因此落盘后立即按平台收紧。
fn tighten_secret_permissions(path: &Path) {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        match std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600)) {
            Ok(()) => tracing::info!(path = %path.display(), "secret.key 权限已收紧为 0600"),
            Err(e) => tracing::warn!(
                path = %path.display(), error = %e,
                "收紧 secret.key 权限失败：同机其他账户可能读到密钥并伪造令牌"
            ),
        }
    }
    #[cfg(windows)]
    {
        // std 没有 ACL API：用系统自带 icacls 去掉继承、只授予当前用户。
        // 失败不致命，但告警必须留——那一段就是可利用窗口。
        // 代价说清楚：如果服务将来改用别的账户跑（计划任务/SYSTEM/独立服务账户），
        // 它读不到这个文件就会起不来。那种部署请显式注入 SA_JWT_SECRET，
        // 而不是把这个文件的权限再放开一轮。
        let user = std::env::var("USERNAME").unwrap_or_default();
        if user.is_empty() {
            tracing::warn!(path = %path.display(), "无法确定当前用户名，secret.key 仍继承父目录 ACL");
            return;
        }
        match std::process::Command::new("icacls")
            .arg(path)
            .args(["/inheritance:r", "/grant:r"])
            .arg(format!("{user}:F"))
            .output()
        {
            Ok(out) if out.status.success() => {
                tracing::info!(path = %path.display(), "secret.key 权限已收紧为仅当前用户可读")
            }
            Ok(out) => tracing::warn!(
                path = %path.display(),
                code = ?out.status.code(),
                stderr = String::from_utf8_lossy(&out.stderr).trim(),
                "icacls 收紧 secret.key 权限失败：同机其他账户可能读到密钥并伪造令牌"
            ),
            Err(e) => tracing::warn!(
                path = %path.display(), error = %e,
                "icacls 不可用，secret.key 仍继承父目录 ACL：同机其他账户可能读到密钥"
            ),
        }
    }
    #[cfg(not(any(unix, windows)))]
    {
        let _ = path;
    }
}

/// FIX-008 复查：老部署的 `secret.key` 是放开权限写出来的。改完代码不等于
/// "这台机器已经安全"，所以每次启动读密钥时都复核一次，过宽就催人收紧。
fn warn_if_secret_too_open(path: &Path) {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if let Ok(meta) = std::fs::metadata(path) {
            let mode = meta.permissions().mode();
            if mode & 0o077 != 0 {
                tracing::warn!(
                    path = %path.display(), mode = format!("{:o}", mode & 0o777),
                    "secret.key 权限过宽（组/其他账户可读）：建议 chmod 600，或显式注入 SA_JWT_SECRET"
                );
            }
        }
    }
    #[cfg(not(unix))]
    {
        let _ = path;
    }
}

fn parse_i64(key: &'static str, default: i64) -> Result<i64, ConfigError> {
    match std::env::var(key) {
        Ok(v) => v.parse().map_err(|e| ConfigError::Invalid(key, format!("{e}"))),
        Err(_) => Ok(default),
    }
}

fn parse_u32(key: &'static str, default: u32) -> Result<u32, ConfigError> {
    match std::env::var(key) {
        Ok(v) => v.parse().map_err(|e| ConfigError::Invalid(key, format!("{e}"))),
        Err(_) => Ok(default),
    }
}

fn parse_u64(key: &'static str, default: u64) -> Result<u64, ConfigError> {
    match std::env::var(key) {
        Ok(v) => v.parse().map_err(|e| ConfigError::Invalid(key, format!("{e}"))),
        Err(_) => Ok(default),
    }
}

/// 抓取令牌：留空视为未启用；启用时要求 >= 24 字符，避免弱口令。
fn parse_metrics_token() -> Result<Option<String>, ConfigError> {
    match std::env::var("SA_METRICS_TOKEN") {
        Ok(v) if v.trim().is_empty() => Ok(None),
        Ok(v) if v.trim().len() >= 24 => Ok(Some(v.trim().to_string())),
        Ok(_) => Err(ConfigError::Invalid(
            "SA_METRICS_TOKEN",
            "长度必须 >= 24 字符（留空表示不启用）".into(),
        )),
        Err(_) => Ok(None),
    }
}

/// 节点标识：`SA_NODE_ID` 优先，缺省 `<主机名>-<pid>`。
///
/// 缺省值**故意**带上 pid：同一台机器并行跑两个进程（调试一个、正式一个）时，
/// 只报主机名会让两个节点在集群视图里重合，谁也说不清某条任务到底被谁扛了。
/// 代价是"重启后 node_id 会变"，这在单机部署里是可接受的——它标识的是**进程**，
/// 要标识机器请显式设置 `SA_NODE_ID`。
///
/// 合法性只要求非空、且不含空白（`node_id` 会成为指标标签与日志字段，
/// 带空格或换行的值会把日志切成两行，让追查变成猜谜）。
fn parse_node_id() -> String {
    let fallback = || {
        let host = std::env::var("COMPUTERNAME")
            .or_else(|_| std::env::var("HOSTNAME"))
            .unwrap_or_else(|_| "localhost".into());
        format!("{}-{}", host.trim(), std::process::id())
    };
    match std::env::var("SA_NODE_ID") {
        Ok(v) if v.trim().is_empty() => fallback(),
        Ok(v) => {
            let cleaned: String = v.trim().chars().filter(|c| !c.is_whitespace()).collect();
            if cleaned.is_empty() {
                fallback()
            } else {
                cleaned
            }
        }
        Err(_) => fallback(),
    }
}

/// 存储后端选择：留空默认 json；非法值直接拒绝启动（避免"以为落盘其实没落"）。
fn parse_storage() -> Result<StorageKind, ConfigError> {
    match std::env::var("SA_STORAGE") {
        Ok(v) if v.trim().is_empty() => Ok(StorageKind::Json),
        Ok(v) => StorageKind::parse(&v).ok_or_else(|| {
            ConfigError::Invalid(
                "SA_STORAGE",
                format!("可选 {}，收到 {v}", StorageKind::ALL.join(" / ")),
            )
        }),
        Err(_) => Ok(StorageKind::Json),
    }
}

/// 执行后端选择：留空默认 sim；非法值直接拒绝启动。
fn parse_runner() -> Result<RunnerKind, ConfigError> {
    // 默认诚实：缺省后端是**真实执行**（process），不是模拟。
    // 历史上缺省 Sim，让"跑起来全绿但什么都没做"成了默认体验（三方审查 H2）。
    // 模拟必须由用户显式 `SA_RUNNER=sim` opt-in，供验收复现与演练使用。
    match std::env::var("SA_RUNNER") {
        Ok(v) if v.trim().is_empty() => Ok(RunnerKind::Process),
        Ok(v) => RunnerKind::parse(&v).ok_or_else(|| {
            ConfigError::Invalid(
                "SA_RUNNER",
                format!("可选 {}，收到 {v}", RunnerKind::ALL.join(" / ")),
            )
        }),
        Err(_) => Ok(RunnerKind::Process),
    }
}

/// 脚本目录：显式 `SA_RUNNER_DIR` 优先；未给出时自动发现随产物分发的 `workers/`
/// （见下方探测顺序）。给定的目录是否存在由执行后端初始化时校验（失败即拒启）。
fn parse_runner_dir() -> Option<PathBuf> {
    // 显式优先；未显式给出时按"随产物分发的 workers 目录"兜底，
    // 这样默认 process 后端开箱可用，而不是一启动就缺脚本目录。
    if let Ok(v) = std::env::var("SA_RUNNER_DIR") {
        if !v.trim().is_empty() {
            return Some(PathBuf::from(v.trim()));
        }
    }
    // 探测顺序：cwd 及其上层 → exe 所在目录及其上层。镜像里 exe 在
    // target-gnu/debug/、脚本目录在镜像根，只查一层等于白查。
    let mut bases: Vec<PathBuf> = Vec::new();
    if let Ok(cwd) = std::env::current_dir() {
        bases.push(cwd);
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(p) = exe.parent() {
            bases.push(p.to_path_buf());
        }
    }
    for base in bases {
        let mut dir = base;
        for _ in 0..3 {
            let cand = dir.join("workers");
            if cand.is_dir() {
                return Some(cand);
            }
            match dir.parent() {
                Some(p) => dir = p.to_path_buf(),
                None => break,
            }
        }
    }
    None
}

/// 审计出口。
///
/// 未显式设置 `SA_AUDIT` 时，默认值随环境走：
///   * `prod` → `file`：审计的唯一用途是**事后回溯**，默认不落盘等于没有审计。
///     个人部署一样需要留痕——出事那天再想起来配，那一周的记录已经没了。
///   * `dev` / `test` / 其它 → `memory`：不写盘、不污染工作目录，跑测试更干净。
///
/// 显式设了 `SA_AUDIT` 就一律听显式的，哪怕与上面的推荐相反（启动会打印告警）。
fn parse_audit(env: &str) -> Result<AuditKind, ConfigError> {
    match std::env::var("SA_AUDIT") {
        Ok(v) if !v.trim().is_empty() => AuditKind::parse(&v).ok_or_else(|| {
            ConfigError::Invalid(
                "SA_AUDIT",
                format!("可选值: {}", AuditKind::ALL.join("|")),
            )
        }),
        _ => Ok(default_audit_for(env)),
    }
}

/// `SA_AUDIT` 未显式设置时的默认出口。抽成纯函数是为了能直接测——
/// 放进 `parse_audit` 里就得靠改全局环境变量来测，那类测试互相干扰、时好时坏。
fn default_audit_for(env: &str) -> AuditKind {
    if is_prod(env) {
        AuditKind::File
    } else {
        AuditKind::Memory
    }
}

/// 生产环境的判定。大小写不敏感，`production` 与 `prod` 等价——
/// 这类"换个写法就换了行为"的地方最容易出错，宁可宽一点。
fn is_prod(env: &str) -> bool {
    matches!(env.trim().to_ascii_lowercase().as_str(), "prod" | "production")
}

fn parse_audit_file() -> Option<PathBuf> {
    std::env::var("SA_AUDIT_FILE")
        .ok()
        .filter(|v| !v.trim().is_empty())
        .map(PathBuf::from)
}

/// 指标导出部件选择：留空默认 prometheus；非法值拒绝启动。
fn parse_metrics_export() -> Result<MetricsKind, ConfigError> {
    match std::env::var("SA_METRICS_EXPORT") {
        Ok(v) if v.trim().is_empty() => Ok(MetricsKind::Prometheus),
        Ok(v) => MetricsKind::parse(&v).ok_or_else(|| {
            ConfigError::Invalid(
                "SA_METRICS_EXPORT",
                format!("可选 {}，收到 {v}", MetricsKind::ALL.join(" / ")),
            )
        }),
        Err(_) => Ok(MetricsKind::Prometheus),
    }
}

/// 限流部件选择：留空默认 memory；非法值拒绝启动（避免"以为有限流其实没限"）。
fn parse_limiter() -> Result<LimiterKind, ConfigError> {
    match std::env::var("SA_LIMITER") {
        Ok(v) if v.trim().is_empty() => Ok(LimiterKind::Memory),
        Ok(v) => LimiterKind::parse(&v).ok_or_else(|| {
            ConfigError::Invalid(
                "SA_LIMITER",
                format!("可选 {}，收到 {v}", LimiterKind::ALL.join(" / ")),
            )
        }),
        Err(_) => Ok(LimiterKind::Memory),
    }
}

fn parse_usize(key: &'static str, default: usize) -> Result<usize, ConfigError> {
    match std::env::var(key) {
        Ok(v) => v.parse().map_err(|e| ConfigError::Invalid(key, format!("{e}"))),
        Err(_) => Ok(default),
    }
}

fn parse_bool(key: &'static str, default: bool) -> Result<bool, ConfigError> {
    match std::env::var(key) {
        Ok(v) => match v.to_ascii_lowercase().as_str() {
            "1" | "true" | "yes" | "on" => Ok(true),
            "0" | "false" | "no" | "off" => Ok(false),
            other => Err(ConfigError::Invalid(key, format!("无法解析布尔值 {other}"))),
        },
        Err(_) => Ok(default),
    }
}

/// `SA_BIND` 是否只对本机可见（`127.0.0.1` / `::1` / `localhost`）。
///
/// 只有"确定是回环"才算安全：`0.0.0.0`、`::`、局域网 IP、以及任何解析不出来的写法
/// 一律按"对外可见"处理。宁可让一个回环别名被误判成对外（用户加一个环境变量即可），
/// 也不能把 0.0.0.0 误判成回环——后者的代价是整台机器开放给局域网。
pub fn bind_is_loopback(addr: &str) -> bool {
    if let Ok(sa) = addr.parse::<std::net::SocketAddr>() {
        return sa.ip().is_loopback();
    }
    let host = addr.rsplit_once(':').map(|(h, _)| h).unwrap_or(addr);
    let host = host.trim_start_matches('[').trim_end_matches(']');
    host.eq_ignore_ascii_case("localhost")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_real_loopback_counts_as_loopback() {
        // 判错的代价不对称：把 0.0.0.0 当回环 = 整个局域网可注册并抢 owner。
        for ok in ["127.0.0.1:8088", "127.1.2.3:9000", "[::1]:8088", "localhost:8088"] {
            assert!(bind_is_loopback(ok), "{ok} 应判为回环");
        }
        for bad in ["0.0.0.0:8088", "[::]:8088", "192.168.1.9:8088", "10.0.0.2:80", "example.com:8088", "8088"] {
            assert!(!bind_is_loopback(bad), "{bad} 不应判为回环");
        }
    }
    #[test]
    fn max_failed_steps_defaults_to_executor_constant() {
        // 熔断阈值的默认值必须与常量同源：曾出现 state.rs 写死 2、常量写 3 的双源，
        // 测试验 3、线上跑 2。这条断言把"两处各写一个数"钉死在门禁里。
        let cfg = Config::for_test(std::env::temp_dir());
        assert_eq!(
            cfg.max_failed_steps,
            crate::orchestration::executor::MAX_FAILED_STEPS,
            "默认熔断阈值必须等于 executor::MAX_FAILED_STEPS"
        );
        assert!(cfg.max_failed_steps >= 1, "阈值不允许为 0，否则首次失败即熔断");
    }

    #[test]
    fn prod_defaults_to_durable_audit() {
        // 生产不落盘 = 没有审计。这条是硬要求，别在重构时被"优化"掉。
        assert_eq!(default_audit_for("prod"), AuditKind::File);
        assert_eq!(default_audit_for("production"), AuditKind::File);
        assert_eq!(default_audit_for("PROD"), AuditKind::File);
        assert_eq!(default_audit_for(" Prod "), AuditKind::File);
    }

    #[test]
    fn non_prod_defaults_to_memory_audit() {
        // 测试与本地开发不写盘：跑一遍测试不该在仓库里留下审计文件。
        assert_eq!(default_audit_for("dev"), AuditKind::Memory);
        assert_eq!(default_audit_for("test"), AuditKind::Memory);
        assert_eq!(default_audit_for("staging"), AuditKind::Memory);
        assert_eq!(default_audit_for(""), AuditKind::Memory);
    }

    #[test]
    fn is_prod_rejects_lookalikes() {
        // 前缀相近的值不能误判成生产，否则会意外把审计写到盘上。
        assert!(!is_prod("prodx"));
        assert!(!is_prod("production-test"));
        assert!(is_prod("Production"));
    }
}

/// 心跳目录：`SA_CLUSTER_DIR` 优先，缺省落在数据目录下的 `cluster/` 子目录。
///
/// 缺省放在 data_dir 里面而不是系统临时目录：心跳是"谁还在"的唯一凭证，
/// 放 %TEMP% 会被清理工具顺手删掉，表现为"节点莫名集体离线"。
fn parse_cluster_dir(data_dir: &Path) -> PathBuf {
    match std::env::var("SA_CLUSTER_DIR") {
        Ok(v) if !v.trim().is_empty() => PathBuf::from(v.trim()),
        _ => data_dir.join("cluster"),
    }
}

/// `for_test` 用的心跳目录。
///
/// 单测里 `Config` 是逐字段构造的，拿不到外层 `data_dir` 变量；
/// 单独抽一个函数只为让"测试默认不写心跳"这件事在字面上成立，
/// 且不会因为 `cluster_enabled=false` 而被误当作"目录可以随便填"。
fn data_dir_for_cluster() -> PathBuf {
    std::env::temp_dir().join("sa-cluster-unused")
}
