# 02 能力清单（26 项内置机制）

> 本文件的事实部分由 `scripts/gen-spec-docs.py` 从源码生成，**请勿手改**。
> 改完 `src/domain/*.rs` 后执行 `python scripts/gen-spec-docs.py`；
> CI 或提交前用 `python scripts/gen-spec-docs.py --check` 校验是否漂移。

每项能力都必须具备五要素：**触发条件 / 负责 agents / 权限边界 / 可观测信号 / 降级或终止条件**。
缺任一要素即视为规格不完整，`tests/spec_matrix.rs` 会直接判失败。

## 0. 原文 25 条 → 本表条目（逐条对齐）

首个提示词点名的 25 条能力，逐条落到下表的具体条目上。**原文哪一条找不到落点，门禁 12 直接红。**

| 原文序号 | 原文术语 | 本表条目（能力 ID） | 说明 |
| --- | --- | --- | --- |
| 1 | 自动化 | `automation` | — |
| 2 | 懒加载 | `lazy_loading` | — |
| 3 | 全可视化 | `full_visualization` | — |
| 4 | 权力分配 | `power_allocation` | — |
| 5 | 安全 | `security` | 与原文第 19 条「安全与治理」为两件事：本条指原子权限与红线，条 19 指治理体系 |
| 6 | 错误纠错 | `error_correction` | — |
| 7 | 自净化 | `self_purification` | — |
| 8 | 正确性校验 | `correctness_check` | — |
| 9 | 合规检查 | `compliance_check` | — |
| 10 | 对话共享记忆 | `shared_memory` | — |
| 11 | 全链路监控 | `full_chain_monitoring` | — |
| 12 | 按任务模型调度 | `model_scheduling` | 档位 fast / standard / deep |
| 13 | 工具调用 | `tool_invocation` | — |
| 14 | 流程编排 | `orchestration` | — |
| 15 | 循环报错处理 | `loop_error_handling` | 重试上限 + 失败步熔断 `3/3` |
| 16 | 数据飞轮 | `data_flywheel` | — |
| 17 | 评估驱动检查与工具增强（即 RAG） | `evaluation_driven` **+** `rag_tool_augmentation` | **原文合写一条，实现拆为两条**：评估驱动=质量闭环，RAG=检索增强。故条目数 26 而原文 25 |
| 18 | 可观测与告警 | `observability_alerting` | — |
| 19 | 安全与治理 | `security_governance` | 见条 5 说明 |
| 20 | 组织与组合管理 | `org_composition` | — |
| 21 | 成本与算力优化 | `cost_compute_optimization` | — |
| 22 | 上下文优化 | `context_optimization` | — |
| 23 | 按任务 agents 调配 | `agent_dispatching` | — |
| 24 | 防幻觉 | `anti_hallucination` | — |
| 25 | 沙箱 | `sandbox` | — |

## 一览表

