//! 真实执行后端：调用脚本目录下的白名单脚本。
//!
//! 安全边界（刻意做窄，宁可拒绝也不放行）：
//! 1. `agent_id` 只允许 `[A-Za-z0-9_-]`，杜绝路径注入；
//! 2. 脚本必须落在 `SA_RUNNER_DIR` **真实路径**之内（`canonicalize` 后比对前缀），杜绝 `../` 穿越；
//! 3. 只认 `.cmd` / `.bat` / `.ps1` / `.py` 四种扩展名；
//! 4. 单步硬超时（`SA_RUNNER_TIMEOUT_SECS`），超时即 kill，不留僵尸；
//! 5. 参数一律走环境变量，不拼接命令行字符串；
//! 6. 输出截断（保留尾部 4000 字符），避免日志被大输出冲垮。
//!
//! 脚本约定：退出码 0 = 成功，非 0 = 可重试失败；stdout 即步骤产出摘要。
//! 注入的环境变量：`SA_TASK_ID` / `SA_STEP_INDEX` / `SA_AGENT_ID` / `SA_PERMISSION` / `SA_ATTEMPT`。

use crate::orchestration::executor::{StepOutcome, StepRequest, StepRunner};
use std::io::Read;
use std::sync::mpsc;
use std::path::{Path, PathBuf};
use super::sandbox::{Job, JobLimits};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

/// Windows 上 `canonicalize` 会返回 `\\?\C:\...` 形式的 verbatim 路径，
/// 这类路径交给 `cmd /C` / `powershell -File` 会被判为"找不到路径"（退出码 1）。
/// 这里统一剥掉前缀：既保证前缀校验口径一致，又保证脚本能被真正启动。
fn simplify(path: PathBuf) -> PathBuf {
    let s = path.as_os_str().to_string_lossy().into_owned();
    if let Some(rest) = s.strip_prefix(r"\\?\UNC\") {
        PathBuf::from(format!(r"\\{rest}"))
    } else if let Some(rest) = s.strip_prefix(r"\\?\") {
        PathBuf::from(rest)
    } else {
        path
    }
}

/// 允许执行的脚本扩展名（顺序即优先级）。
/// 默认只认 `.py`：ps1/bat/cmd 等于把 shell 解释器交给脚本，一旦有人能写脚本
/// 就等于能在宿主机执行任意命令（三方审查 H1）。要跑 shell 脚本必须显式
/// 设 `SA_RUNNER_ALLOW_SHELL=1`，让"放开"成为一个有意识的动作。
const ALLOWED_EXT: [&str; 1] = ["py"];
const SHELL_EXT: [&str; 3] = ["cmd", "bat", "ps1"];

/// 当前允许的扩展名集合：默认 py-only，显式开启后兼容 shell 脚本。
fn allowed_exts() -> Vec<&'static str> {
    let mut v: Vec<&'static str> = ALLOWED_EXT.to_vec();
    if std::env::var("SA_RUNNER_ALLOW_SHELL").map(|s| s.trim() == "1").unwrap_or(false) {
        v.extend(SHELL_EXT);
    }
    v
}

/// 输出保留的最大字符数（按字符而非字节截断，避免切断 UTF-8）。
const OUTPUT_LIMIT: usize = 4_000;

/// 宿主环境 → worker 的透传白名单**前缀**。
///
/// 为什么单独留这个口子：worker 只收上面那 11 个 `SA_*`（刻意做窄，减少注入面），
/// 但"这一步要不要调外部模型"属于部署期决策，不该硬编码进枚举；
/// 更不能靠"子进程默认继承父进程全部环境"这种暗开关——那会把 `SA_AUTH_SECRET`
/// 一类凭据一并泄进脚本。用前缀而不是逐个变量名：以后加 `SA_LLM_TEMPERATURE`
/// 之类的调参项，不必再回来改这里，换模型部件也就只是换环境变量。
const ENV_PASSTHROUGH_PREFIXES: [&str; 1] = ["SA_LLM_"];

/// 单条透传值的长度上限（逐条截断，防止超长变量把子进程环境撑坏）。
const PASSTHROUGH_VALUE_LIMIT: usize = 512;

/// 该环境变量是否允许透传给 worker 脚本。
///
/// 只认**开头**匹配：`SA_LLM_MODEL` 放行，而 `XSA_LLM_MODEL` 必须拒绝——
/// 否则前缀检查会被"把前缀拼在中间"绕过去。
pub fn should_passthrough(key: &str) -> bool {
    ENV_PASSTHROUGH_PREFIXES
        .iter()
        .any(|prefix| key.starts_with(prefix))
}

