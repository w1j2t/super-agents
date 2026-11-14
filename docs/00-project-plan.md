# 项目计划 — Personal Super Agents（后端，Rust）

## 1. 交付边界

| 项 | 结论 |
|---|---|
| 交付形态 | 纯后端 HTTP 服务（Rust + axum），无任何前端代码、无静态页面、无模板渲染 |
| 前端约定 | 前端由老板后续提供，后端只暴露版本化 REST/JSON API（`/api/v1/*`）与 CORS |
| 必做功能 | 注册、登录、新手教程 |
| 系统主体 | 个人超级 agents 系统：统一超级 agents 层 + 多个子 agents，含垂直知识、专业文档、专业 skill、角色编排、共享记忆；统一接入 API / MCP / skill 市场 / 浏览器沙箱（以注册表 + 能力声明 + 沙箱执行器形式落地，不外连第三方） |
| 文档组织 | 严格按六部分：总体定位、能力清单、分工协作、交互规范、边界处理、验收标准 |
| 运行要求 | `cargo build --release` 一次通过，`cargo test` 通过，启动后可完成注册→登录→教程→任务全链路 |
| 不含 | 任何前端、LLM 供应商联网调用、真实 MCP 外连、真实浏览器沙箱内核、支付/邮件/短信通道 |

## 2. 目录结构

```
新建文件夹(7)/
├── Cargo.toml
├── .cargo/config.toml          # 国内镜像（可删）
├── README.md                   # 运行与能力边界（精简）
├── docs/
│   ├── 00-project-plan.md      # 本文件
│   ├── 01-overall-positioning.md
│   ├── 02-capability-catalog.md
│   ├── 03-division-of-labor.md
│   ├── 04-interaction-spec.md
│   ├── 05-boundary-handling.md
│   ├── 06-acceptance-criteria.md
│   └── 07-api-reference.md
├── scripts/smoke.ps1           # 端到端冒烟脚本
├── src/
│   ├── main.rs                 # 装配：配置 → 存储 → 路由 → 优雅关闭
│   ├── config.rs               # 环境变量配置 + 校验
│   ├── error.rs                # 统一错误 → RFC7807 风格响应
│   ├── state.rs                # AppState（store / metrics / rate limiter / clock）
│   ├── domain/
│   │   ├── user.rs             # 用户、角色、状态
│   │   ├── auth.rs             # 会话、token 对
│   │   ├── onboarding.rs       # 教程步骤定义 + 进度状态机
│   │   ├── agent.rs            # 超级 agent / 子 agent 注册表
│   │   ├── capability.rs       # 24 项能力矩阵（五要素）
│   │   ├── task.rs             # 任务、调度结果、执行轨迹
│   │   └── memory.rs           # 共享记忆条目（作用域/可见性/摘要）
│   ├── security/
│   │   ├── password.rs         # PBKDF2-HMAC-SHA256（随机盐、恒定时间比较）
│   │   ├── jwt.rs              # HS256 签发/校验（自实现，零 C 依赖）
│   │   ├── ratelimit.rs        # 令牌桶（登录/注册/任务提交）
│   │   └── rbac.rs             # 角色权限判定 + 越权审计
│   ├── store/
│   │   ├── mod.rs              # Store trait + 内存实现 + JSON 快照持久化
│   │   └── snapshot.rs         # 原子落盘（tmp + rename）、文件锁、启动恢复
│   ├── obs/
│   │   ├── metrics.rs          # 计数器/直方图，Prometheus 文本输出
│   │   └── audit.rs            # 审计事件流
│   └── api/
│       ├── mod.rs              # 路由注册 + 中间件栈
│       ├── middleware.rs       # request-id、访问日志、鉴权提取器
│       ├── auth_handlers.rs
│       ├── onboarding_handlers.rs
│       ├── agent_handlers.rs
│       ├── capability_handlers.rs
│       ├── task_handlers.rs
│       ├── memory_handlers.rs
│       └── system_handlers.rs  # health / ready / metrics
└── tests/
    ├── auth_flow.rs            # 注册/登录/刷新/登出/越权
    └── onboarding_task_flow.rs # 教程推进 + 任务调度 + 共享记忆
```

## 3. 数据模型（内存主存 + JSON 快照）