| 编号 | 能力 ID | 名称 | 分类 | 负责 agents |
| --- | --- | --- | --- | --- |
| C01 | `automation` | 全自动化执行 | 执行 | `super-agent`、`planner-agent` |
| C02 | `lazy_loading` | 懒加载 | 性能 | `super-agent` |
| C03 | `full_visualization` | 全可视化 | 可观测 | `super-agent` |
| C04 | `power_allocation` | 权力分配 | 治理 | `super-agent`、`guard-agent` |
| C05 | `security` | 安全约束 | 治理 | `super-agent`、`guard-agent` |
| C06 | `error_correction` | 错误纠错 | 执行 | `super-agent`、`code-agent` |
| C07 | `self_purification` | 自净化 | 进化 | `super-agent` |
| C08 | `correctness_check` | 正确性校验 | 质量 | `super-agent`、`guard-agent`、`code-agent` |
| C09 | `compliance_check` | 合规检查 | 治理 | `guard-agent` |
| C10 | `shared_memory` | 对话共享记忆 | 记忆 | `super-agent`、`all-sub-agents` |
| C11 | `full_chain_monitoring` | 全链路监控 | 可观测 | `super-agent` |
| C12 | `model_scheduling` | 按任务模型调度 | 执行 | `super-agent`、`planner-agent` |
| C13 | `tool_invocation` | 工具调用 | 执行 | `code-agent`、`research-agent`、`doc-agent` |
| C14 | `orchestration` | 流程编排 | 执行 | `super-agent`、`planner-agent` |
| C15 | `loop_error_handling` | 循环报错处理 | 执行 | `super-agent` |
| C16 | `data_flywheel` | 数据飞轮 | 进化 | `super-agent`、`research-agent` |
| C17 | `evaluation_driven` | 评估驱动检查 | 质量 | `super-agent`、`guard-agent` |
| C18 | `rag_tool_augmentation` | 工具增强（RAG） | 质量 | `research-agent`、`code-agent`、`doc-agent` |
| C19 | `observability_alerting` | 可观测与告警 | 可观测 | `super-agent`、`code-agent` |
| C20 | `security_governance` | 安全与治理 | 治理 | `guard-agent`、`super-agent` |
| C21 | `org_composition` | 组织与组合管理 | 治理 | `super-agent` |
| C22 | `cost_compute_optimization` | 成本与算力优化 | 性能 | `super-agent` |
| C23 | `context_optimization` | 上下文优化 | 性能 | `super-agent` |
| C24 | `agent_dispatching` | 按任务 agents 调配 | 执行 | `super-agent` |
| C25 | `anti_hallucination` | 防幻觉 | 质量 | `guard-agent`、`research-agent` |
| C26 | `sandbox` | 沙箱 | 安全 | `code-agent`、`research-agent` |

## 分类：执行

### C01 全自动化执行 `automation`

- **触发条件**：用户提交目标（POST /api/v1/tasks）后立即触发，无需二次编排确认。
- **负责 agents**：`super-agent`、`planner-agent`
- **权限边界**：自动执行仅限 Read/Write/CrossAgent；涉及 SandboxExec 的步骤逐条过 RBAC，涉及 system 级写入一律不自动执行。
- **可观测信号**：`sa_tasks_created_total`、`sa_task_steps_total`、`task.trace[].phase`
- **降级/终止**：连续 2 个步骤失败即熔断转 failed；预算耗尽转 aborted_budget；用户取消转 cancelled。

### C06 错误纠错 `error_correction`

- **触发条件**：单步执行返回可重试错误（超时、瞬时失败、工具返回 5xx）。
- **负责 agents**：`super-agent`、`code-agent`
- **权限边界**：重试不得改变原步骤的权限级别与沙箱画像，禁止以重试绕过授权。
- **可观测信号**：`sa_task_retries_total`、`sa_task_step_failures_total`
- **降级/终止**：同一步骤最多重试 2 次（指数退避 200ms/400ms）；同类错误第 2 次仍失败即熔断，不无限重试。

### C12 按任务模型调度 `model_scheduling`

- **触发条件**：步骤生成时按任务类型与难度选择 fast/standard/deep 档位。
- **负责 agents**：`super-agent`、`planner-agent`
- **权限边界**：用户可指定 requested_model，但不得超过其配额；系统降档无需用户确认，升档必须显式请求。
- **可观测信号**：`sa_model_profile_used_total`、`task.tokens_used`
- **降级/终止**：预算不足时按 deep→standard→fast 逐级降档；降到 fast 仍超预算则终止该步骤并标记 aborted_budget。

### C13 工具调用 `tool_invocation`

- **触发条件**：步骤声明需要工具（检索、读写、执行）时。
- **负责 agents**：`code-agent`、`research-agent`、`doc-agent`
- **权限边界**：工具白名单 + 参数 schema 校验；跨 agent 调用需 CrossAgent 权限；禁止调用未注册工具。
- **可观测信号**：`sa_tool_calls_total`、`sa_tool_failures_total`
- **降级/终止**：同一工具同一参数失败 2 次即停用该工具并改用备用路径；无备用则标记步骤失败。

### C14 流程编排 `orchestration`