/// 透传值的硬上限截断：**保证结果字符数不超过 `limit`**。
///
/// 不能复用 `head()`——它会额外追加 6 个字符的「…(已截断)」标记，
/// 于是"上限 512"实际变成 518。对提示词来说多 6 个字无所谓，
/// 但这里是当安全上限用的，突破上限就是 bug（单测已抓过一次）。
fn clip_env(value: &str, limit: usize) -> String {
    const MARK: &str = "…(已截断)";
    let count = value.chars().count();
    if count <= limit {
        return value.to_string();
    }
    let mark_len = MARK.chars().count();
    if limit <= mark_len {
        // 上限比标记还短：只截断，不标记（否则标记本身就超限）
        return value.chars().take(limit).collect();
    }
    let mut out: String = value.chars().take(limit - mark_len).collect();
    out.push_str(MARK);
    out
}

/// 从一组键值里挑出可透传项，逐条截断，并按 key 排序。
///
/// 排序不是为了好看：环境变量在 Windows 上无序，排序后"同一份配置跑两次"
/// 的注入列表才可逐字比对，排查时能直接 diff。
pub fn passthrough_envs<I>(source: I) -> Vec<(String, String)>
where
    I: IntoIterator<Item = (String, String)>,
{
    let mut out: Vec<(String, String)> = source
        .into_iter()
        .filter(|(key, _)| should_passthrough(key))
        .map(|(key, value)| (key, clip_env(&value, PASSTHROUGH_VALUE_LIMIT)))
        .collect();
    out.sort();
    out
}

/// 环境块安全预算（字节）。
///
/// Windows 给整个环境块的上限是 32KB（`CreateProcess` 的硬限制），超了不是报错，
/// 而是**静默截断**——任务照常"成功"，但目标文本只剩半截，用户拿到基于残缺目标的
/// 结果而系统不自知（三方审查 M3）。这里取 8KB 做安全线：留足余量给系统自带变量，
/// 又远小于 32KB，越线时改走文件通道而不是赌一把。
pub const ENV_BUDGET_BYTES: usize = 8 * 1024;

/// 超预算时改为文件传递的键：值可能极长、且下游 worker 支持 `*_FILE` 回退。
const SPILL_KEYS: [&str; 2] = ["SA_GOAL", "SA_CONTEXT_SUMMARY"];

/// 一组环境变量的估算字节数（`K=V\0` 是 Windows 的真实排布）。
fn env_bytes(envs: &[(String, String)]) -> usize {
    envs.iter().map(|(k, v)| k.len() + v.len() + 2).sum()
}

/// 超预算时把大字段落盘，返回（新环境变量表，落盘目录）。
///
/// 落盘目录由调用方在脚本跑完后删除：临时文件里是任务目标原文，不能在磁盘上久留。
fn spill_oversized_envs(
    envs: Vec<(String, String)>,
) -> (Vec<(String, String)>, Option<std::path::PathBuf>) {
    if env_bytes(&envs) <= ENV_BUDGET_BYTES {
        return (envs, None);
    }
    let dir = std::env::temp_dir().join(format!("sa_env_{}", uuid::Uuid::new_v4()));
    if std::fs::create_dir_all(&dir).is_err() {
        // 落盘失败不能假装没事：保持原样交给 CreateProcess，让截断风险显式暴露在
        // 审计日志里，而不是悄悄改道。
        tracing::warn!("环境块超过预算且临时目录创建失败，降级为环境变量直传");
        return (envs, None);
    }
    let mut out = Vec::with_capacity(envs.len() + SPILL_KEYS.len());
    let mut spilled = 0usize;
    for (k, v) in envs {
        if SPILL_KEYS.contains(&k.as_str()) && !v.is_empty() {
            let path = dir.join(format!("{k}.txt"));
            if std::fs::write(&path, v.as_bytes()).is_ok() {
                out.push((format!("{k}_FILE"), path.display().to_string()));
                // 原键留一条"已改道"的说明：脚本若只认 SA_GOAL 也能看出发生了什么
                out.push((k, format!("(已改道文件传递：{})", path.display())));
                spilled += 1;
                continue;
            }
        }
        out.push((k, v));
    }
    if spilled == 0 {
        let _ = std::fs::remove_dir_all(&dir);
        return (out, None);
    }
    tracing::info!(spilled, bytes = env_bytes(&out), "环境块超预算，大字段改走文件传递");
    (out, Some(dir))
}

/// 解析「按 agent 覆盖超时」的原始值，失败一律回落。
///
/// 抽成纯函数是为了能直接测边界：读环境变量写单测会和其他用例抢进程级状态，
/// 测试之间互相污染比不测更糟。
fn resolve_timeout(raw: Option<String>, fallback: Duration) -> Duration {
    match raw.as_deref().map(str::trim).and_then(|v| v.parse::<u64>().ok()) {
        Some(secs) if secs > 0 => Duration::from_secs(secs.clamp(1, 600)),
        // 未配、配成空串、配错、配 0 都回落：配置写错不该让任务跑不成
        _ => fallback,
    }
}

