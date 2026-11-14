//! Agent 注册表：统一超级 agent 层 + 子 agents，每个 agent 自带垂直知识、专业文档、
//! 专业 skill、权限与沙箱画像。这是"组织与组合管理"的数据真源。

use crate::domain::task::{ModelProfile, TaskType};
use crate::security::rbac::Permission;
use serde::Serialize;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum AgentKind {
    Super,
    Sub,
}

#[derive(Debug, Clone, Serialize)]
pub struct AgentSpec {
    pub id: &'static str,
    pub kind: AgentKind,
    pub name: &'static str,
    pub domain: &'static str,
    pub summary: &'static str,
    pub vertical_knowledge: &'static [&'static str],
    pub doc_refs: &'static [&'static str],
    pub skills: &'static [&'static str],
    pub permissions: &'static [Permission],
    pub sandbox_profile: &'static str,
    pub default_model: ModelProfile,
    pub task_types: &'static [TaskType],
    /// 调度用关键词（命中即加权）
    pub keywords: &'static [&'static str],
    pub max_concurrency: u8,
}

const ALL_TASK_TYPES: &[TaskType] = &[
    TaskType::Coding,
    TaskType::Writing,
    TaskType::Research,
    TaskType::Planning,
    TaskType::Ideation,
    TaskType::Collaboration,
];

const ORCH_TRUSTED: &[Permission] = &[
    Permission::Read,
    Permission::Write,
    Permission::SandboxExec,
    Permission::MemoryWriteShared,
    Permission::MemoryWriteSystem,
    Permission::CrossAgent,
    Permission::Admin,
];

const WORKER: &[Permission] = &[
    Permission::Read,
    Permission::Write,
    Permission::SandboxExec,
    Permission::MemoryWriteShared,
    Permission::CrossAgent,
];

const READONLY: &[Permission] = &[Permission::Read];