- **触发条件**：任务提交后由 super-agent 生成 DAG/线性计划。
- **负责 agents**：`super-agent`、`planner-agent`
- **权限边界**：编排只能调用注册表中存在的 agent 与其声明权限的交集，不得提升任何 agent 权限。
- **可观测信号**：`sa_task_plan_steps`、`task.plan[].agent_id`
- **降级/终止**：无可承接 agent → 直接返回 out_of_scope 而非硬编；步骤数超过 12 时拆分为子任务。

### C15 循环报错处理 `loop_error_handling`

- **触发条件**：同一步骤重复失败、或 agent 间互相推诿形成调用环。
- **负责 agents**：`super-agent`
- **权限边界**：熔断器只能停止执行，不能修改历史轨迹；恢复需重新提交任务。
- **可观测信号**：`sa_task_circuit_open_total`、`sa_task_retries_total`
- **降级/终止**：单步重试上限 2 次、单任务累计失败步骤上限 3；触发即熔断，任务转 failed 并输出根因摘要。

### C24 按任务 agents 调配 `agent_dispatching`

- **触发条件**：计划步骤需要指派执行者时。
- **负责 agents**：`super-agent`
- **权限边界**：只能指派注册表中声明了对应 task_type 的子 agent；不得临时创造 agent。
- **可观测信号**：`sa_agent_dispatches_total`、`task.plan[].agent_id`
- **降级/终止**：最高匹配分低于阈值 0.45 时不上报硬编结果，而是返回 out_of_scope 并说明缺什么能力。


## 分类：性能

### C02 懒加载 `lazy_loading`

- **触发条件**：仅在任务实际命中某 agent / skill / 知识文档时才装配其上下文。
- **负责 agents**：`super-agent`
- **权限边界**：只能加载调用方有权访问的文档与记忆（private 仅本人，system 需 owner/admin）。
- **可观测信号**：`sa_context_tokens_saved_total`、`sa_agents_loaded_total`
- **降级/终止**：上下文超阈值时按优先级截断：共享记忆摘要 > 垂直知识摘录 > 文档全文；截断仍超限则降档模型。

### C22 成本与算力优化 `cost_compute_optimization`

- **触发条件**：任务创建时分配 token 预算；步骤执行时实时扣减。
- **负责 agents**：`super-agent`
- **权限边界**：预算只能收紧不能透支；降档不改变权限；提额需用户显式指定。
- **可观测信号**：`sa_tokens_used_total`、`sa_budget_downgrades_total`
- **降级/终止**：预算消耗 80% 时告警并停止升档；100% 时终止剩余步骤并标记 aborted_budget。

### C23 上下文优化 `context_optimization`

- **触发条件**：装配上下文前，估算 token 需求。
- **负责 agents**：`super-agent`
- **权限边界**：压缩只能摘要自有与已授权内容，不得跨用户拼接上下文。
- **可观测信号**：`sa_context_tokens_used`、`sa_context_truncations_total`
- **降级/终止**：超限按“共享记忆摘要 → 垂直知识摘录 → 文档全文”顺序裁剪；仍超限则降档模型或拆分子任务。


## 分类：可观测

### C03 全可视化 `full_visualization`

- **触发条件**：任何任务执行、调度决策、权限判定均产生可查询的结构化记录。
- **负责 agents**：`super-agent`
- **权限边界**：用户只能查看自己的任务轨迹与审计；全局视图需 Admin 权限。
- **可观测信号**：`GET /api/v1/tasks/{id}/trace`、`GET /api/v1/audit`、`GET /metrics`
- **降级/终止**：审计队列有界（默认 5000 条），溢出时丢弃最旧事件并在 sa_audit_dropped_total 计数。

### C11 全链路监控 `full_chain_monitoring`

- **触发条件**：每个 HTTP 请求、每个任务步骤、每次外部调用。
- **负责 agents**：`super-agent`
- **权限边界**：监控数据只读；request_id 贯穿全链路，日志不得包含密码与 token。
- **可观测信号**：`sa_http_requests_total`、`sa_task_duration_ms`、`sa_uptime_seconds`
- **降级/终止**：指标写入失败不影响业务（best-effort）；连续 30s 无心跳则 /health/ready 返回 503。