/// 「启用模型后默认给足 300s」的纯函数版本。
///
/// 单独抽出来的唯一理由是可测：直接读环境变量写单测会和其他用例抢进程级状态，
/// 测试之间互相污染比不测更糟（和 `resolve_timeout` 同样的取舍）。
fn model_aware_default(base: Duration, model_on: bool) -> Duration {
    if model_on {
        base.max(Duration::from_secs(300))
    } else {
        base
    }
}

pub struct ProcessRunner {
    dir: PathBuf,
    /// `canonicalize` 后的根目录，用于前缀校验
    root: PathBuf,
    timeout: Duration,
    python: String,
}

#[derive(Debug)]
pub struct RunOutput {
    pub exit_code: i32,
    pub stdout: String,
    pub stderr: String,
    pub elapsed_ms: u128,
    pub timed_out: bool,
}

#[derive(Debug, thiserror::Error)]
pub enum RunError {
    #[error("非法 agent 标识（只允许字母/数字/下划线/连字符）: {0}")]
    BadAgentId(String),
    #[error("脚本目录不可用: {0}")]
    BadDir(String),
    #[error("未找到该 agent 的脚本（默认只跑 .py；要跑 cmd/bat/ps1 需设 SA_RUNNER_ALLOW_SHELL=1）: {0}")]
    NotFound(String),
    #[error("脚本路径越界（必须位于配置目录内）: {0}")]
    Escape(String),
    #[error("启动脚本失败: {0}")]
    Spawn(String),
    #[error(
        "进程沙箱未能生效，已拒绝执行脚本（加固失效不做静默降级）。确需无沙箱运行请显式设 SA_RUNNER_SANDBOX=off"
    )]
    SandboxMissing,
}

/// 是否"必须"有进程沙箱。
///
/// FIX-005：Windows 下除非显式 `SA_RUNNER_SANDBOX=off`，否则不允许"挂不上作业照样跑"。
/// 非 Windows 平台本仓没有作业对象实现（`Job::attach` 恒 None），保持原有放行 + 告警口径，
/// 否则换个平台整个 runner 都不可用——那不是加固，是自断。
fn sandbox_required() -> bool {
    cfg!(windows) && !JobLimits::disabled()
}

impl ProcessRunner {
    pub fn new(dir: PathBuf, timeout_secs: u64) -> Result<Self, RunError> {
        let root = simplify(
            std::fs::canonicalize(&dir)
                .map_err(|e| RunError::BadDir(format!("{}: {e}", dir.display())))?,
        );
        if !root.is_dir() {
            return Err(RunError::BadDir(format!("不是目录: {}", root.display())));
        }
        let python = std::env::var("SA_PYTHON").unwrap_or_else(|_| "python".to_string());
        Ok(Self {
            dir: root.clone(),
            root,
            timeout: Duration::from_secs(timeout_secs.clamp(1, 600)),
            python,
        })
    }

    pub fn dir(&self) -> &Path {
        &self.dir
    }

    /// 解析脚本路径：白名单扩展名 + 真实路径必须落在根目录内。
    pub fn resolve(&self, agent_id: &str) -> Result<PathBuf, RunError> {
        if agent_id.is_empty()
            || !agent_id
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
        {
            return Err(RunError::BadAgentId(agent_id.to_string()));
        }
        for ext in allowed_exts() {
            let candidate = self.dir.join(format!("{agent_id}.{ext}"));
            if !candidate.is_file() {
                continue;
            }
            let real = simplify(
                std::fs::canonicalize(&candidate)
                    .map_err(|e| RunError::BadDir(format!("{}: {e}", candidate.display())))?,
            );
            if !real.starts_with(&self.root) {
                return Err(RunError::Escape(real.display().to_string()));
            }
            return Ok(real);
        }
        Err(RunError::NotFound(agent_id.to_string()))
    }

    /// 这一步实际可用的硬超时：全局默认 + 按 agent 覆盖。
    ///
    /// 为什么必须能按 agent 覆盖：规则实现的 worker 20s 够用，但接了模型之后
    /// 一次生成就是 75s 量级（实测 minicpm5-2b:f16 在 CPU 上 10.5 tok/s），
    /// 沿用全局 20s 会把"模型慢"误报成"步骤超时"，而且脚本是被 kill 的，
    /// 连一句解释都留不下来。覆盖键形如 `SA_RUNNER_TIMEOUT_DOC_AGENT`：
    /// agent_id 里的 `-` 转 `_` 再大写，因为环境变量名不允许连字符。
    fn timeout_for(&self, agent_id: &str) -> Duration {
        let key = format!(
            "SA_RUNNER_TIMEOUT_{}",
            agent_id.replace('-', "_").to_ascii_uppercase()
        );
        resolve_timeout(std::env::var(&key).ok(), self.model_aware_default())
    }

