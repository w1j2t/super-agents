# 03 分工协作（14 个 agent）

> 本文件的事实部分由 `scripts/gen-spec-docs.py` 从源码生成，**请勿手改**。
> 改完 `src/domain/*.rs` 后执行 `python scripts/gen-spec-docs.py`；
> CI 或提交前用 `python scripts/gen-spec-docs.py --check` 校验是否漂移。

边界原则：**唯一入口是 super-agent**，子 agent 只做自己域内的事；
跨域需求由 super-agent 拆步后顺序/并行派发，禁止子 agent 越权直连第三方。

## 0. 原文五项要求 → 本文落点

首个需求对「分工协作」点名了五件事。逐条给出落点，避免只写了花名册却漏了调度与分权。

| 原文要求 | 本文落点 | 实现/证据 |
| --- | --- | --- |
| 定义职责 | §2 各自职责 | 每个 agent 的 `summary` / `task_types` / `skills` 均来自 `src/domain/agent.rs` |
| 定义编排 | §4 典型协作链路 | `src/orchestration/executor.rs`：每步过 RBAC → 红线 → 预算 → 执行 → 重试/熔断 |
| 按任务模型调度 | §1「默认模型」列 + §3「计划置信度」 | `ModelProfile` = fast / standard / deep；agent 自带 `default_model`；预算不足按 deep→standard→fast 降档（`docs/05` §4） |
| 按任务 agents 调配 | §3「工人打分 / 派发门槛 / 并发上限」 | `routing.rs`：`MIN_DISPATCH_SCORE = 0.45`（该类型下无 agent 可承接即判越界，不静默降级）；`max_concurrency` 超出排队而非失败 |
| 权力分配 | §5 权限档位 + §1「权限档」列 | 七项原子权限（`security/rbac.rs`）三档分组，权限不可临时提权（`docs/05` §2） |

## 1. 花名册

| ID | 名称 | 类型 | 领域 | 默认模型 | 并发 | 权限档 | 沙箱画像 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `super-agent` | 统一超级 Agent | Super | 总控与编排 | Deep | 8 | Read/Write/SandboxExec/MemoryWriteShared/MemoryWriteSystem/CrossAgent/Admin | `orchestrator-no-fs-write` |
| `code-agent` | Code Agent | Sub | 编程开发与运行态 | Deep | 4 | Read/Write/SandboxExec/MemoryWriteShared/CrossAgent | `sandbox-repo-rw` |
| `doc-agent` | Doc Agent | Sub | 文档写作 | Standard | 6 | Read/Write/SandboxExec/MemoryWriteShared/CrossAgent | `sandbox-doc-rw` |
| `research-agent` | Research Agent | Sub | 信息检索与研究 | Standard | 6 | Read/Write/SandboxExec/MemoryWriteShared/CrossAgent | `sandbox-net-readonly` |
| `planner-agent` | Planner Agent | Sub | 任务规划 | Standard | 4 | Read/Write/SandboxExec/MemoryWriteShared/CrossAgent | `sandbox-no-exec` |
| `ideation-agent` | Ideation Agent | Sub | 创意构思 | Standard | 4 | Read/Write/SandboxExec/MemoryWriteShared/CrossAgent | `sandbox-no-exec` |
| `game-agent` | Game Agent | Sub | 游戏设计 | Standard | 3 | Read/Write/SandboxExec/MemoryWriteShared/CrossAgent | `sandbox-no-exec` |
| `finance-agent` | Finance Agent | Sub | 财务核算 | Standard | 3 | Read/Write/SandboxExec/MemoryWriteShared/CrossAgent | `sandbox-no-exec` |
| `legal-agent` | Legal Agent | Sub | 法务合同 | Standard | 3 | Read/Write/SandboxExec/MemoryWriteShared/CrossAgent | `sandbox-no-exec` |
| `data-agent` | Data Agent | Sub | 数据分析 | Standard | 4 | Read/Write/SandboxExec/MemoryWriteShared/CrossAgent | `sandbox-repo-rw` |
| `narrative-agent` | Narrative Agent | Sub | 叙事与世界观 | Standard | 3 | Read/Write/SandboxExec/MemoryWriteShared/CrossAgent | `sandbox-no-exec` |
| `market-agent` | Market Agent | Sub | 市场与竞争 | Standard | 3 | Read/Write/SandboxExec/MemoryWriteShared/CrossAgent | `sandbox-no-exec` |
| `qa-agent` | QA Agent | Sub | 质量与验收 | Standard | 3 | Read/Write/SandboxExec/MemoryWriteShared/CrossAgent | `sandbox-no-exec` |
| `guard-agent` | Guard Agent | Sub | 合规与安全审核 | Standard | 8 | Read | `sandbox-readonly` |

## 2. 各自职责

### `super-agent` 统一超级 Agent