### C19 可观测与告警 `observability_alerting`

- **触发条件**：错误率、延迟、限流、越权、预算超支等阈值命中。
- **负责 agents**：`super-agent`、`code-agent`
- **权限边界**：告警只做通知与标记；自动处置限于停止重试与降档，不做数据删除。
- **可观测信号**：`sa_alerts_total`、`sa_http_5xx_total`、`sa_task_failures_total`
- **降级/终止**：同一条告警 5 分钟内去重；告警风暴时只保留严重级别，其余计数聚合。


## 分类：治理

### C04 权力分配 `power_allocation`

- **触发条件**：每次跨 agent 调用、共享记忆写入、系统级操作前判定。
- **负责 agents**：`super-agent`、`guard-agent`
- **权限边界**：owner > admin > member；system 作用域记忆与用户管理仅 owner/admin；guard-agent 只读且有一票否决。
- **可观测信号**：`sa_authz_denied_total`、`audit.decision=deny`
- **降级/终止**：越权即拒绝（403）并记录审计，不做静默降级；同一请求连续 3 次越权则终止该任务。

### C05 安全约束 `security`

- **触发条件**：所有请求入口、密码处理、token 校验、敏感写操作。
- **负责 agents**：`super-agent`、`guard-agent`
- **权限边界**：不落明文密码与 token；密钥只来自环境变量或受保护密钥文件；禁止跨用户资源读取。
- **可观测信号**：`sa_login_failures_total`、`sa_rate_limited_total`、`sa_authz_denied_total`
- **降级/终止**：登录连续失败 5 次锁定 15 分钟；限流命中返回 429 + Retry-After；熵源不可用直接拒绝启动。

### C09 合规检查 `compliance_check`

- **触发条件**：每一步产物落库前、记忆写入前。
- **负责 agents**：`guard-agent`
- **权限边界**：guard-agent 只读；命中红线清单时可否决，但不得自行改写内容或放宽规则。
- **可观测信号**：`sa_compliance_blocks_total`、`audit.action=compliance_check`
- **降级/终止**：命中红线（凭据、越权数据、违法内容）→ 直接终止该步骤并整任务转 failed，不降级放行。

### C20 安全与治理 `security_governance`

- **触发条件**：权限变更、记忆作用域变更、管理接口调用。
- **负责 agents**：`guard-agent`、`super-agent`
- **权限边界**：角色变更仅 owner；审计只追加不修改；禁用用户即时生效（token 校验时检查状态）。
- **可观测信号**：`sa_admin_actions_total`、`audit.decision`
- **降级/终止**：治理组件异常时采用保守策略：拒绝新的高权限写入，已有会话保持只读。

### C21 组织与组合管理 `org_composition`

- **触发条件**：agent 注册、组合（super + sub 编组）、并发与配额分配时。
- **负责 agents**：`super-agent`
- **权限边界**：组合不得超出被编组 agent 的权限交集；新增 agent 必须声明 sandbox 画像与权限集。
- **可观测信号**：`sa_agents_registered`、`sa_agent_concurrency`
- **降级/终止**：注册表校验失败（id 重复、权限越界、无沙箱画像）拒绝加载；运行期超并发则排队而非丢弃。


## 分类：进化

### C07 自净化 `self_purification`

- **触发条件**：任务结束、记忆写入冲突、审计队列膨胀、过期会话堆积时。
- **负责 agents**：`super-agent`
- **权限边界**：清理与去重只作用于自有数据与过期数据；不得删除他人记忆或审计留痕（仅裁剪最旧）。
- **可观测信号**：`sa_memory_dedup_total`、`sa_sessions_revoked_total`、`sa_audit_dropped_total`
- **降级/终止**：同 key 记忆写入即版本递增覆盖（不产生重复条目）；清理失败不阻塞主流程，仅告警。

### C16 数据飞轮 `data_flywheel`