pub const AGENTS: &[AgentSpec] = &[
    AgentSpec {
        id: "super-agent",
        kind: AgentKind::Super,
        name: "统一超级 Agent",
        domain: "总控与编排",
        summary: "唯一入口：理解意图、判定能力边界、拆分任务、调度子 agents、收敛结果并做最终交付校验。",
        vertical_knowledge: &["任务分解模式", "能力边界判定", "多 agent 协作协议", "成本与预算控制"],
        doc_refs: &[
            "docs/01-overview.md",
            "docs/03-division-of-labor.md",
            "docs/05-boundary-handling.md",
        ],
        skills: &["intent-routing", "task-decomposition", "result-synthesis", "escalation"],
        permissions: ORCH_TRUSTED,
        sandbox_profile: "orchestrator-no-fs-write",
        default_model: ModelProfile::Deep,
        task_types: ALL_TASK_TYPES,
        keywords: &["综合", "整体", "帮我", "全流程", "一站式", "统筹"],
        max_concurrency: 8,
    },
    AgentSpec {
        id: "code-agent",
        kind: AgentKind::Sub,
        name: "Code Agent",
        domain: "编程开发与运行态",
        // 本轮裁撤运维 agent：它登记的 task_types 只有 Coding，与本 agent 完全重合，
        // 按 A1（任务类型组合不重合）本就不该单独立册。但它的知识不丢——运维排障
        // 本来就是"代码跑起来之后"的同一个域，并入这里比留两个抢同一批任务更实在。
        summary: "代码生成、重构、审查、单测补齐、依赖与构建问题定位，以及本机与服务运行态排障（日志、端口、进程、磁盘、环境依赖）；只输出可编译可运行的产物与可复现的排障结论。",
        vertical_knowledge: &[
            "Rust 所有权与生命周期",
            "API 设计",
            "SQL 与索引",
            "CI 构建",
            "Windows 服务与进程模型",
            "日志检索与端口排查",
            "磁盘与文件系统诊断",
        ],
        doc_refs: &[
            "docs/kb/code-standards.md",
            "docs/kb/review-checklist.md",
            "docs/kb/ops-runbook.md",
        ],
        skills: &[
            "code-review", "unit-test-gen", "refactor-plan", "build-triage",
            "log-triage", "port-check", "disk-audit",
        ],
        permissions: WORKER,
        sandbox_profile: "sandbox-repo-rw",
        default_model: ModelProfile::Deep,
        task_types: &[TaskType::Coding],
        keywords: &[
            "代码", "编译", "报错", "重构", "函数", "sql", "rust", "接口", "单测",
            "系统", "进程", "端口", "日志", "磁盘", "服务", "环境", "依赖", "内存占用",
        ],
        max_concurrency: 4,
    },
    // 原 `collab-agent` 于 2026-11 撤并：它的产出（纪要、对齐说明、催办清单）在
    // 交付形态上与"写作"完全同构，且共用同一个 `sandbox-no-exec` 画像，唯一差异只是
    // 关键词表。单独挂一个 agent 并没有换来任何独立能力，反而让 Collaboration 类任务
    // 多一跳调度。这里把它并入 doc-agent，能力不减，路由少一跳。
    AgentSpec {
        id: "doc-agent",
        kind: AgentKind::Sub,
        name: "Doc Agent",
        domain: "文档写作",
        summary: "长文写作、结构化报告、合同与邮件、会议纪要与多方对齐、格式规范与术语一致性。",
        vertical_knowledge: &[
            "中文书面语规范",
            "结构化写作",
            "公文与商务邮件",
            "会议纪要结构",
            "术语表管理",
        ],
        doc_refs: &["docs/kb/writing-style.md", "docs/kb/glossary.md"],
        skills: &[
            "outline-writer",
            "style-enforcer",
            "summarize",
            "meeting-notes",
            "alignment-brief",
            "follow-up",
        ],
        permissions: WORKER,
        sandbox_profile: "sandbox-doc-rw",
        default_model: ModelProfile::Standard,
        task_types: &[TaskType::Writing, TaskType::Collaboration],
        keywords: &[
            "文档", "写作", "报告", "总结", "邮件", "润色", "文案", "周报", "纪要", "会议",
            "对齐", "分工", "催办", "沟通",
        ],
        max_concurrency: 6,
    },
    AgentSpec {
        id: "research-agent",
        kind: AgentKind::Sub,
        name: "Research Agent",
        domain: "信息检索与研究",
        summary: "多源检索、来源分级、证据链留存、事实核对；无来源不下结论。",
        vertical_knowledge: &["检索式设计", "来源可信度分级", "事实核查流程"],
        doc_refs: &["docs/kb/source-grading.md"],
        skills: &["web-search", "source-grading", "evidence-log"],
        permissions: WORKER,
        sandbox_profile: "sandbox-net-readonly",
        default_model: ModelProfile::Standard,
        task_types: &[TaskType::Research],
        keywords: &["查", "搜索", "资料", "对比", "调研", "最新", "来源", "核实"],
        max_concurrency: 6,
    },
    AgentSpec {
        id: "planner-agent",
        kind: AgentKind::Sub,
        name: "Planner Agent",
        domain: "任务规划",
        summary: "目标拆解、里程碑与依赖排序、风险登记与回滚预案、验收口径定义。",
        vertical_knowledge: &["WBS 拆解", "关键路径", "风险登记", "验收标准编写"],
        doc_refs: &["docs/kb/planning-templates.md"],
        skills: &["wbs", "risk-register", "milestone-plan"],
        permissions: WORKER,
        sandbox_profile: "sandbox-no-exec",
        default_model: ModelProfile::Standard,
        task_types: &[TaskType::Planning],
        keywords: &["规划", "计划", "排期", "里程碑", "拆解", "待办", "路线"],
        max_concurrency: 4,
    },
    AgentSpec {
        id: "ideation-agent",
        kind: AgentKind::Sub,
        name: "Ideation Agent",
        domain: "创意构思",
        summary: "发散生成、约束内收敛、方案打分与取舍理由；输出可执行候选而非空想。",
        vertical_knowledge: &["发散收敛双钻", "方案评分矩阵", "命名与叙事"],
        doc_refs: &["docs/kb/ideation-methods.md"],
        skills: &["brainstorm", "score-matrix", "story-craft"],
        permissions: WORKER,
        sandbox_profile: "sandbox-no-exec",
        default_model: ModelProfile::Standard,
        task_types: &[TaskType::Ideation],
        keywords: &["创意", "点子", "命名", "方案", "脑洞", "设计", "构思"],
        max_concurrency: 4,
    },
    
    // ---- 垂直域 agent（2026-11 新增）----
    // 判断标准只有两条：① 有一批「只有懂这个行业才问得对」的目标；② 通用 agent
    // 做这类目标时会退化成正确但无用的模板。四条都满足，才新增一个 agent。
    AgentSpec {
        id: "game-agent",
        kind: AgentKind::Sub,
        name: "Game Agent",
        domain: "游戏设计",
        summary: "玩法核心循环、数值曲线与通胀控制、关卡节奏与难度曲线、系统奖励结构；产出可评审的设计检查表。",
        vertical_knowledge: &[
            "核心循环设计",
            "数值曲线与通胀控制",
            "关卡节奏与难度曲线",
            "系统耦合与奖励结构",
        ],
        doc_refs: &["docs/kb/game-design-checklist.md"],
        skills: &["core-loop-canvas", "balance-audit", "level-pacing", "design-review"],
        permissions: WORKER,
        sandbox_profile: "sandbox-no-exec",
        default_model: ModelProfile::Standard,
        task_types: &[TaskType::Ideation, TaskType::Planning],
        keywords: &[
            "玩法", "关卡", "数值", "战斗", "技能", "修仙", "平衡", "掉落", "肉鸽", "策划",
            "美术",
        ],
        max_concurrency: 3,
    },
    AgentSpec {
        id: "finance-agent",
        kind: AgentKind::Sub,
        name: "Finance Agent",
        domain: "财务核算",
        summary: "费用口径（期间/主体/含税）、单据链五单一致、预算与成本归集、异常付款信号；只给可核对清单，不猜金额。",
        vertical_knowledge: &[
            "费用口径三问",
            "单据链五单一致",
            "预算与成本归集",
            "异常付款识别",
        ],
        doc_refs: &["docs/kb/finance-checklist.md"],
        skills: &["voucher-chain-check", "caliber-card", "budget-variance", "anomaly-scan"],
        permissions: WORKER,
        sandbox_profile: "sandbox-no-exec",
        default_model: ModelProfile::Standard,
        task_types: &[TaskType::Writing, TaskType::Planning],
        keywords: &[
            "报销", "发票", "对账", "预算", "成本", "账目", "税务", "现金流", "付款", "单据",
        ],
        max_concurrency: 3,
    },
    AgentSpec {
        id: "legal-agent",
        kind: AgentKind::Sub,
        name: "Legal Agent",
        domain: "法务合同",
        summary: "合同条款体检、必备条款缺失与高危句式识别、风险分级与谈判底线；不出法律意见，重大条款交律师复核。",
        vertical_knowledge: &[
            "合同必备条款",
            "违约与赔偿测算",
            "知识产权与数据合规",
            "争议解决与管辖",
        ],
        doc_refs: &["docs/kb/contract-checklist.md"],
        skills: &["clause-audit", "red-flag-scan", "risk-grading", "negotiation-baseline"],
        permissions: WORKER,
        sandbox_profile: "sandbox-no-exec",
        default_model: ModelProfile::Standard,
        task_types: &[TaskType::Writing, TaskType::Research],
        keywords: &[
            "合同", "条款", "协议", "违约", "保密", "知识产权", "管辖", "签署", "甲方", "乙方",
        ],
        max_concurrency: 3,
    },
    AgentSpec {
        id: "data-agent",
        kind: AgentKind::Sub,
        name: "Data Agent",
        domain: "数据分析",
        summary: "指标定义三要素、异常波动归因、报表口径统一、数据质量六查；口径未确认前不下结论。",
        vertical_knowledge: &[
            "指标定义三要素",
            "归因维度树",
            "数据质量六查",
            "样本量与结论强度",
        ],
        doc_refs: &["docs/kb/metrics-handbook.md"],
        skills: &["metric-card", "attribution-tree", "dq-check", "anomaly-detect"],
        permissions: WORKER,
        sandbox_profile: "sandbox-repo-rw",
        default_model: ModelProfile::Standard,
        task_types: &[TaskType::Research, TaskType::Coding],
        keywords: &[
            "指标", "口径", "报表", "同比", "环比", "转化率", "留存", "数据质量", "埋点", "归因",
        ],
        max_concurrency: 4,
    },
    AgentSpec {
        id: "narrative-agent",
        kind: AgentKind::Sub,
        name: "Narrative Agent",
        domain: "叙事与世界观",
        // 立册理由：叙事 bug 从来不是"写得不好看"，而是"信息还没交代就要求读者接受"。
        // 这类判断需要独立的成立条件，跟玩法数值（game）和文字润色（doc）都不是一回事。
        summary: "设定自洽与代价守恒、人物动机链、伏笔埋收台账、信息交代顺序与叙事节奏；只做承重结构审查，不做文字润色（归 doc-agent），不做玩法与数值（归 game-agent）。",
        vertical_knowledge: &[
            "设定自洽与代价守恒",
            "人物动机链四段",
            "伏笔四态台账",
            "信息交代顺序与读者认知差",
        ],
        doc_refs: &["docs/kb/narrative-canon.md"],
        skills: &["canon-audit", "motive-chain", "foreshadow-ledger", "pacing-check"],
        permissions: WORKER,
        sandbox_profile: "sandbox-no-exec",
        default_model: ModelProfile::Standard,
        task_types: &[TaskType::Writing, TaskType::Ideation],
        keywords: &[
            "叙事", "设定", "伏笔", "主线", "支线", "剧情", "人物", "动机", "世界观",
            "大纲", "章节", "黑化",
        ],
        max_concurrency: 3,
    },
    AgentSpec {
        id: "market-agent",
        kind: AgentKind::Sub,
        name: "Market Agent",
        domain: "市场与竞争",
        // 立册理由：结论必须"当场可被反驳"——竞品矩阵、定位、定价阶梯都能落到具体格子，
        // 与 research（查证）和 finance（算账）的产物不重叠。
        summary: "竞品矩阵五维、定位三问、定价阶梯与渠道评估；不做销量预测与财务测算（归 finance-agent），不做检索取证与来源分级（归 research-agent）。",
        vertical_knowledge: &[
            "竞品矩阵与替代方案",
            "定位与差异化证据",
            "价格带与定价阶梯",
            "渠道获客与回本周期",
        ],
        doc_refs: &["docs/kb/market-playbook.md"],
        skills: &["competitor-matrix", "positioning-brief", "pricing-ladder", "channel-scorecard"],
        permissions: WORKER,
        sandbox_profile: "sandbox-no-exec",
        default_model: ModelProfile::Standard,
        task_types: &[TaskType::Research, TaskType::Planning],
        keywords: &[
            "市场", "竞品", "竞对", "定位", "定价", "价格带", "渠道", "投放", "份额", "用户画像",
        ],
        max_concurrency: 3,
    },
    AgentSpec {
        id: "qa-agent",
        kind: AgentKind::Sub,
        name: "QA Agent",
        domain: "质量与验收",
        // 立册理由：验收会上八成的争吵来自"流畅""稳定"这类无法判定的词。把模糊词换成
        // 可测指标、把用例铺到边界与异常路径、把缺陷分级写成门槛，是件独立且可评审的活。
        summary: "验收标准量化、用例按等价类/边界/异常路径铺开、缺陷分级门槛与回归范围裁剪；不做代码实现（归 code-agent），不做玩法设计（归 game-agent）。",
        vertical_knowledge: &[
            "验收标准可测化",
            "等价类与边界设计",
            "缺陷分级门槛",
            "回归范围与风险裁剪",
        ],
        doc_refs: &["docs/kb/qa-test-strategy.md"],
        skills: &["accept-criteria", "case-matrix", "defect-triage", "regression-scope"],
        permissions: WORKER,
        sandbox_profile: "sandbox-no-exec",
        default_model: ModelProfile::Standard,
        task_types: &[TaskType::Coding, TaskType::Collaboration],
        keywords: &[
            "验收", "测试", "用例", "回归", "缺陷", "冒烟", "复现", "通过标准", "质量", "边界",
        ],
        max_concurrency: 3,
    },
    AgentSpec {
        id: "guard-agent",
        kind: AgentKind::Sub,
        name: "Guard Agent",
        domain: "合规与安全审核",
        summary: "对每一步产物做合规、越权与幻觉风险复核；拥有否决权，只读不可写。",
        vertical_knowledge: &["数据分级", "越权判定", "合规红线", "幻觉识别信号"],
        doc_refs: &["docs/05-boundary-handling.md", "docs/kb/compliance-rules.md"],
        skills: &["compliance-scan", "hallucination-check", "permission-audit"],
        permissions: READONLY,
        sandbox_profile: "sandbox-readonly",
        default_model: ModelProfile::Standard,
        task_types: &[],
        keywords: &["合规", "隐私", "敏感", "审核", "风险", "越权"],
        max_concurrency: 8,
    },
];