    /// 默认超时要随「是否启用模型」切换，而不是永远 20s。
    ///
    /// 实测证据 `docs/evidence/soak/soak-smoke4.jsonl`：code-agent 接上
    /// minicpm5-2b:f16 后单次要 100~300s，而默认 20s 让它每一轮都被 kill，
    /// 任务侧只看到 partially_succeeded + code_chars=0，看着像"代码写不出来"，
    /// 实际是"还没写完就被杀了"。上面注释里早就写了这个道理，但只做了
    /// 「可按 agent 覆盖」、没做「默认就够用」—— 用户不配环境变量就必然踩坑。
    ///
    /// 规则 agent 20s 依然够用；一旦配置了模型，任何 agent 都可能调模型，
    /// 所以统一抬到 300s。未配模型时维持原值，不多等一秒。
    fn model_on(&self) -> bool {
        std::env::var("SA_LLM_MODEL")
            .map(|v| !v.trim().is_empty())
            .unwrap_or(false)
    }

    fn model_aware_default(&self) -> Duration {
        model_aware_default(self.timeout, self.model_on())
    }

    /// 真实执行：带硬超时，stdout/stderr 由独立线程抽干，杜绝管道写满导致的假死。
    pub fn run_script(
        &self,
        agent_id: &str,
        envs: &[(String, String)],
    ) -> Result<RunOutput, RunError> {
        let timeout = self.timeout_for(agent_id);
        // 每次真执行都把"这一步给了多少秒、依据是什么"写进日志。
        // 为什么要这一行：超时是"最会伪装成别的问题"的故障——模型慢被杀，
        // 上层只看到 code_chars=0，读起来像"模型写不出代码"。有了实际生效值，
        // 排查时能一眼分清"配了没生效"和"配了但确实不够"。
        tracing::debug!(
            agent = agent_id,
            timeout_secs = timeout.as_secs(),
            base_secs = self.timeout.as_secs(),
            model_on = self.model_on(),
            "脚本执行超时预算"
        );
        let path = self.resolve(agent_id)?;
        let ext = path
            .extension()
            .and_then(|e| e.to_str())
            .unwrap_or_default()
            .to_ascii_lowercase();

        let mut cmd = match ext.as_str() {
            "py" => {
                let mut c = Command::new(&self.python);
                c.arg(&path);
                c
            }
            "ps1" => {
                let mut c = Command::new("powershell");
                c.arg("-NoProfile")
                    .arg("-NonInteractive")
                    .arg("-ExecutionPolicy")
                    .arg("Bypass")
                    .arg("-File")
                    .arg(&path);
                c
            }
            _ => {
                let mut c = Command::new("cmd");
                c.arg("/C").arg(&path);
                c
            }
        };
        cmd.current_dir(&self.dir)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        for (k, v) in envs {
            cmd.env(k, v);
        }

        let started = Instant::now();
        let mut child = cmd.spawn().map_err(|e| RunError::Spawn(e.to_string()))?;

        // 作业对象必须紧贴 spawn：中间多插一行，脚本就已经能在那一瞬间吃掉内存。
        // 挂不上不当作失败（沙箱是加固，不是执行前提），但必须留痕——
        // "以为受控、其实没受控"比"明确知道没受控"危险得多。
        let job = Job::attach(JobLimits::from_env());
        let sandboxed = match job.as_ref() {
            Some(j) => j.assign(&child),
            None => false,
        };
        match (sandboxed, job.as_ref()) {
            (true, Some(j)) => {
                let l = j.limits();
                tracing::debug!(
                    agent = agent_id,
                    mem_mb = l.mem_mb,
                    max_procs = l.max_procs,
                    cpu_secs = l.cpu_secs,
                    "脚本已挂入作业对象（内存 / 进程数上限生效）"
                );
            }
            _ => {
                // FIX-005：挂不上作业不再"带病跑"。
                // 原实现只打一条 warn 继续执行，等于"脚本已经在跑、限制没有"；
                // 而 spawn 到 assign 之间本来就是逃逸窗口（脚本能在这一瞬拉满内存、起进程树）。
                // 现在分三种：显式关掉 / 非 Windows 平台 → 放行但留痕；Windows 且未显式关闭 → 当场杀掉。
                if sandbox_required() {
                    let _ = child.kill();
                    let _ = child.wait();
                    tracing::error!(
                        agent = agent_id,
                        "进程沙箱未能生效：已终止本次脚本执行（加固失效不做静默降级）"
                    );
                    return Err(RunError::SandboxMissing);
                }
                tracing::warn!(
                    agent = agent_id,
                    "未启用进程沙箱：本步脚本不受内存 / 进程数上限约束（检查 SA_RUNNER_SANDBOX 与作业对象创建是否成功）"
                );
            }
        }

        // 抽干线程带"有界保留 + 继续排空"：超过上限的部分不留在内存里，
        // 但仍然读走——只截断不排空会把管道写满，子进程写日志时反过来卡死（假死的老坑）。
        let out_rx = spawn_reader(child.stdout.take());
        let err_rx = spawn_reader(child.stderr.take());

        let mut timed_out = false;
        let status = loop {
            match child.try_wait() {
                Ok(Some(s)) => break Some(s),
                Ok(None) => {
                    if started.elapsed() >= timeout {
                        let _ = child.kill();
                        let _ = child.wait();
                        timed_out = true;
                        break None;
                    }
                    std::thread::sleep(Duration::from_millis(20));
                }
                Err(e) => return Err(RunError::Spawn(e.to_string())),
            }
        };

        // 进程已退出：先关作业句柄，连带收掉它可能残留的孙进程。
        // 顺序很要紧——孙进程会继承管道写端，只要它还活着，下面等 EOF 就一直等。
        drop(job);

        let stdout = finish_reader(out_rx);
        let stderr = finish_reader(err_rx);

        Ok(RunOutput {
            exit_code: status.and_then(|s| s.code()).unwrap_or(-1),
            stdout: tail(&stdout, OUTPUT_LIMIT),
            stderr: tail(&stderr, OUTPUT_LIMIT),
            elapsed_ms: started.elapsed().as_millis(),
            timed_out,
        })
    }
}

