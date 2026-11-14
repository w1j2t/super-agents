//! 执行后端（Runner）端口与注册表。
//!
//! 与 `store::backends` 同构：**换执行后端不改治理链路**。
//! 新增后端 = 新增一个实现 `StepRunner` 的文件 + 在 `build()` 里登记一行。
//! 鉴权、预算降档、重试退避、熔断、审计、指标、飞轮沉淀全部在 `Executor` 内，与后端无关。

pub mod process;

use crate::config::Config;
use crate::orchestration::executor::StepRunner;
use std::sync::Arc;

/// 可选执行后端。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RunnerKind {
    /// 确定性模拟（默认，零外部依赖，验收可复现）
    Sim,
    /// 真实执行：调用 `SA_RUNNER_DIR` 下的白名单脚本
    Process,
}

impl RunnerKind {
    pub const ALL: [&'static str; 2] = ["sim", "process"];

    pub fn as_str(&self) -> &'static str {
        match self {
            RunnerKind::Sim => "sim",
            RunnerKind::Process => "process",
        }
    }

    pub fn parse(s: &str) -> Option<RunnerKind> {
        match s.trim().to_ascii_lowercase().as_str() {
            "sim" | "mock" | "simulate" => Some(RunnerKind::Sim),
            "process" | "script" | "real" => Some(RunnerKind::Process),
            _ => None,
        }
    }
}

/// 工厂：新增后端只需在这里加一条分支。
///
/// 返回 `None` 表示使用 `Executor` 内置的 `SimRunner`（零大小、无状态）。
pub fn build(config: &Config) -> Result<Option<Arc<dyn StepRunner>>, String> {
    match config.runner {
        RunnerKind::Sim => Ok(None),
        RunnerKind::Process => {
            let dir = config.runner_dir.clone().ok_or_else(|| {
                format!(
                    "SA_RUNNER=process 时必须同时指定 SA_RUNNER_DIR（脚本目录），可选 {}",
                    RunnerKind::ALL.join(" / ")
                )
            })?;
            let runner = process::ProcessRunner::new(dir, config.runner_timeout_secs)
                .map_err(|e| format!("执行后端初始化失败: {e}"))?;
            Ok(Some(Arc::new(runner)))
        }
    }
}
