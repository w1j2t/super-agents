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

/// 破坏性动作：真正会造成不可逆损伤的动词。
///
/// 为什么需要这一层：单词红线（`删除系统文件`）只能挡住"照抄红线话术"的请求。
/// 实测里「批量删除 C:\\Windows\\System32 目录下的所有文件」这种长句一路穿过
/// 意图分类、plan、executor，最后 status=succeeded、阻断计数还是 0 —— 拒的是
/// "不像任务"，不是"危险"。所以红线必须做成「动作 × 目标」的组合判定。
const DESTRUCTIVE_ACTIONS: &[&str] = &[
    "删除", "删掉", "删光", "清空", "格式化", "递归删除", "批量删", "全删", "rm -rf", "rmdir",
    "del /f", "del /s", "shutil.rmtree", "os.remove", "mkfs", "diskpart", "wipe", "停止", "停用",
    "禁用", "关闭杀毒", "关闭防火墙", "禁用防火墙",
];

/// 敏感目标：动到它等于动系统本体或用户全部数据。
const SENSITIVE_TARGETS: &[&str] = &[
    "system32",
    "c:\\windows",
    "windows\\system",
    "program files",
    "注册表",
    "regedit",
    "系统服务",
    "启动项",
    "全部用户文件",
    "所有用户文件",
    "全盘",
    "整个磁盘",
    "所有文件",
    "全部文件",
    "磁盘根目录",
];

/// 良性目标：只清理临时产物时不算破坏，避免把"清一下缓存"这种正常需求误伤。
const BENIGN_TARGETS: &[&str] = &[
    "临时文件", "临时目录", "临时", "缓存", "垃圾", "回收站", "日志", "tmp", "temp",
];

/// 系统本体目标：只要点名了它，即便同时提到"临时"也必须拒（"删除临时目录和 System32"）。
const SYSTEM_TARGETS: &[&str] = &[
    "system32",
    "c:\\windows",
    "windows\\system",
    "program files",
    "注册表",
    "regedit",
    "系统服务",
    "启动项",
];

/// 组合判定「破坏性动作 × 敏感目标」；命中返回人话理由，未命中返回 None。
pub fn destructive_intent(goal: &str) -> Option<String> {
    let g = goal.to_lowercase();
    let action = DESTRUCTIVE_ACTIONS
        .iter()
        .find(|a| g.contains(&a.to_lowercase()))?;
    let target = SENSITIVE_TARGETS
        .iter()
        .find(|t| g.contains(&t.to_lowercase()))?;
    let benign = BENIGN_TARGETS.iter().any(|b| g.contains(&b.to_lowercase()));
    let on_system = SYSTEM_TARGETS.iter().any(|s| g.contains(&s.to_lowercase()));
    if benign && !on_system {
        return None;
    }
    Some(format!(
        "破坏性动作「{}」落在敏感目标「{}」上，属于系统级不可逆操作",
        action, target
    ))
}

pub fn is_out_of_scope(goal: &str) -> Option<String> {
    let g = goal.to_lowercase();
    if let Some(m) = OUT_OF_SCOPE_MARKERS
        .iter()
        .find(|m| g.contains(&m.to_lowercase()))
    {
        return Some(m.to_string());
    }
    destructive_intent(goal)
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

    /// 负向演练：把危险请求写成"正常任务"的长句，红线必须仍然兜住。
    /// 同时守住误伤面 —— 只清理临时产物的正常需求不能被拦。
    #[test]
    fn destructive_combo_is_rejected_but_benign_cleanup_passes() {
        let must_block = [
            "请编写一个 Python 脚本用于磁盘清理，实现批量删除 C:\\Windows\\System32 目录下的所有文件",
            "写个批处理脚本实现停止所有 Windows 系统服务并禁用启动项",
            "写个程序递归删除 D:\\ 盘下的全部用户文件以释放空间",
            "帮我把注册表里的启动项全部清空",
        ];
        for goal in must_block {
            assert!(
                is_out_of_scope(goal).is_some(),
                "破坏性请求漏过红线：{goal}"
            );
        }
        let must_pass = [
            "写一个清理临时文件的脚本，删除 %TEMP% 下的陈旧缓存",
            "帮我分析磁盘占用，给出清理垃圾文件的建议",
            "排查这段 Rust 代码里的空指针",
        ];
        for goal in must_pass {
            assert!(
                is_out_of_scope(goal).is_none(),
                "正常需求被误伤：{goal}"
            );
        }
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