/// 单个流保留上限（字节）：1 MiB 足够定位问题，再多也不该塞进任务结果里。
const READ_LIMIT: usize = 1024 * 1024;

/// 收尾宽限：作业句柄已关、写端进程通常应已消失，EOF 会立刻到来。
/// 超过这个时间说明写端被外部进程（不是本步子孙）持有着，
/// 宁可少留日志，也不让 `run_script` 永久挂住。
const DRAIN_GRACE: Duration = Duration::from_secs(5);

/// 起一个抽干线程：读满 `READ_LIMIT` 后继续把剩余数据排空丢弃。
///
/// 为什么不再用 `read_to_string`：
/// 1. **无界**——脚本 `for i in range(10**8): print(i)` 能把后端内存吃光；
/// 2. **遇非 UTF-8 直接失败**——worker 吐了 GBK 中文，日志就整段丢失，
///    排查时只看到空 stdout，极易被误判成"脚本没输出"。
/// 所以这里读 `Vec<u8>`，最后统一做 lossy 转换。
fn spawn_reader<R: Read + Send + 'static>(pipe: Option<R>) -> mpsc::Receiver<Vec<u8>> {
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let Some(mut p) = pipe else {
            let _ = tx.send(Vec::new());
            return;
        };
        let mut buf: Vec<u8> = Vec::new();
        let mut limited = (&mut p).take(READ_LIMIT as u64);
        let _ = limited.read_to_end(&mut buf);
        let mut rest = limited.into_inner();
        let _ = std::io::copy(&mut rest, &mut std::io::sink());
        let _ = tx.send(buf);
    });
    rx
}

/// 取回抽干结果，**不会无限等待**。超时按空输出处理而不是阻塞——
/// `run_script` 的契约是"要么给结果，要么给超时"，不包括"卡在这里不回来"。
fn finish_reader(rx: mpsc::Receiver<Vec<u8>>) -> String {
    match rx.recv_timeout(DRAIN_GRACE) {
        Ok(bytes) => String::from_utf8_lossy(&bytes).into_owned(),
        Err(_) => {
            tracing::warn!("脚本输出管道未在宽限期内关闭，本次按空输出处理（写端可能被外部进程持有）");
            String::new()
        }
    }
}

/// 按字符截断，保留头部（提示词类内容开头信息量最大）。
fn head(s: &str, limit: usize) -> String {
    if s.chars().count() <= limit {
        return s.to_string();
    }
    let mut out: String = s.chars().take(limit).collect();
    out.push_str("…(已截断)");
    out
}

/// 按字符截断，保留尾部（错误信息通常在末尾）。
fn tail(s: &str, limit: usize) -> String {
    let count = s.chars().count();
    if count <= limit {
        return s.to_string();
    }
    s.chars().skip(count - limit).collect()
}

