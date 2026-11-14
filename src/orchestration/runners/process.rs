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
use std::path::{Path, PathBuf};
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
const ALLOWED_EXT: [&str; 4] = ["cmd", "bat", "ps1", "py"];

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
    #[error("未找到该 agent 的脚本（支持 cmd/bat/ps1/py）: {0}")]
    NotFound(String),
    #[error("脚本路径越界（必须位于配置目录内）: {0}")]
    Escape(String),
    #[error("启动脚本失败: {0}")]
    Spawn(String),
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
        for ext in ALLOWED_EXT {
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
        resolve_timeout(std::env::var(&key).ok(), self.timeout)
    }

    /// 真实执行：带硬超时，stdout/stderr 由独立线程抽干，杜绝管道写满导致的假死。
    pub fn run_script(
        &self,
        agent_id: &str,
        envs: &[(String, String)],
    ) -> Result<RunOutput, RunError> {
        let timeout = self.timeout_for(agent_id);
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

        let mut out_pipe = child.stdout.take();
        let mut err_pipe = child.stderr.take();
        let out_handle = std::thread::spawn(move || {
            let mut buf = String::new();
            if let Some(p) = out_pipe.as_mut() {
                let _ = p.read_to_string(&mut buf);
            }
            buf
        });
        let err_handle = std::thread::spawn(move || {
            let mut buf = String::new();
            if let Some(p) = err_pipe.as_mut() {
                let _ = p.read_to_string(&mut buf);
            }
            buf
        });

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

        let stdout = out_handle.join().unwrap_or_default();
        let stderr = err_handle.join().unwrap_or_default();

        Ok(RunOutput {
            exit_code: status.and_then(|s| s.code()).unwrap_or(-1),
            stdout: tail(&stdout, OUTPUT_LIMIT),
            stderr: tail(&stderr, OUTPUT_LIMIT),
            elapsed_ms: started.elapsed().as_millis(),
            timed_out,
        })
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

        match self.run_script(&req.step.agent_id, &envs) {
            Ok(out) if out.timed_out => StepOutcome::retryable(format!(
                "脚本执行超时（>{}s）已强制终止：agent={}",
                self.timeout.as_secs(),
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
        std::fs::write(
            dir.join("coder.cmd"),
            "@echo off\r\necho REPORT task=%SA_TASK_ID% step=%SA_STEP_INDEX%\r\nexit /b 0\r\n",
        )
        .unwrap();
        let runner = ProcessRunner::new(dir.clone(), 20).unwrap();
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
        std::fs::write(dir.join("writer.cmd"), "@echo off\r\necho boom 1>&2\r\nexit /b 3\r\n").unwrap();
        std::fs::write(dir.join("slow.cmd"), "@echo off\r\nping -n 6 127.0.0.1 >nul\r\n").unwrap();
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
    fn tail_keeps_utf8_boundaries() {
        let s = "中文输出很长".repeat(10);
        let t = tail(&s, 5);
        assert_eq!(t.chars().count(), 5);
    }
}
