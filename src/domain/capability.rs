//! 能力矩阵：26 项内置机制，每项都必须具备五要素——
//! 触发条件 / 负责 agents / 权限边界 / 可观测信号 / 降级或终止条件。

use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
pub struct CapabilitySpec {
    pub id: &'static str,
    pub name: &'static str,
    pub category: &'static str,
    /// 何时启动
    pub trigger: &'static str,
    /// 谁负责
    pub owner_agents: &'static [&'static str],
    /// 能做什么 / 不能做什么
    pub permission_boundary: &'static str,
    /// 可观测信号（指标名或事件名）
    pub observability_signals: &'static [&'static str],
    /// 什么情况下降级、什么情况下终止
    pub degradation_or_abort: &'static str,
}

pub const CAPABILITIES: &[CapabilitySpec] = &[
    CapabilitySpec {
        id: "automation",
        name: "全自动化执行",
        category: "执行",
        trigger: "用户提交目标（POST /api/v1/tasks）后立即触发，无需二次编排确认。",
        owner_agents: &["super-agent", "planner-agent"],
        permission_boundary: "自动执行仅限 Read/Write/CrossAgent；涉及 SandboxExec 的步骤逐条过 RBAC，涉及 system 级写入一律不自动执行。",
        observability_signals: &["sa_tasks_created_total", "sa_task_steps_total", "task.trace[].phase"],
        degradation_or_abort: "连续 2 个步骤失败即熔断转 failed；预算耗尽转 aborted_budget；用户取消转 cancelled。",
    },
    CapabilitySpec {
        id: "lazy_loading",
        name: "懒加载",
        category: "性能",
        trigger: "仅在任务实际命中某 agent / skill / 知识文档时才装配其上下文。",
        owner_agents: &["super-agent"],
        permission_boundary: "只能加载调用方有权访问的文档与记忆（private 仅本人，system 需 owner/admin）。",
        observability_signals: &["sa_context_tokens_saved_total", "sa_agents_loaded_total"],
        degradation_or_abort: "上下文超阈值时按优先级截断：共享记忆摘要 > 垂直知识摘录 > 文档全文；截断仍超限则降档模型。",
    },
    CapabilitySpec {
        id: "full_visualization",
        name: "全可视化",
        category: "可观测",
        trigger: "任何任务执行、调度决策、权限判定均产生可查询的结构化记录。",
        owner_agents: &["super-agent"],
        permission_boundary: "用户只能查看自己的任务轨迹与审计；全局视图需 Admin 权限。",
        observability_signals: &["GET /api/v1/tasks/{id}/trace", "GET /api/v1/audit", "GET /metrics"],
        degradation_or_abort: "审计队列有界（默认 5000 条），溢出时丢弃最旧事件并在 sa_audit_dropped_total 计数。",
    },
    CapabilitySpec {
        id: "power_allocation",
        name: "权力分配",
        category: "治理",
        trigger: "每次跨 agent 调用、共享记忆写入、系统级操作前判定。",
        owner_agents: &["super-agent", "guard-agent"],
        permission_boundary: "owner > admin > member；system 作用域记忆与用户管理仅 owner/admin；guard-agent 只读且有一票否决。",
        observability_signals: &["sa_authz_denied_total", "audit.decision=deny"],
        degradation_or_abort: "越权即拒绝（403）并记录审计，不做静默降级；同一请求连续 3 次越权则终止该任务。",
    },
    CapabilitySpec {
        id: "security",
        name: "安全约束",
        category: "治理",
        trigger: "所有请求入口、密码处理、token 校验、敏感写操作。",
        owner_agents: &["super-agent", "guard-agent"],
        permission_boundary: "不落明文密码与 token；密钥只来自环境变量或受保护密钥文件；禁止跨用户资源读取。",
        observability_signals: &["sa_login_failures_total", "sa_rate_limited_total", "sa_authz_denied_total"],
        degradation_or_abort: "登录连续失败 5 次锁定 15 分钟；限流命中返回 429 + Retry-After；熵源不可用直接拒绝启动。",
    },
    CapabilitySpec {
        id: "error_correction",
        name: "错误纠错",
        category: "执行",
        trigger: "单步执行返回可重试错误（超时、瞬时失败、工具返回 5xx）。",
        owner_agents: &["super-agent", "code-agent"],
        permission_boundary: "重试不得改变原步骤的权限级别与沙箱画像，禁止以重试绕过授权。",
        observability_signals: &["sa_task_retries_total", "sa_task_step_failures_total"],
        degradation_or_abort: "同一步骤最多重试 2 次（指数退避 200ms/400ms）；同类错误第 2 次仍失败即熔断，不无限重试。",
    },
    CapabilitySpec {
        id: "self_purification",
        name: "自净化",
        category: "进化",
        trigger: "任务结束、记忆写入冲突、审计队列膨胀、过期会话堆积时。",
        owner_agents: &["super-agent"],
        permission_boundary: "清理与去重只作用于自有数据与过期数据；不得删除他人记忆或审计留痕（仅裁剪最旧）。",
        observability_signals: &["sa_memory_dedup_total", "sa_sessions_revoked_total", "sa_audit_dropped_total"],
        degradation_or_abort: "同 key 记忆写入即版本递增覆盖（不产生重复条目）；清理失败不阻塞主流程，仅告警。",
    },
    CapabilitySpec {
        id: "correctness_check",
        name: "正确性校验",
        category: "质量",
        trigger: "每步产物生成后、最终结果汇总前。",
        owner_agents: &["super-agent", "guard-agent", "code-agent"],
        permission_boundary: "校验器只读产物与轨迹，不得修改产物内容；发现不一致只能标记并要求重做。",
        observability_signals: &["sa_correctness_checks_total", "task.agreement_score"],
        degradation_or_abort: "一致性得分 < 0.6 标记 partially_succeeded 并附失败原因；关键校验失败直接 failed。",
    },
    CapabilitySpec {
        id: "compliance_check",
        name: "合规检查",
        category: "治理",
        trigger: "每一步产物落库前、记忆写入前。",
        owner_agents: &["guard-agent"],
        permission_boundary: "guard-agent 只读；命中红线清单时可否决，但不得自行改写内容或放宽规则。",
        observability_signals: &["sa_compliance_blocks_total", "audit.action=compliance_check"],
        degradation_or_abort: "命中红线（凭据、越权数据、违法内容）→ 直接终止该步骤并整任务转 failed，不降级放行。",
    },
    CapabilitySpec {
        id: "shared_memory",
        name: "对话共享记忆",
        category: "记忆",
        trigger: "任务创建时读取相关记忆；任务结束后写入经验记忆。",
        owner_agents: &["super-agent", "all-sub-agents"],
        permission_boundary: "private 仅本人可读写；shared 本人与协作 agent 可读、member 可写；system 仅 owner/admin 可写。",
        observability_signals: &["sa_memory_writes_total", "sa_memory_reads_total", "memory.version"],
        degradation_or_abort: "记忆服务异常时任务继续执行但标记 degraded，并在结果中声明未使用历史上下文。",
    },
    CapabilitySpec {
        id: "full_chain_monitoring",
        name: "全链路监控",
        category: "可观测",
        trigger: "每个 HTTP 请求、每个任务步骤、每次外部调用。",
        owner_agents: &["super-agent"],
        permission_boundary: "监控数据只读；request_id 贯穿全链路，日志不得包含密码与 token。",
        observability_signals: &["sa_http_requests_total", "sa_task_duration_ms", "sa_uptime_seconds"],
        degradation_or_abort: "指标写入失败不影响业务（best-effort）；连续 30s 无心跳则 /health/ready 返回 503。",
    },
    CapabilitySpec {
        id: "model_scheduling",
        name: "按任务模型调度",
        category: "执行",
        trigger: "步骤生成时按任务类型与难度选择 fast/standard/deep 档位。",
        owner_agents: &["super-agent", "planner-agent"],
        permission_boundary: "用户可指定 requested_model，但不得超过其配额；系统降档无需用户确认，升档必须显式请求。",
        observability_signals: &["sa_model_profile_used_total", "task.tokens_used"],
        degradation_or_abort: "预算不足时按 deep→standard→fast 逐级降档；降到 fast 仍超预算则终止该步骤并标记 aborted_budget。",
    },
    CapabilitySpec {
        id: "tool_invocation",
        name: "工具调用",
        category: "执行",
        trigger: "步骤声明需要工具（检索、读写、执行）时。",
        owner_agents: &["code-agent", "research-agent", "doc-agent"],
        permission_boundary: "工具白名单 + 参数 schema 校验；跨 agent 调用需 CrossAgent 权限；禁止调用未注册工具。",
        observability_signals: &["sa_tool_calls_total", "sa_tool_failures_total"],
        degradation_or_abort: "同一工具同一参数失败 2 次即停用该工具并改用备用路径；无备用则标记步骤失败。",
    },
    CapabilitySpec {
        id: "orchestration",
        name: "流程编排",
        category: "执行",
        trigger: "任务提交后由 super-agent 生成 DAG/线性计划。",
        owner_agents: &["super-agent", "planner-agent"],
        permission_boundary: "编排只能调用注册表中存在的 agent 与其声明权限的交集，不得提升任何 agent 权限。",
        observability_signals: &["sa_task_plan_steps", "task.plan[].agent_id"],
        degradation_or_abort: "无可承接 agent → 直接返回 out_of_scope 而非硬编；步骤数超过 12 时拆分为子任务。",
    },
    CapabilitySpec {
        id: "loop_error_handling",
        name: "循环报错处理",
        category: "执行",
        trigger: "同一步骤重复失败、或 agent 间互相推诿形成调用环。",
        owner_agents: &["super-agent"],
        permission_boundary: "熔断器只能停止执行，不能修改历史轨迹；恢复需重新提交任务。",
        observability_signals: &["sa_task_circuit_open_total", "sa_task_retries_total"],
        degradation_or_abort: "单步重试上限 2 次、单任务累计失败步骤上限 3；触发即熔断，任务转 failed 并输出根因摘要。",
    },
    CapabilitySpec {
        id: "data_flywheel",
        name: "数据飞轮",
        category: "进化",
        trigger: "任务结束后沉淀经验（成功的做法与踩坑），供后续调度打分使用。",
        owner_agents: &["super-agent", "research-agent"],
        permission_boundary: "只沉淀任务摘要与结论，不沉淀用户隐私原文；system 级沉淀需 owner/admin。",
        observability_signals: &["sa_flywheel_events_total", "memory.tags"],
        degradation_or_abort: "沉淀失败不影响任务结果；同一 task_type 沉淀条目超过 200 条时按访问热度淘汰。",
    },
    CapabilitySpec {
        id: "evaluation_driven",
        name: "评估驱动检查",
        category: "质量",
        trigger: "每轮任务结束、以及每 100 次任务执行的漂移检查。",
        owner_agents: &["super-agent", "guard-agent"],
        permission_boundary: "评估只读指标与轨迹；不得为了指标好看而跳过高风险校验。",
        observability_signals: &["sa_eval_success_rate", "sa_eval_avg_agreement", "sa_eval_drift_alerts_total"],
        degradation_or_abort: "成功率环比下降超过 15% 触发告警并收紧重试与预算；连续两轮下降则自动降档默认模型。",
    },
    CapabilitySpec {
        id: "rag_tool_augmentation",
        name: "工具增强（RAG）",
        category: "质量",
        trigger: "步骤需要专业文档/垂直知识支撑时，先检索再生成。",
        owner_agents: &["research-agent", "code-agent", "doc-agent"],
        permission_boundary: "只检索调用方有权访问的文档；引用必须带来源标识，无来源内容不得作为事实输出。",
        observability_signals: &["sa_rag_retrievals_total", "sa_rag_misses_total"],
        degradation_or_abort: "检索为空时输出降级为通用建议并显式声明“未命中知识库”，禁止编造来源。",
    },
    CapabilitySpec {
        id: "observability_alerting",
        name: "可观测与告警",
        category: "可观测",
        trigger: "错误率、延迟、限流、越权、预算超支等阈值命中。",
        owner_agents: &["super-agent", "code-agent"],
        permission_boundary: "告警只做通知与标记；自动处置限于停止重试与降档，不做数据删除。",
        observability_signals: &["sa_alerts_total", "sa_http_5xx_total", "sa_task_failures_total"],
        degradation_or_abort: "同一条告警 5 分钟内去重；告警风暴时只保留严重级别，其余计数聚合。",
    },
    CapabilitySpec {
        id: "security_governance",
        name: "安全与治理",
        category: "治理",
        trigger: "权限变更、记忆作用域变更、管理接口调用。",
        owner_agents: &["guard-agent", "super-agent"],
        permission_boundary: "角色变更仅 owner；审计只追加不修改；禁用用户即时生效（token 校验时检查状态）。",
        observability_signals: &["sa_admin_actions_total", "audit.decision"],
        degradation_or_abort: "治理组件异常时采用保守策略：拒绝新的高权限写入，已有会话保持只读。",
    },
    CapabilitySpec {
        id: "org_composition",
        name: "组织与组合管理",
        category: "治理",
        trigger: "agent 注册、组合（super + sub 编组）、并发与配额分配时。",
        owner_agents: &["super-agent"],
        permission_boundary: "组合不得超出被编组 agent 的权限交集；新增 agent 必须声明 sandbox 画像与权限集。",
        observability_signals: &["sa_agents_registered", "sa_agent_concurrency"],
        degradation_or_abort: "注册表校验失败（id 重复、权限越界、无沙箱画像）拒绝加载；运行期超并发则排队而非丢弃。",
    },
    CapabilitySpec {
        id: "cost_compute_optimization",
        name: "成本与算力优化",
        category: "性能",
        trigger: "任务创建时分配 token 预算；步骤执行时实时扣减。",
        owner_agents: &["super-agent"],
        permission_boundary: "预算只能收紧不能透支；降档不改变权限；提额需用户显式指定。",
        observability_signals: &["sa_tokens_used_total", "sa_budget_downgrades_total"],
        degradation_or_abort: "预算消耗 80% 时告警并停止升档；100% 时终止剩余步骤并标记 aborted_budget。",
    },
    CapabilitySpec {
        id: "context_optimization",
        name: "上下文优化",
        category: "性能",
        trigger: "装配上下文前，估算 token 需求。",
        owner_agents: &["super-agent"],
        permission_boundary: "压缩只能摘要自有与已授权内容，不得跨用户拼接上下文。",
        observability_signals: &["sa_context_tokens_used", "sa_context_truncations_total"],
        degradation_or_abort: "超限按“共享记忆摘要 → 垂直知识摘录 → 文档全文”顺序裁剪；仍超限则降档模型或拆分子任务。",
    },
    CapabilitySpec {
        id: "agent_dispatching",
        name: "按任务 agents 调配",
        category: "执行",
        trigger: "计划步骤需要指派执行者时。",
        owner_agents: &["super-agent"],
        permission_boundary: "只能指派注册表中声明了对应 task_type 的子 agent；不得临时创造 agent。",
        observability_signals: &["sa_agent_dispatches_total", "task.plan[].agent_id"],
        degradation_or_abort: "最高匹配分低于阈值 0.45 时不上报硬编结果，而是返回 out_of_scope 并说明缺什么能力。",
    },
    CapabilitySpec {
        id: "anti_hallucination",
        name: "防幻觉",
        category: "质量",
        trigger: "任何产出事实性陈述、数值、引用、代码符号时。",
        owner_agents: &["guard-agent", "research-agent"],
        permission_boundary: "无来源或检索未命中时必须声明不确定性；禁止虚构文件路径、接口、版本号。",
        observability_signals: &["sa_hallucination_flags_total", "task.agreement_score"],
        degradation_or_abort: "关键主张无来源支撑 → 降级为“未确认”并降低 agreement_score；两次复核仍无依据则拒绝输出该主张。",
    },
    CapabilitySpec {
        id: "sandbox",
        name: "沙箱",
        category: "安全",
        trigger: "代码执行、浏览器访问、文件写入、外部工具调用前。",
        owner_agents: &["code-agent", "research-agent"],
        permission_boundary: "沙箱内仅允许声明的路径与网络白名单；orchestrator 默认无文件写权限；只读 agent 运行在 readonly 沙箱。",
        observability_signals: &["sa_sandbox_runs_total", "sa_sandbox_violations_total"],
        degradation_or_abort: "沙箱拒绝启动（策略缺失/越界路径）时步骤直接失败，不降级到宿主执行。",
    },
];