pub fn find(id: &str) -> Option<&'static AgentSpec> {
    AGENTS.iter().find(|a| a.id == id)
}

pub fn super_agent() -> &'static AgentSpec {
    &AGENTS[0]
}

pub fn sub_agents() -> impl Iterator<Item = &'static AgentSpec> {
    AGENTS.iter().filter(|a| a.kind == AgentKind::Sub)
}

pub fn workers_for(task_type: TaskType) -> Vec<&'static AgentSpec> {
    AGENTS
        .iter()
        .filter(|a| a.kind == AgentKind::Sub && a.task_types.contains(&task_type))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn registry_is_consistent() {
        assert!(find("super-agent").is_some());
        assert_eq!(super_agent().kind, AgentKind::Super);
        assert!(AGENTS.len() >= 12);
        // 每个 task_type 至少有一个子 agent 承接
        for t in ALL_TASK_TYPES {
            assert!(
                !workers_for(*t).is_empty(),
                "task type {} 没有对应子 agent",
                t.as_str()
            );
        }
        // guard-agent 必须只读
        let guard = find("guard-agent").unwrap();
        assert_eq!(guard.permissions, READONLY);
        // id 唯一
        let mut ids: Vec<_> = AGENTS.iter().map(|a| a.id).collect();
        ids.sort_unstable();
        ids.dedup();
        assert_eq!(ids.len(), AGENTS.len());
    }
    /// 撤并 collab-agent 时留下的护栏：Collaboration 类任务必须仍有承接者。
    ///
    /// 单独写而不塞进上面的循环，是因为它守的不是「表里有没有这一行」，而是
    /// 「删 agent 时有没有把整类任务一起删空」——循环也能发现，但报的是泛泛的
    /// 「没有子 agent」，定位成本高。
    #[test]
    fn collaboration_still_has_owner() {
        let owners = workers_for(TaskType::Collaboration);
        assert!(!owners.is_empty(), "Collaboration 类任务没有承接者");
        assert!(
            owners.iter().any(|a| a.id == "doc-agent"),
            "doc-agent 应承接 Collaboration（原 collab-agent 已撤并）"
        );
    }

    /// 防「空壳 agent」：除只读守卫外，每个子 agent 都必须声明可承接的任务类型，
    /// 且带够关键词与垂直知识。否则它在路由里永远拿不到分、在 catalog 里却看起来
    /// 像一项能力——这属于静默失效，比报错更难发现。
    #[test]
    fn no_hollow_agent() {
        for a in AGENTS
            .iter()
            .filter(|a| a.kind == AgentKind::Sub && a.id != "guard-agent")
        {
            assert!(
                !a.task_types.is_empty(),
                "{} 的 task_types 为空，只能靠关键词碰运气命中",
                a.id
            );
            assert!(!a.keywords.is_empty(), "{} 没有关键词，路由永远拿不到分", a.id);
            assert!(
                !a.vertical_knowledge.is_empty(),
                "{} 没有垂直知识声明，属于空壳 agent",
                a.id
            );
        }
        // 本次新增的四个垂直域必须在册，防后续重构被静默删掉
        for id in ["game-agent", "finance-agent", "legal-agent", "data-agent"] {
            assert!(find(id).is_some(), "垂直域 agent {} 缺失", id);
        }
        // collab-agent 已撤并，不得回潮
        assert!(find("collab-agent").is_none(), "collab-agent 已撤并，不应重新注册");
    }

    /// 防「死引用」：`doc_refs` 指向的文件必须真的存在于磁盘上。
    ///
    /// 起因：2026-11 补垂直域知识库之前，13 个 `doc_refs` 全是空引用 —— `docs/kb/`
    /// 目录整体不存在，而当时既没有测试也没有门禁比对这件事。文档引用断裂是静默的：
    /// catalog 里照样列着「参考文档」，点开却是 404，而 agent 的"垂直知识"只是几句
    /// 形容词。所以这里不放行"读不到 docs 就跳过"，读不到本身就是缺陷。
    #[test]
    fn doc_refs_point_to_existing_files() {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
        assert!(
            root.join("docs").is_dir(),
            "编译镜像缺 docs 目录（{}）：先跑 scripts/sync_build.py，\
             本测试不能因为读不到文件就当作通过",
            root.display()
        );
        for a in AGENTS {
            for r in a.doc_refs {
                assert!(
                    root.join(r).is_file(),
                    "{} 的 doc_refs 指向不存在的文件：{}",
                    a.id,
                    r
                );
            }
        }
    }

    /// 防「路由歧义」：同一任务类型下，两个 agent 不得共用同一个关键词。
    ///
    /// `dispatch_scores` 在命中数相同时按表顺序取先者，所以重复关键词会让胜负由
    /// "谁排在前面"决定 —— 结果稳定，但是错的；更糟的是调整 AGENTS 顺序会悄悄
    /// 换掉承接人，而所有测试照样全绿。
    #[test]
    fn keywords_are_unique_within_task_type() {
        use std::collections::HashMap;
        let mut seen: HashMap<(String, &str), Vec<&str>> = HashMap::new();
        for a in sub_agents() {
            for &t in a.task_types {
                for &k in a.keywords {
                    seen.entry((t.as_str().to_string(), k)).or_default().push(a.id);
                }
            }
        }
        let mut clashes: Vec<String> = seen
            .iter()
            .filter(|(_, owners)| owners.len() > 1)
            .map(|((t, k), owners)| format!("{t} 下「{k}」被 {owners:?} 共用"))
            .collect();
        clashes.sort();
        assert!(clashes.is_empty(), "关键词重复会造成路由歧义：{clashes:?}");
    }
}