| 实体 | 关键字段 |
|---|---|
| User | id, username, email, password_hash(salt+params), role(owner/admin/member), status(active/disabled), created_at, last_login_at, tutorial_state |
| Session | id, user_id, refresh_token_hash, expires_at, revoked_at, ua/ip |
| OnboardingStep | key, order, title, body（结构化步骤定义，内置 6 步） |
| OnboardingProgress | user_id, completed: [key], skipped: bool, updated_at |
| Agent | id, kind(super|sub), name, domain, vertical_knowledge[], doc_refs[], skills[], permissions[], sandbox_profile |
| Capability | id, name, trigger, owner_agents[], permission_boundary, observability_signals[], degradation_or_abort |
| Task | id, owner, goal, task_type, plan(steps), assignments[{step, agent_id, model_profile}], status, trace[], result, cost{token, wall_ms}, created_at |
| MemoryEntry | id, scope(private|shared|system), owner, key, content, tags, visibility, version, written_by |
| AuditEvent | id, at, actor, action, target, decision(allow|deny), reason, request_id |

## 4. 调度设计（不依赖外部 LLM，可确定性复现）

1. **任务建模**：`goal` + `task_type`（coding / writing / research / planning / ideation / collaboration / auto）。
2. **子任务分解**：内置任务模板（按 task_type 生成步骤序列），`auto` 走关键词分类器。
3. **模型档位调度**：每步声明 `model_profile`（fast/standard/deep），映射到成本与并发上限；成本预算超限即降级（deep→standard→fast）或终止。
4. **agents 调配**：能力匹配打分 = 领域命中(0.5) + skill 命中(0.3) + 垂直知识命中(0.2)，取最高且 ≥ 阈值者；低于阈值 → 上报"超出能力范围"而非硬编。
5. **权力分配**：步骤级 `permission`（read / write / sandbox_exec / memory_write / cross_agent），在执行前经 RBAC 校验；越权立即 deny + 审计。
6. **循环报错处理**：同一步骤最多重试 2 次（指数退避），同类失败 2 次即熔断，任务转 `failed` 并给出可观测原因。
7. **自净化**：任务结束后写入 `task.knowledge/gotcha` 记忆（共享记忆），下轮同领域调度加分；同时记录成本与失败率，供评估驱动漂移检测。

## 5. 实施阶段（分批编译验证，每批必须 `cargo check` 通过）

| 阶段 | 内容 | 验证 |
|---|---|---|
| P0 | 骨架、config、error、state | `cargo check` |
| P1 | security（密码/JWT/RBAC/限流） | 单测 |
| P2 | store + 快照持久化 | 单测（重启恢复） |
| P3 | 注册/登录/刷新/登出/me + 鉴权中间件 | 集成测试 |
| P4 | 新手教程（状态机、跳过、重置） | 集成测试 |
| P5 | agents 注册表 + 24 项能力矩阵 | 集成测试 |
| P6 | 任务编排/调度/成本/熔断/共享记忆 | 集成测试 |
| P7 | 可观测（metrics/audit/health）、限流、优雅关闭 | 冒烟脚本 |
| P8 | 六部分规格文档 + API 参考 + README | 一致性核对 |

## 6. 安全与合规基线

- 密码：PBKDF2-HMAC-SHA256，随机 16 字节盐，迭代 120000，恒定时间比较。
- Token：HS256 JWT（access 30min / refresh 7d），refresh 只存哈希；登出即吊销。
- 限流：登录 10 次/5 分钟/IP+账号，注册 5 次/小时/IP，任务提交 60 次/小时/用户。
- 越权：跨用户读取一律 403 + 审计；`system` 作用域记忆仅 owner/admin 可写。
- 密钥：`SA_JWT_SECRET` 未设置时自动生成并写入 `data/secret.key`（0600 语义）并告警，禁止硬编码。
- 日志：不落明文密码/token；请求 ID 贯穿全链路。

## 7. 风险与对策

| 风险 | 对策 |
|---|---|
| 全局 cargo 包缓存被其他进程占用 | 本构建独立 target 目录；构建串行等待 |
| crates.io 直连慢 | 项目内 `.cargo/config.toml` 走 rsproxy 镜像 |
| axum 0.8 路径语法（`{id}`） | 全量使用新语法，编译期校验 |
| 快照损坏 | 写入 tmp+rename 原子替换，启动时坏文件自动备份为 `.corrupt` 并从空库恢复 |

## 8. 完成定义（DoD）

- `cargo build --release` 成功；`cargo test` 全绿。
- 服务启动后：注册 → 登录 → 拉教程 → 完成 2 步 → 重置 → 创建任务 → 查看 trace → 读写共享记忆 → `/metrics` 有数据，全链路无 5xx。
- 六部分文档齐备，24 项能力逐项具备五要素。
