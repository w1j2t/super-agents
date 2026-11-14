//! 路由与计划生成：任务分类、agent 打分调配、预算分配、越界判定。

use crate::domain::agent::{self, AgentSpec};
use crate::domain::task::{ModelProfile, PlanStep, TaskType};
use crate::security::rbac::Permission;

/// 分类关键词表。命中即加权，命中越多置信度越高。
const TASK_KEYWORDS: &[(TaskType, &[&str])] = &[
    (
        TaskType::Coding,
        &[
            "代码", "编程", "编译", "报错", "重构", "函数", "接口", "api", "sql", "rust",
            "python", "脚本", "单元测试", "单测", "bug", "调试", "部署", "端口", "进程",
            "日志", "磁盘", "环境", "依赖", "性能", "内存",
        ],
    ),
    (
        TaskType::Writing,
        &[
            "文档", "写作", "报告", "总结", "邮件", "文案", "润色", "周报", "说明书", "prd",
            "标书", "稿", "写作风格", "合同", "条款", "协议", "报销", "发票", "对账", "单据",
        ],
    ),
    (
        TaskType::Research,
        &[
            "调研", "查资料", "搜索", "检索", "对比", "最新", "来源", "核实", "查一下", "行情",
            "政策", "论文", "文献", "分析", "指标", "口径", "数据", "归因", "趋势",
            "转化率", "留存",
        ],
    ),
    (
        TaskType::Planning,
        &[
            "规划", "计划", "排期", "里程碑", "拆解", "待办", "路线图", "学习路线", "备考",
            "时间表", "任务分解", "预算", "成本", "账期",
        ],
    ),
    (
        TaskType::Ideation,
        &[
            "创意", "点子", "命名", "构思", "脑暴", "方案设计", "起个名字", "灵感", "文案方向",
            "玩法", "关卡", "数值", "技能", "战斗", "游戏", "策划",
        ],
    ),
    (
        TaskType::Collaboration,
        &[
            "会议", "纪要", "对齐", "协作", "分工", "催办", "沟通", "同步进度", "多人",
            "团队",
        ],
    ),
];

/// 明显越界/超纲信号：命中则直接拒绝，交给用户澄清或转人工。
const OUT_OF_SCOPE_MARKERS: &[&str] = &[
    "监听手机", "破解", "绕过验证", "盗", "开后门", "抓取他人隐私", "删除系统文件",
    "格式化磁盘", "加密货币挖矿", "伪造证件",
];

#[derive(Debug, Clone)]
pub struct RoutingOutcome {
    pub task_type: TaskType,
    pub confidence: f64,
    pub signals: Vec<String>,
    pub dispatch_scores: Vec<(String, f64)>,
}

#[derive(Debug, Clone)]
pub enum PlanOutcome {
    Ok(PlanBundle),
    OutOfScope { reason: String, suggestion: String },
}

#[derive(Debug, Clone)]
pub struct PlanBundle {
    pub routing: RoutingOutcome,
    pub steps: Vec<PlanStep>,
    pub budget_tokens: u64,
    pub planned_confidence: f64,
}

pub fn is_out_of_scope(goal: &str) -> Option<String> {
    let g = goal.to_lowercase();
    OUT_OF_SCOPE_MARKERS
        .iter()
        .find(|m| g.contains(&m.to_lowercase()))
        .map(|m| m.to_string())
}

fn hits(text: &str, keywords: &[&str]) -> usize {
    let t = text.to_lowercase();
    keywords
        .iter()
        .filter(|k| t.contains(&k.to_lowercase()))
        .count()
}