- **触发条件**：任务结束后沉淀经验（成功的做法与踩坑），供后续调度打分使用。
- **负责 agents**：`super-agent`、`research-agent`
- **权限边界**：只沉淀任务摘要与结论，不沉淀用户隐私原文；system 级沉淀需 owner/admin。
- **可观测信号**：`sa_flywheel_events_total`、`memory.tags`
- **降级/终止**：沉淀失败不影响任务结果；同一 task_type 沉淀条目超过 200 条时按访问热度淘汰。


## 分类：质量

### C08 正确性校验 `correctness_check`

- **触发条件**：每步产物生成后、最终结果汇总前。
- **负责 agents**：`super-agent`、`guard-agent`、`code-agent`
- **权限边界**：校验器只读产物与轨迹，不得修改产物内容；发现不一致只能标记并要求重做。
- **可观测信号**：`sa_correctness_checks_total`、`task.agreement_score`
- **降级/终止**：一致性得分 < 0.6 标记 partially_succeeded 并附失败原因；关键校验失败直接 failed。

### C17 评估驱动检查 `evaluation_driven`

- **触发条件**：每轮任务结束、以及每 100 次任务执行的漂移检查。
- **负责 agents**：`super-agent`、`guard-agent`
- **权限边界**：评估只读指标与轨迹；不得为了指标好看而跳过高风险校验。
- **可观测信号**：`sa_eval_success_rate`、`sa_eval_avg_agreement`、`sa_eval_drift_alerts_total`
- **降级/终止**：成功率环比下降超过 15% 触发告警并收紧重试与预算；连续两轮下降则自动降档默认模型。

### C18 工具增强（RAG） `rag_tool_augmentation`

- **触发条件**：步骤需要专业文档/垂直知识支撑时，先检索再生成。
- **负责 agents**：`research-agent`、`code-agent`、`doc-agent`
- **权限边界**：只检索调用方有权访问的文档；引用必须带来源标识，无来源内容不得作为事实输出。
- **可观测信号**：`sa_rag_retrievals_total`、`sa_rag_misses_total`
- **降级/终止**：检索为空时输出降级为通用建议并显式声明“未命中知识库”，禁止编造来源。

### C25 防幻觉 `anti_hallucination`

- **触发条件**：任何产出事实性陈述、数值、引用、代码符号时。
- **负责 agents**：`guard-agent`、`research-agent`
- **权限边界**：无来源或检索未命中时必须声明不确定性；禁止虚构文件路径、接口、版本号。
- **可观测信号**：`sa_hallucination_flags_total`、`task.agreement_score`
- **降级/终止**：关键主张无来源支撑 → 降级为“未确认”并降低 agreement_score；两次复核仍无依据则拒绝输出该主张。


## 分类：记忆

### C10 对话共享记忆 `shared_memory`

- **触发条件**：任务创建时读取相关记忆；任务结束后写入经验记忆。
- **负责 agents**：`super-agent`、`all-sub-agents`
- **权限边界**：private 仅本人可读写；shared 本人与协作 agent 可读、member 可写；system 仅 owner/admin 可写。
- **可观测信号**：`sa_memory_writes_total`、`sa_memory_reads_total`、`memory.version`
- **降级/终止**：记忆服务异常时任务继续执行但标记 degraded，并在结果中声明未使用历史上下文。


## 分类：安全

### C26 沙箱 `sandbox`

- **触发条件**：代码执行、浏览器访问、文件写入、外部工具调用前。
- **负责 agents**：`code-agent`、`research-agent`
- **权限边界**：沙箱内仅允许声明的路径与网络白名单；orchestrator 默认无文件写权限；只读 agent 运行在 readonly 沙箱。
- **可观测信号**：`sa_sandbox_runs_total`、`sa_sandbox_violations_total`
- **降级/终止**：沙箱拒绝启动（策略缺失/越界路径）时步骤直接失败，不降级到宿主执行。

## 覆盖度自查

| 检查项 | 结果 |
| --- | --- |
| 能力总数 | 26 |
| 分类数 | 8 |
| 五要素齐全 | 是 |
| 无 owner 的能力 | 0 |
