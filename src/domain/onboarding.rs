//! 新手教程：内置步骤定义 + 按用户推进的进度状态机。

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize)]
pub struct StepDef {
    pub key: &'static str,
    pub order: u8,
    pub title: &'static str,
    pub summary: &'static str,
    /// 前端可据此定位到具体页面/动作（后端不渲染任何 UI）
    pub action: &'static str,
    pub required: bool,
    pub est_seconds: u32,
}

pub const STEPS: [StepDef; 6] = [
    StepDef {
        key: "welcome",
        order: 1,
        title: "认识你的超级 agent",
        summary: "了解统一超级 agents 层如何理解意图、拆分任务并调度子 agents。",
        action: "open_agent_overview",
        required: true,
        est_seconds: 60,
    },
    StepDef {
        key: "profile",
        order: 2,
        title: "设定个人偏好",
        summary: "设置语言、时区与工作偏好，后续所有回答与调度均以此为准。",
        action: "edit_profile",
        required: true,
        est_seconds: 90,
    },
    StepDef {
        key: "agents",
        order: 3,
        title: "熟悉子 agents 分工",
        summary: "查看各子 agent 的垂直领域、专业文档与技能，理解任务会交给谁。",
        action: "open_agent_directory",
        required: true,
        est_seconds: 120,
    },
    StepDef {
        key: "memory",
        order: 4,
        title: "建立共享记忆",
        summary: "写入第一条记忆，让所有子 agents 在后续任务中复用这份上下文。",
        action: "create_memory",
        required: true,
        est_seconds: 120,
    },
    StepDef {
        key: "first_task",
        order: 5,
        title: "发起第一个任务",
        summary: "提交一个真实目标，观察编排计划、agent 分配、执行轨迹与成本。",
        action: "create_task",
        required: true,
        est_seconds: 180,
    },
    StepDef {
        key: "boundaries",
        order: 6,
        title: "了解能力边界与安全",
        summary: "确认哪些请求会被拒绝、越权如何拦截、出错时如何降级。",
        action: "review_boundaries",
        required: false,
        est_seconds: 60,
    },
];

pub fn step_keys() -> Vec<&'static str> {
    STEPS.iter().map(|s| s.key).collect()
}

pub fn find_step(key: &str) -> Option<&'static StepDef> {
    STEPS.iter().find(|s| s.key == key)
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OnboardingProgress {
    pub user_id: String,
    pub completed: Vec<String>,
    pub skipped: bool,
    pub started_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
    /// 幂等记录：每个步骤最近一次完成时间
    pub step_completed_at: Vec<(String, DateTime<Utc>)>,
}

impl OnboardingProgress {
    pub fn new(user_id: &str, now: DateTime<Utc>) -> Self {
        Self {
            user_id: user_id.to_string(),
            completed: Vec::new(),
            skipped: false,
            started_at: now,
            updated_at: now,
            step_completed_at: Vec::new(),
        }
    }

    pub fn complete(&mut self, key: &str, now: DateTime<Utc>) -> Result<bool, String> {
        if find_step(key).is_none() {
            return Err(format!("未知教程步骤: {key}"));
        }
        if self.completed.iter().any(|k| k == key) {
            self.updated_at = now;
            return Ok(false); // 幂等：重复完成不报错、不重复计数
        }
        self.completed.push(key.to_string());
        self.step_completed_at.push((key.to_string(), now));
        self.updated_at = now;
        Ok(true)
    }

    pub fn reset(&mut self, now: DateTime<Utc>) {
        self.completed.clear();
        self.step_completed_at.clear();
        self.skipped = false;
        self.started_at = now;
        self.updated_at = now;
    }

    pub fn mark_skipped(&mut self, now: DateTime<Utc>) {
        self.skipped = true;
        self.updated_at = now;
    }

    pub fn required_total(&self) -> usize {
        STEPS.iter().filter(|s| s.required).count()
    }

    pub fn required_done(&self) -> usize {
        STEPS
            .iter()
            .filter(|s| s.required && self.completed.iter().any(|k| k == s.key))
            .count()
    }

    pub fn status(&self) -> &'static str {
        if self.required_done() >= self.required_total() {
            "completed"
        } else if self.skipped {
            "skipped"
        } else if self.completed.is_empty() {
            "not_started"
        } else {
            "in_progress"
        }
    }

    pub fn next_step(&self) -> Option<&'static StepDef> {
        if self.skipped {
            return None;
        }
        STEPS
            .iter()
            .find(|s| s.required && !self.completed.iter().any(|k| k == s.key))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn completes_and_is_idempotent() {
        let now = Utc::now();
        let mut p = OnboardingProgress::new("u1", now);
        assert_eq!(p.status(), "not_started");
        assert!(p.complete("welcome", now).unwrap());
        assert!(!p.complete("welcome", now).unwrap());
        assert_eq!(p.completed.len(), 1);
        assert_eq!(p.status(), "in_progress");
        assert_eq!(p.next_step().unwrap().key, "profile");
    }

    #[test]
    fn rejects_unknown_step() {
        let now = Utc::now();
        let mut p = OnboardingProgress::new("u1", now);
        assert!(p.complete("nope", now).is_err());
    }

    #[test]
    fn all_required_makes_completed() {
        let now = Utc::now();
        let mut p = OnboardingProgress::new("u1", now);
        for s in STEPS.iter().filter(|s| s.required) {
            p.complete(s.key, now).unwrap();
        }
        assert_eq!(p.status(), "completed");
        assert!(p.next_step().is_none());
    }

    #[test]
    fn skip_and_reset() {
        let now = Utc::now();
        let mut p = OnboardingProgress::new("u1", now);
        p.complete("welcome", now).unwrap();
        p.mark_skipped(now);
        assert_eq!(p.status(), "skipped");
        assert!(p.next_step().is_none());
        p.reset(now);
        assert_eq!(p.status(), "not_started");
        assert!(p.completed.is_empty());
    }
}