- **职责**：唯一入口：理解意图、判定能力边界、拆分任务、调度子 agents、收敛结果并做最终交付校验。
- **可承接任务类型**：Coding、Writing、Research、Planning、Ideation、Collaboration（由 `TaskType` 驱动路由）
- **技能**：`intent-routing`、`task-decomposition`、`result-synthesis`、`escalation`
- **垂直知识**：任务分解模式、能力边界判定、多 agent 协作协议、成本与预算控制
- **参考文档**：—
- **调度关键词**：综合、整体、帮我、全流程、一站式、统筹

### `code-agent` Code Agent

- **职责**：代码生成、重构、审查、单测补齐、依赖与构建问题定位，以及本机与服务运行态排障（日志、端口、进程、磁盘、环境依赖）；只输出可编译可运行的产物与可复现的排障结论。
- **可承接任务类型**：Coding（由 `TaskType` 驱动路由）
- **技能**：—
- **垂直知识**：—
- **参考文档**：—
- **调度关键词**：—

### `doc-agent` Doc Agent

- **职责**：长文写作、结构化报告、合同与邮件、会议纪要与多方对齐、格式规范与术语一致性。
- **可承接任务类型**：Writing、Collaboration（由 `TaskType` 驱动路由）
- **技能**：—
- **垂直知识**：—
- **参考文档**：`docs/kb/writing-style.md`、`docs/kb/glossary.md`
- **调度关键词**：—

### `research-agent` Research Agent

- **职责**：多源检索、来源分级、证据链留存、事实核对；无来源不下结论。
- **可承接任务类型**：Research（由 `TaskType` 驱动路由）
- **技能**：`web-search`、`source-grading`、`evidence-log`
- **垂直知识**：检索式设计、来源可信度分级、事实核查流程
- **参考文档**：`docs/kb/source-grading.md`
- **调度关键词**：查、搜索、资料、对比、调研、最新、来源、核实

### `planner-agent` Planner Agent

- **职责**：目标拆解、里程碑与依赖排序、风险登记与回滚预案、验收口径定义。
- **可承接任务类型**：Planning（由 `TaskType` 驱动路由）
- **技能**：`wbs`、`risk-register`、`milestone-plan`
- **垂直知识**：WBS 拆解、关键路径、风险登记、验收标准编写
- **参考文档**：`docs/kb/planning-templates.md`
- **调度关键词**：规划、计划、排期、里程碑、拆解、待办、路线

### `ideation-agent` Ideation Agent

- **职责**：发散生成、约束内收敛、方案打分与取舍理由；输出可执行候选而非空想。
- **可承接任务类型**：Ideation（由 `TaskType` 驱动路由）
- **技能**：`brainstorm`、`score-matrix`、`story-craft`
- **垂直知识**：发散收敛双钻、方案评分矩阵、命名与叙事
- **参考文档**：`docs/kb/ideation-methods.md`
- **调度关键词**：创意、点子、命名、方案、脑洞、设计、构思

### `game-agent` Game Agent

- **职责**：玩法核心循环、数值曲线与通胀控制、关卡节奏与难度曲线、系统奖励结构；产出可评审的设计检查表。
- **可承接任务类型**：Ideation、Planning（由 `TaskType` 驱动路由）
- **技能**：`core-loop-canvas`、`balance-audit`、`level-pacing`、`design-review`
- **垂直知识**：—
- **参考文档**：`docs/kb/game-design-checklist.md`
- **调度关键词**：—

### `finance-agent` Finance Agent

- **职责**：费用口径（期间/主体/含税）、单据链五单一致、预算与成本归集、异常付款信号；只给可核对清单，不猜金额。
- **可承接任务类型**：Writing、Planning（由 `TaskType` 驱动路由）
- **技能**：`voucher-chain-check`、`caliber-card`、`budget-variance`、`anomaly-scan`
- **垂直知识**：—
- **参考文档**：`docs/kb/finance-checklist.md`
- **调度关键词**：—

### `legal-agent` Legal Agent

- **职责**：合同条款体检、必备条款缺失与高危句式识别、风险分级与谈判底线；不出法律意见，重大条款交律师复核。
- **可承接任务类型**：Writing、Research（由 `TaskType` 驱动路由）
- **技能**：`clause-audit`、`red-flag-scan`、`risk-grading`、`negotiation-baseline`
- **垂直知识**：—
- **参考文档**：`docs/kb/contract-checklist.md`
- **调度关键词**：—

### `data-agent` Data Agent

- **职责**：指标定义三要素、异常波动归因、报表口径统一、数据质量六查；口径未确认前不下结论。
- **可承接任务类型**：Research、Coding（由 `TaskType` 驱动路由）
- **技能**：`metric-card`、`attribution-tree`、`dq-check`、`anomaly-detect`
- **垂直知识**：—
- **参考文档**：`docs/kb/metrics-handbook.md`
- **调度关键词**：—

### `narrative-agent` Narrative Agent