/// 分类：显式指定优先；否则按关键词打分。全部落空则返回 None（交由上层判定超纲）。
pub fn classify(explicit: Option<TaskType>, goal: &str) -> Option<RoutingOutcome> {
    let mut scores: Vec<(TaskType, usize, Vec<String>)> = TASK_KEYWORDS
        .iter()
        .map(|(t, kws)| {
            let t_low = goal.to_lowercase();
            let matched: Vec<String> = kws
                .iter()
                .filter(|k| t_low.contains(&k.to_lowercase()))
                .map(|k| k.to_string())
                .collect();
            (*t, matched.len(), matched)
        })
        .collect();
    scores.sort_by(|a, b| b.1.cmp(&a.1));

    if let Some(t) = explicit {
        let signals = scores
            .iter()
            .find(|(st, _, _)| *st == t)
            .map(|(_, _, s)| s.clone())
            .unwrap_or_default();
        let dispatch = dispatch_scores(goal, t);
        return Some(RoutingOutcome {
            task_type: t,
            confidence: 1.0,
            signals,
            dispatch_scores: dispatch,
        });
    }

    let (best, best_hits, best_signals) = scores.first()?.clone();
    if best_hits == 0 {
        return None;
    }
    // 归一化：1 命中 = 0.55，2 命中 = 0.72，3 命中 = 0.82，渐近 1.0
    let confidence = 0.55 + 0.45 * (best_hits as f64 / (best_hits as f64 + 1.6));
    let dispatch = dispatch_scores(goal, best);
    Some(RoutingOutcome {
        task_type: best,
        confidence: (confidence * 100.0).round() / 100.0,
        signals: best_signals,
        dispatch_scores: dispatch,
    })
}

/// 按任务为候选 agent 打分：领域匹配 0.5 + 关键词 0.15/个（上限 0.3）+ 领域名命中 0.1，
/// 并做对应 task_type 的准入过滤。
pub fn dispatch_scores(goal: &str, task_type: TaskType) -> Vec<(String, f64)> {
    let mut out: Vec<(String, f64)> = agent::sub_agents()
        .filter(|a| a.task_types.contains(&task_type))
        .map(|a| {
            let kw = hits(goal, a.keywords).min(2) as f64 * 0.15;
            let domain = if goal.contains(a.domain) { 0.1 } else { 0.0 };
            let score = (0.5 + kw + domain).min(1.0);
            (a.id.to_string(), (score * 100.0).round() / 100.0)
        })
        .collect();
    out.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    out
}

const MIN_DISPATCH_SCORE: f64 = 0.45;

fn pick_worker(goal: &str, task_type: TaskType) -> Option<(&'static AgentSpec, f64)> {
    let scores = dispatch_scores(goal, task_type);
    let (id, score) = scores.first()?;
    if *score < MIN_DISPATCH_SCORE {
        return None;
    }
    agent::find(id).map(|a| (a, *score))
}