pub fn find(id: &str) -> Option<&'static CapabilitySpec> {
    CAPABILITIES.iter().find(|c| c.id == id)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_capability_has_five_elements() {
        assert_eq!(CAPABILITIES.len(), 26, "能力矩阵必须覆盖 26 项机制");
        for c in CAPABILITIES {
            assert!(!c.trigger.trim().is_empty(), "{} 缺触发条件", c.id);
            assert!(!c.owner_agents.is_empty(), "{} 缺负责 agents", c.id);
            assert!(!c.permission_boundary.trim().is_empty(), "{} 缺权限边界", c.id);
            assert!(
                !c.observability_signals.is_empty(),
                "{} 缺可观测信号",
                c.id
            );
            assert!(
                !c.degradation_or_abort.trim().is_empty(),
                "{} 缺降级/终止条件",
                c.id
            );
        }
    }

    #[test]
    fn capability_ids_are_unique_and_complete() {
        let mut ids: Vec<_> = CAPABILITIES.iter().map(|c| c.id).collect();
        ids.sort_unstable();
        let before = ids.len();
        ids.dedup();
        assert_eq!(before, ids.len(), "能力 id 必须唯一");
        for required in [
            "automation",
            "lazy_loading",
            "full_visualization",
            "power_allocation",
            "security",
            "error_correction",
            "self_purification",
            "correctness_check",
            "compliance_check",
            "shared_memory",
            "full_chain_monitoring",
            "model_scheduling",
            "tool_invocation",
            "orchestration",
            "loop_error_handling",
            "data_flywheel",
            "evaluation_driven",
            "rag_tool_augmentation",
            "observability_alerting",
            "security_governance",
            "org_composition",
            "cost_compute_optimization",
            "context_optimization",
            "agent_dispatching",
            "anti_hallucination",
            "sandbox",
        ] {
            assert!(find(required).is_some(), "缺少能力: {required}");
        }
    }

    #[test]
    fn owners_refer_to_registered_agents() {
        for c in CAPABILITIES {
            for owner in c.owner_agents {
                if *owner == "all-sub-agents" {
                    continue;
                }
                assert!(
                    crate::domain::agent::find(owner).is_some(),
                    "能力 {} 引用了未注册 agent: {}",
                    c.id,
                    owner
                );
            }
        }
    }
}