- **职责**：设定自洽与代价守恒、人物动机链、伏笔埋收台账、信息交代顺序与叙事节奏；只做承重结构审查，不做文字润色（归 doc-agent），不做玩法与数值（归 game-agent）。
- **可承接任务类型**：Writing、Ideation（由 `TaskType` 驱动路由）
- **技能**：`canon-audit`、`motive-chain`、`foreshadow-ledger`、`pacing-check`
- **垂直知识**：—
- **参考文档**：`docs/kb/narrative-canon.md`
- **调度关键词**：—

### `market-agent` Market Agent

- **职责**：竞品矩阵五维、定位三问、定价阶梯与渠道评估；不做销量预测与财务测算（归 finance-agent），不做检索取证与来源分级（归 research-agent）。
- **可承接任务类型**：Research、Planning（由 `TaskType` 驱动路由）
- **技能**：`competitor-matrix`、`positioning-brief`、`pricing-ladder`、`channel-scorecard`
- **垂直知识**：—
- **参考文档**：`docs/kb/market-playbook.md`
- **调度关键词**：—

### `qa-agent` QA Agent

- **职责**：验收标准量化、用例按等价类/边界/异常路径铺开、缺陷分级门槛与回归范围裁剪；不做代码实现（归 code-agent），不做玩法设计（归 game-agent）。
- **可承接任务类型**：Coding、Collaboration（由 `TaskType` 驱动路由）
- **技能**：`accept-criteria`、`case-matrix`、`defect-triage`、`regression-scope`
- **垂直知识**：—
- **参考文档**：`docs/kb/qa-test-strategy.md`
- **调度关键词**：—

### `guard-agent` Guard Agent

- **职责**：对每一步产物做合规、越权与幻觉风险复核；拥有否决权，只读不可写。
- **可承接任务类型**：—（由 `TaskType` 驱动路由）
- **技能**：`compliance-scan`、`hallucination-check`、`permission-audit`
- **垂直知识**：数据分级、越权判定、合规红线、幻觉识别信号
- **参考文档**：`docs/05-boundary-handling.md`、`docs/kb/compliance-rules.md`
- **调度关键词**：合规、隐私、敏感、审核、风险、越权

## 3. 调度与打分（源码实况，`src/orchestration/routing.rs`）

| 环节 | 规则 |
| --- | --- |
| 任务类型识别 | 按 `TASK_KEYWORDS` 命中数排序，取第一名；置信度 `0.55 + 0.45×hits/(hits+1.6)`。**命中数为 0 时不猜**：直接 422，message 给出两条补救（显式指定 `task_type`，或把 goal 写成「产出物＋领域」） |
| 工人打分 | `score = min(1.0, 0.5 + 0.15×min(关键词命中,2) + 0.1×[目标含领域词])` |
| 派发门槛 | `MIN_DISPATCH_SCORE = 0.45`：该任务类型下若没有任何 agent 达标，判为越界并返回 422 说明缺什么能力（**不静默降级**） |
| 计划置信度 | `clamp(0.45×worker_score + 0.35×routing_conf + 0.2, 0, 0.98)` |
| 并发上限 | 每个 agent 的 `max_concurrency`，超出排队而非失败 |

## 4. 典型协作链路

```text
用户目标 → super-agent(理解/拆步/选人)
        → worker agent(code/doc/research/planner/ideation/game/finance/legal/data/narrative/market/qa)
        → 每步过 guard-agent 合规与权限校验
        → super-agent 收敛结果 + 交付校验 → 落库
```

单步执行体通过 process runner 拉起，注入的环境变量即交接协议：

| 变量 | 含义 |
| --- | --- |
| `SA_TASK_ID` / `SA_STEP_INDEX` / `SA_STEP_NAME` / `SA_ATTEMPT` | 步骤身份与重试序号 |
| `SA_AGENT_ID` / `SA_AGENT_NAME` / `SA_PERMISSION` | 执行者与权限档 |
| `SA_GOAL` / `SA_CONTEXT_SUMMARY` / `SA_TASK_TYPE` | 目标、上下文摘要、任务类型 |
| `SA_LLM_BASE` / `SA_LLM_MODEL` / `SA_LLM_API_KEY` / `SA_LLM_TIMEOUT` | 模型接入点（缺省走模拟） |
| `SA_SANDBOX` | 沙箱画像名，与花名册最后一列对应 |

## 5. 权限档位

| 档位 | 内容 | 使用方 |
| --- | --- | --- |
| `ORCH_TRUSTED` | Read/Write/SandboxExec/MemoryWriteShared/MemoryWriteSystem/CrossAgent/Admin | super-agent 等编排者 |
| `WORKER` | Read/Write/SandboxExec/MemoryWriteShared/CrossAgent | 普通子 agent |
| `READONLY` | Read | 只读类 agent（如 guard-agent） |

七项原子权限语义见 `src/security/rbac.rs`：`Read` / `Write` / `SandboxExec` / `MemoryWriteShared` / `MemoryWriteSystem` / `CrossAgent` / `Admin`。