/// 生成计划：意图确认 → **并行执行簇** → 合规复核 → 汇总。
///
/// # 并行簇的编排意图（多 agents 集群的第一次落地）
///
/// 旧计划是一条直线：`intent → main → [evidence] → guard → synthesis`，
/// 每一步都等前一步跑完。但 `evidence`（只读检索）与 `main`（写产出）之间
/// **没有数据依赖**——它们只是在旧实现里"恰好被排在一起"。把它们放进同一层并行，
/// 一次研究类任务就能省掉一次整段检索耗时（真模型下单步实测 129s 量级，省的不是毫秒）。
///
/// 依赖关系（`depends_on`）表达的是**真实的数据流**，而不是数组下标顺序：
/// ```text
/// 0 intent ─┬─> 1 main ─────┬─> 3 guard ─> 4 synthesis
///           └─> 2 evidence ─┘   （evidence 仅 research 类任务存在）
/// ```
/// `guard` 同时审 `main` 与 `evidence` 两路产出，所以它依赖两者；
/// `synthesis` 依赖 `guard`——一票否决权必须在汇总之前生效，否则"审过了"只是句口号。
pub fn build_plan(goal: &str, routing: &RoutingOutcome, requested_model: Option<ModelProfile>) -> PlanOutcome {
    if let Some(marker) = is_out_of_scope(goal) {
        return PlanOutcome::OutOfScope {
            reason: format!("请求命中超纲/越界信号「{marker}」，不在可承接范围内。"),
            suggestion: "请把目标改为合规的写作、检索、规划、编码或沟通协作类任务。".to_string(),
        };
    }

    let Some((worker, worker_score)) = pick_worker(goal, routing.task_type) else {
        return PlanOutcome::OutOfScope {
            reason: format!(
                "无子 agent 能承接「{}」类任务（最高匹配分低于 {:.2}）。",
                routing.task_type.as_str(),
                MIN_DISPATCH_SCORE
            ),
            suggestion: "请说明更具体的产出物（例如：代码/文档/检索结论/规划表/创意候选）与验收标准。".to_string(),
        };
    };

    let base = requested_model.unwrap_or(worker.default_model);
    let mut steps: Vec<PlanStep> = Vec::new();
    let mut idx = 0u8;

    // 1. 意图与边界确认（统一超级 agent 层，不可跳过；集群的唯一入口层）
    steps.push(PlanStep {
        index: idx,
        name: "intent_and_boundary".into(),
        agent_id: "super-agent".into(),
        model_profile: ModelProfile::Standard,
        permission: Permission::CrossAgent,
        est_tokens: 900,
        sandbox: false,
        depends_on: Vec::new(),
    });
    let intent_idx = idx;
    idx += 1;

    // 2. 主执行 agent（写产出）
    steps.push(PlanStep {
        index: idx,
        name: format!("main:{}", worker.id),
        agent_id: worker.id.to_string(),
        model_profile: base,
        permission: Permission::Write,
        est_tokens: estimate_tokens(base, 1.0),
        sandbox: !worker.sandbox_profile.contains("no-exec"),
        depends_on: vec![intent_idx],
    });
    let main_idx = idx;
    idx += 1;

    // 3. 检索类任务追加证据整理（RAG 增强）：**与主执行并行**，不是排在它后面。
    //    这两步之间没有数据依赖，串行只是历史写法；并进同一拓扑层才有实际收益。
    let mut evidence_idx: Option<u8> = None;
    if routing.task_type == TaskType::Research {
        steps.push(PlanStep {
            index: idx,
            name: "evidence:research-agent".into(),
            agent_id: "research-agent".into(),
            model_profile: ModelProfile::Standard,
            permission: Permission::Read,
            est_tokens: 700,
            sandbox: true,
            depends_on: vec![intent_idx],
        });
        evidence_idx = Some(idx);
        idx += 1;
    }

    // 4. 有害/越权复核（只读，一票否决）：两路产出都要过审，因此依赖全部上游执行层。
    let mut review_deps = vec![main_idx];
    if let Some(e) = evidence_idx {
        review_deps.push(e);
    }
    steps.push(PlanStep {
        index: idx,
        name: "compliance_review".into(),
        agent_id: "guard-agent".into(),
        model_profile: ModelProfile::Standard,
        permission: Permission::Read,
        est_tokens: 500,
        sandbox: false,
        depends_on: review_deps,
    });
    let review_idx = idx;
    idx += 1;

    // 5. 结果汇总与交付校验（只在复核通过之后才有意义）
    steps.push(PlanStep {
        index: idx,
        name: "synthesis".into(),
        agent_id: "super-agent".into(),
        model_profile: ModelProfile::Standard,
        permission: Permission::Write,
        est_tokens: 800,
        sandbox: false,
        depends_on: vec![review_idx],
    });

    // 预算 = 规划估算 × 1.2 余量：执行侧抖动区间为 0.85~1.15，
    // 若以估算值本身作上限，会因正常抖动产生「假性超支」。
    let planned: u64 = steps.iter().map(|s| s.est_tokens).sum();
    let budget: u64 = ((planned as f64) * 1.2).ceil() as u64;
    let planned_confidence = (0.45 * worker_score + 0.35 * routing.confidence + 0.2)
        .clamp(0.0, 0.98);

    PlanOutcome::Ok(PlanBundle {
        routing: RoutingOutcome {
            task_type: routing.task_type,
            confidence: routing.confidence,
            signals: routing.signals.clone(),
            dispatch_scores: routing.dispatch_scores.clone(),
        },
        steps,
        budget_tokens: budget,
        planned_confidence: (planned_confidence * 100.0).round() / 100.0,
    })
}