impl StepRunner for ProcessRunner {
    fn backend(&self) -> &'static str {
        "process"
    }

    fn run_step(&self, req: &StepRequest<'_>) -> StepOutcome {
        // 注入给脚本的上下文：只有环境变量一条通道（命令行参数容易被拼接注入，刻意不用）。
        // 全部做长度截断：Windows 单变量上限 32767，且任务目标可能极长，截断比失败好。
        let mut envs: Vec<(String, String)> = vec![
            ("SA_TASK_ID".into(), req.task.id.clone()),
            ("SA_TASK_TYPE".into(), req.task.task_type.as_str().to_string()),
            ("SA_GOAL".into(), head(&req.task.goal, 2000)),
            ("SA_STEP_INDEX".into(), req.step.index.to_string()),
            ("SA_STEP_NAME".into(), head(&req.step.name, 200)),
            ("SA_AGENT_ID".into(), req.step.agent_id.clone()),
            ("SA_AGENT_NAME".into(), head(req.spec.name, 100)),
            ("SA_PERMISSION".into(), req.step.permission.as_str().to_string()),
            ("SA_SANDBOX".into(), req.step.sandbox.to_string()),
            ("SA_CONTEXT_SUMMARY".into(), head(&req.ctx.summary, 1000)),
            ("SA_ATTEMPT".into(), req.attempt.to_string()),
        ];
        // 部署期配置（模型地址/模型名/凭据）按前缀显式透传：不配则 worker 走规则实现，
        // 配了则走模型；两条路径都能跑通，所以冒烟不依赖本机装没装模型服务。
        envs.extend(passthrough_envs(std::env::vars()));
        // 越线不在"截断"里解决，而是换通道：goal/context 落盘、环境变量只传路径。
        let (envs, spill_dir) = spill_oversized_envs(envs);

        let result = self.run_script(&req.step.agent_id, &envs);
        // 临时文件里是任务目标原文，脚本一结束就删，不留在磁盘上过夜
        if let Some(dir) = spill_dir {
            let _ = std::fs::remove_dir_all(&dir);
        }
        match result {
            Ok(out) if out.timed_out => StepOutcome::retryable(format!(
                "脚本执行超时（>{}s）已强制终止：agent={}",
                self.timeout_for(&req.step.agent_id).as_secs(),
                req.step.agent_id
            )),
            Ok(out) if out.exit_code == 0 => {
                let text = if out.stdout.trim().is_empty() {
                    format!("[{}] 执行成功但无输出", req.step.agent_id)
                } else {
                    out.stdout
                };
                // 粗估 token：4 字符 ≈ 1 token，仅用于预算记账下限保护
                let tokens = (text.chars().count() as u64 / 4).max(1);
                let mut outcome = StepOutcome::ok(tokens, text);
                outcome.counters.push("sa_runner_process_ok_total");
                outcome
            }
            Ok(out) => {
                let detail = if out.stderr.trim().is_empty() {
                    format!("退出码 {}", out.exit_code)
                } else {
                    format!("退出码 {}：{}", out.exit_code, out.stderr.trim())
                };
                StepOutcome::retryable(format!("脚本执行失败（{}）", detail))
            }
            Err(e @ (RunError::BadAgentId(_) | RunError::Escape(_))) => {
                StepOutcome::fatal(format!("执行后端拒绝该步骤（安全边界）：{e}"))
            }
            Err(e) => StepOutcome::retryable(e.to_string()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn oversized_env_spills_to_files_instead_of_silent_truncation() {
        // M3：环境块超预算时不能靠截断，必须换通道，且原值一个字都不能少
        let big = "目标".repeat(ENV_BUDGET_BYTES); // 约 3 倍预算
        let envs = vec![
            ("SA_TASK_ID".to_string(), "t1".to_string()),
            ("SA_GOAL".to_string(), big.clone()),
            ("SA_CONTEXT_SUMMARY".to_string(), "上下文".repeat(1000)),
        ];
        let before = env_bytes(&envs);
        assert!(before > ENV_BUDGET_BYTES, "构造的用例必须真的超预算");

        let (out, dir) = spill_oversized_envs(envs);
        let dir = dir.expect("超预算必须落盘");
        assert!(env_bytes(&out) < before, "改道后环境块应显著变小");

        // 换成了 *_FILE，且文件里是完整原文（不是 512 字符的截断版）
        let goal_file = out
            .iter()
            .find(|(k, _)| k == "SA_GOAL_FILE")
            .map(|(_, v)| v.clone())
            .expect("SA_GOAL_FILE 必须在");
        let text = std::fs::read_to_string(&goal_file).unwrap();
        assert_eq!(text, big, "落盘内容必须与原文逐字一致");
        assert!(
            out.iter().any(|(k, v)| k == "SA_GOAL" && v.contains("已改道")),
            "原键要留一句说明，别让只看 SA_GOAL 的脚本莫名其妙"
        );
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn small_env_is_left_untouched() {
        // 没超预算就一点都不许动：改道本身也是成本
        let envs = vec![
            ("SA_GOAL".to_string(), "写一个周报".to_string()),
            ("SA_TASK_ID".to_string(), "t1".to_string()),
        ];
        let snapshot = envs.clone();
        let (out, dir) = spill_oversized_envs(envs);
        assert!(dir.is_none(), "未超预算不该落盘");
        assert_eq!(out, snapshot);
    }

    fn temp_dir(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("sa_runner_{tag}_{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn rejects_agent_id_injection() {
        let dir = temp_dir("inject");
        let runner = ProcessRunner::new(dir.clone(), 5).unwrap();
        for bad in ["../evil", "a/b", r"a\b", "", "a b", "a;rm"] {
            assert!(
                matches!(runner.resolve(bad), Err(RunError::BadAgentId(_))),
                "必须拒绝 {bad:?}"
            );
        }
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn rejects_unknown_extension() {
        let dir = temp_dir("ext");
        std::fs::write(dir.join("coder.sh"), "echo hi").unwrap();
        let runner = ProcessRunner::new(dir.clone(), 5).unwrap();
        assert!(matches!(runner.resolve("coder"), Err(RunError::NotFound(_))));
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn refuses_directory_outside_root() {
        let base = temp_dir("escape");
        let inner = base.join("scripts");
        std::fs::create_dir_all(&inner).unwrap();
        // 软链接逃逸在 Windows 需要权限，这里直接验证 resolve 的白名单拒绝路径
        let runner = ProcessRunner::new(inner.clone(), 5).unwrap();
        std::fs::write(base.join("outer.cmd"), "@echo off").unwrap();
        assert!(matches!(runner.resolve(".."), Err(RunError::BadAgentId(_))));
        let _ = std::fs::remove_dir_all(base);
    }

    #[cfg(windows)]
    #[test]
    fn runs_real_script_and_captures_output() {
        let dir = temp_dir("run");
        let runner = ProcessRunner::new(dir.clone(), 20).unwrap();
        // H1：默认配置下 shell 脚本必须被拒——能写 cmd 等于能在宿主机执行任意命令
        std::fs::write(dir.join("coder.cmd"), "@echo off\r\nexit /b 0\r\n").unwrap();
        assert!(
            matches!(runner.resolve("coder"), Err(RunError::NotFound(_))),
            "默认只跑 .py，.cmd 必须解析失败"
        );
        // 换成 .py 后同一 agent 立刻可跑
        std::fs::write(
            dir.join("coder.py"),
            "import os\nprint(\"REPORT task=%s step=%s\" % (os.environ.get(\"SA_TASK_ID\"), os.environ.get(\"SA_STEP_INDEX\")))\n",
        )
        .unwrap();
        let out = runner
            .run_script(
                "coder",
                &[
                    ("SA_TASK_ID".to_string(), "t-1".to_string()),
                    ("SA_STEP_INDEX".to_string(), "2".to_string()),
                ],
            )
            .unwrap();
        assert!(!out.timed_out);
        assert_eq!(out.exit_code, 0);
        assert!(
            out.stdout.contains("REPORT task=t-1 step=2"),
            "必须真实跑出脚本输出，实际={:?}",
            out.stdout
        );
        let _ = std::fs::remove_dir_all(dir);
    }

    #[cfg(windows)]
    #[test]
    fn nonzero_exit_is_retryable_and_timeout_kills() {
        let dir = temp_dir("fail");
        std::fs::write(
            dir.join("writer.py"),
            "import sys\nprint(\"boom\", file=sys.stderr)\nsys.exit(3)\n",
        )
        .unwrap();
        std::fs::write(dir.join("slow.py"), "import time\ntime.sleep(6)\n").unwrap();
        let runner = ProcessRunner::new(dir.clone(), 1).unwrap();

        let bad = runner.run_script("writer", &[]).unwrap();
        assert_eq!(bad.exit_code, 3);
        assert!(bad.stderr.contains("boom"));

        let slow = runner.run_script("slow", &[]).unwrap();
        assert!(slow.timed_out, "超时必须被识别并终止");

        let _ = std::fs::remove_dir_all(dir);
    }

    // ---------------------------------------------------- 环境透传白名单

    #[test]
    fn passthrough_needs_prefix_at_the_very_start() {
        assert!(should_passthrough("SA_LLM_MODEL"));
        assert!(should_passthrough("SA_LLM_BASE"));
        // 前缀拼在中间不算：否则 `XSA_LLM_MODEL` 就能绕过白名单
        assert!(!should_passthrough("XSA_LLM_MODEL"));
        // 前缀必须完整（含分隔下划线），`SA_LLMX` 不是我们的配置项
        assert!(!should_passthrough("SA_LLMX"));
        // 凭据与系统变量绝不透传
        assert!(!should_passthrough("SA_AUTH_SECRET"));
        assert!(!should_passthrough("PATH"));
    }

    #[test]
    fn passthrough_envs_filters_truncates_and_sorts() {
        let long = "x".repeat(PASSTHROUGH_VALUE_LIMIT + 50);
        let got = passthrough_envs(vec![
            ("SA_LLM_MODEL".to_string(), "minicpm5-2b:f16".to_string()),
            ("SA_AUTH_SECRET".to_string(), "s3cret".to_string()),
            ("SA_LLM_API_KEY".to_string(), long),
            (
                "SA_LLM_BASE".to_string(),
                "http://127.0.0.1:11434/v1".to_string(),
            ),
        ]);
        let keys: Vec<&str> = got.iter().map(|(k, _)| k.as_str()).collect();
        assert_eq!(keys, vec!["SA_LLM_API_KEY", "SA_LLM_BASE", "SA_LLM_MODEL"]);
        assert!(
            !keys.contains(&"SA_AUTH_SECRET"),
            "凭据类变量绝不能被透传进脚本"
        );
        assert_eq!(
            got[0].1.chars().count(),
            PASSTHROUGH_VALUE_LIMIT,
            "超长值必须截断到硬上限（含省略标记在内），否则会撑坏子进程环境"
        );
        // 上限比标记还短时不许反过来超限
        assert!(clip_env(&"x".repeat(100), 3).chars().count() <= 3);
        // 未超长则一字不改（不能给正常值平白加标记）
        assert_eq!(clip_env("http://127.0.0.1:11434/v1", 512), "http://127.0.0.1:11434/v1");
    }

    // ---------------------------------------------------- 按 agent 覆盖超时

    #[test]
    fn timeout_override_clamps_and_falls_back() {
        let base = Duration::from_secs(20);
        // 未配 → 全局默认
        assert_eq!(resolve_timeout(None, base), base);
        // 正常覆盖生效（模型步骤要 300s）
        assert_eq!(
            resolve_timeout(Some("300".into()), base),
            Duration::from_secs(300)
        );
        // 上限封顶 600s，防止一个手滑写成 999999 把任务挂死一整天
        assert_eq!(
            resolve_timeout(Some("999999".into()), base),
            Duration::from_secs(600)
        );
        // 配错/配 0/空串一律回落，不报错
        assert_eq!(resolve_timeout(Some("abc".into()), base), base);
        assert_eq!(resolve_timeout(Some("0".into()), base), base);
        assert_eq!(resolve_timeout(Some("  ".into()), base), base);
        // 允许带空白的合法值
        assert_eq!(
            resolve_timeout(Some(" 45 ".into()), base),
            Duration::from_secs(45)
        );
    }

    #[test]
    fn model_on_raises_default_timeout_only_upward() {
        // 没配模型：默认值原样不动，规则 agent 不会白等 5 分钟
        assert_eq!(
            model_aware_default(Duration::from_secs(20), false),
            Duration::from_secs(20)
        );
        // 配了模型：20s 抬到 300s，否则 code-agent 每次都被 kill
        assert_eq!(
            model_aware_default(Duration::from_secs(20), true),
            Duration::from_secs(300)
        );
        // 已经比 300s 大就不许往下压：只升不降
        assert_eq!(
            model_aware_default(Duration::from_secs(600), true),
            Duration::from_secs(600)
        );
    }

    #[test]
    fn tail_keeps_utf8_boundaries() {
        let s = "中文输出很长".repeat(10);
        let t = tail(&s, 5);
        assert_eq!(t.chars().count(), 5);
    }

    #[test]
    fn reader_keeps_non_utf8_output_instead_of_dropping_it() {
        // 老写法 `read_to_string` 遇到非 UTF-8 会整体失败，日志变空——
        // 排查时看到"脚本没输出"，实际是脚本吐了 GBK 中文。这里守 lossy 转换。
        let raw = vec![0xff, 0xfe, b'h', b'i'];
        let s = finish_reader(spawn_reader(Some(std::io::Cursor::new(raw))));
        assert!(s.contains("hi"), "ASCII 部分不能丢：{s:?}");
        assert!(
            s.contains('\u{FFFD}'),
            "非法字节应被替换而不是整段丢弃：{s:?}"
        );
    }

    #[test]
    fn reader_caps_what_it_keeps() {
        // 大输出必须"只保留上限那么多、其余排空丢弃"，否则脚本疯狂 print 就能吃光后端内存。
        let raw = vec![b'a'; READ_LIMIT * 2];
        let s = finish_reader(spawn_reader(Some(std::io::Cursor::new(raw))));
        assert_eq!(
            s.len(),
            READ_LIMIT,
            "保留量必须有上限，否则大输出会吃光后端内存"
        );
    }

    #[cfg(windows)]
    #[test]
    fn grandchild_holding_the_pipe_does_not_hang_the_runner() {
        use std::process::{Command, Stdio};
        // 造一个"父进程退出、孙进程仍握着 stdout 写端"的局面：
        // `start /b` 起的 ping 继承管道，cmd 自己立刻退出。
        // 老实现会 join 到 ping 结束（30 秒）；新实现先关作业句柄收掉整棵进程树，
        // 管道随即 EOF，收尾在宽限期内返回。
        let mut child = Command::new("cmd")
            .args(["/C", "start /b ping -n 30 127.0.0.1 & exit"])
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .expect("spawn cmd");
        let job = Job::attach(JobLimits::default()).expect("建作业");
        assert!(job.assign(&child), "沙箱没挂上，这条断言就没有意义");
        let rx = spawn_reader(child.stdout.take());
        let _ = child.wait();
        drop(job);
        let started = Instant::now();
        let _ = finish_reader(rx);
        assert!(
            started.elapsed() < DRAIN_GRACE,
            "收尾等了 {:?}：孙进程没被作业收掉，管道一直没 EOF",
            started.elapsed()
        );
    }
}