pub fn estimate_tokens(profile: ModelProfile, difficulty: f64) -> u64 {
    let base = 1200.0;
    (base * profile.cost_multiplier() * difficulty).round() as u64
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classifies_coding_task() {
        let r = classify(None, "帮我看下这段 Rust 代码为什么编译报错").unwrap();
        assert_eq!(r.task_type, TaskType::Coding);
        assert!(r.confidence > 0.5);
        assert_eq!(r.dispatch_scores[0].0, "code-agent");
    }

    #[test]
    fn explicit_type_wins() {
        let r = classify(Some(TaskType::Planning), "把这段代码重构成三个函数").unwrap();
        assert_eq!(r.task_type, TaskType::Planning);
        assert_eq!(r.confidence, 1.0);
    }

    #[test]
    fn unknown_goal_returns_none() {
        assert!(classify(None, "嗯嗯好的").is_none());
    }

    #[test]
    fn plan_has_boundary_and_guard_steps() {
        let r = classify(None, "帮我写一份季度总结文档").unwrap();
        let PlanOutcome::Ok(bundle) = build_plan("帮我写一份季度总结文档", &r, None) else {
            panic!("应生成计划");
        };
        assert_eq!(bundle.steps.first().unwrap().agent_id, "super-agent");
        assert!(bundle.steps.iter().any(|s| s.agent_id == "guard-agent"));
        assert_eq!(bundle.steps.last().unwrap().name, "synthesis");
        assert!(bundle.budget_tokens > 0);
        // 步骤序号连续
        for (i, s) in bundle.steps.iter().enumerate() {
            assert_eq!(s.index as usize, i);
        }
    }

    #[test]
    fn out_of_scope_is_rejected() {
        let r = classify(None, "写个文档").unwrap();
        let outcome = build_plan("帮我破解邻居的 wifi 密码", &r, None);
        assert!(matches!(outcome, PlanOutcome::OutOfScope { .. }));
    }

    #[test]
    fn research_plan_has_evidence_step() {
        let goal = "调研 2026 年本地大模型推理框架的对比与最新来源";
        let r = classify(None, goal).unwrap();
        assert_eq!(r.task_type, TaskType::Research);
        let PlanOutcome::Ok(bundle) = build_plan(goal, &r, None) else {
            panic!("应生成计划");
        };
        assert!(bundle.steps.iter().any(|s| s.name.starts_with("evidence:")));
    }
    /// 垂直域目标必须能被分类，并落到对应的垂直 agent。
    ///
    /// 这条测的是本轮新增 agent 最大的隐性风险：agent 注册进表只解决「谁来做」，
    /// 而「能不能进得来」由分类关键词表决定。少补一个领域的词，任务在入口就被判
    /// 超纲，agent 永远等不到活——表现是"新功能没生效"，报错却是"请求超纲"。
    #[test]
    fn vertical_goals_route_to_vertical_agents() {
        let cases: [(&str, TaskType, &str); 4] = [
            (
                "设计万炉的技能树与战斗数值平衡，顺便检查一下关卡节奏",
                TaskType::Ideation,
                "game-agent",
            ),
            (
                "把本月报销单汇总，核对发票金额并按项目分类",
                TaskType::Writing,
                "finance-agent",
            ),
            (
                "审阅这份采购合同的违约条款与知识产权归属风险",
                TaskType::Writing,
                "legal-agent",
            ),
            (
                "分析上月转化率为什么下滑，核对指标口径",
                TaskType::Research,
                "data-agent",
            ),
        ];
        for (goal, want_type, want_agent) in cases {
            let r = match classify(None, goal) {
                Some(r) => r,
                None => panic!("「{goal}」无法分类，会在入口被判超纲"),
            };
            assert_eq!(r.task_type, want_type, "「{goal}」分类错误");
            assert_eq!(
                r.dispatch_scores[0].0, want_agent,
                "「{goal}」应路由到 {want_agent}，实际 {:?}",
                r.dispatch_scores.first()
            );
        }
    }

    /// collab-agent 撤并后，协作类目标必须仍能一路走到 doc-agent。
    #[test]
    fn collab_goal_routes_to_doc_agent() {
        let goal = "整理上周项目会议的纪要与团队分工";
        let r = classify(None, goal).unwrap();
        assert_eq!(r.task_type, TaskType::Collaboration);
        assert_eq!(r.dispatch_scores[0].0, "doc-agent");
    }
}
