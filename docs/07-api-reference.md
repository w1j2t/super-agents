# 07 API 参考索引（33 个操作 / 51 个数据结构）

> 本文件的事实部分由 `scripts/gen-spec-docs.py` 从源码生成，**请勿手改**。
> 改完 `src/domain/*.rs` 后执行 `python scripts/gen-spec-docs.py`；
> CI 或提交前用 `python scripts/gen-spec-docs.py --check` 校验是否漂移。

真源是 `docs/openapi.json`（由 `scripts/gen-openapi.py` 从路由与 handler 生成），本文件只是它的可读索引。前端 TS 类型用 `scripts/gen-ts-types.py` 从同一份 openapi 生成。

- `info.version`：1.0.0
- 服务地址：`http://127.0.0.1:8088`
- 错误响应统一为 **Problem JSON**（`components.schemas.Problem` 及变体），语义见 `docs/10-verification-report.md`

## 操作清单

| 方法 | 路径 | 摘要 | 分组 | 需鉴权 |
| --- | --- | --- | --- | --- |
| GET | `/api/v1/agents` | catalog::agents | agents | 否（公开） |
| GET | `/api/v1/agents/{id}` | catalog::agent_detail | agents | 否（公开） |
| POST | `/api/v1/auth/login` | auth::login | auth | 否（公开） |
| POST | `/api/v1/auth/logout` | auth::logout | auth | 否（公开） |
| GET | `/api/v1/auth/me` | auth::me | auth | 是 |
| PUT | `/api/v1/auth/password` | auth::change_password | auth | 是 |
| POST | `/api/v1/auth/refresh` | auth::refresh | auth | 否（公开） |
| POST | `/api/v1/auth/register` | auth::register | auth | 否（公开） |
| GET | `/api/v1/capabilities` | catalog::capabilities | capabilities | 否（公开） |
| GET | `/api/v1/capabilities/{id}` | catalog::capability_detail | capabilities | 否（公开） |
| GET | `/api/v1/cluster` | cluster::overview | cluster | 是 |
| GET | `/api/v1/memories` | memory::list | memories | 是 |
| POST | `/api/v1/memories` | memory::upsert | memories | 是 |
| GET | `/api/v1/memories/{id}` | memory::detail | memories | 是 |
| PUT | `/api/v1/memories/{id}` | memory::update | memories | 是 |
| DELETE | `/api/v1/memories/{id}` | memory::remove | memories | 是 |
| GET | `/api/v1/observability/alerts` | observability::alerts | observability | 是 |
| GET | `/api/v1/observability/audit` | observability::audit | observability | 是 |
| GET | `/api/v1/observability/components` | observability::components | observability | 是 |
| GET | `/api/v1/observability/overview` | observability::overview | observability | 是 |
| GET | `/api/v1/onboarding` | onboarding::overview | onboarding | 是 |
| POST | `/api/v1/onboarding/reset` | onboarding::reset | onboarding | 是 |
| POST | `/api/v1/onboarding/skip` | onboarding::skip | onboarding | 是 |
| POST | `/api/v1/onboarding/steps/{step_id}/complete` | onboarding::complete_step | onboarding | 是 |
| GET | `/api/v1/tasks` | task::list | tasks | 是 |
| POST | `/api/v1/tasks` | task::create | tasks | 是 |
| GET | `/api/v1/tasks/{id}` | task::detail | tasks | 是 |
| POST | `/api/v1/tasks/{id}/cancel` | task::cancel | tasks | 是 |
| GET | `/api/v1/tasks/{id}/events` | task_events::stream | tasks | 是 |
| GET | `/api/v1/tasks/{id}/trace` | task::trace | tasks | 是 |
| GET | `/healthz` | observability::health | healthz | 否（公开） |
| GET | `/metrics` | observability::metrics | metrics | 是 |
| GET | `/readyz` | observability::ready | readyz | 否（公开） |

## 数据结构（components/schemas）

| # | 名称 |
| --- | --- |
| 1 | `AgentView` |
| 2 | `Alert` |
| 3 | `AlertList` |
| 4 | `AlertPolicyView` |
| 5 | `AlertStats` |
| 6 | `AuditEvent` |
| 7 | `AuditList` |
| 8 | `AuthResult` |
| 9 | `AuthTokens` |
| 10 | `AutoAction` |
| 11 | `CapabilityView` |
| 12 | `ChangePasswordInput` |
| 13 | `ClusterNode` |
| 14 | `ClusterTasks` |
| 15 | `ClusterTotals` |
| 16 | `ClusterView` |
| 17 | `ComponentInfo` |
| 18 | `ComponentList` |
| 19 | `CreateTaskInput` |
| 20 | `EvaluatedNow` |
| 21 | `LoginInput` |
| 22 | `MemoryEntry` |
| 23 | `MemoryList` |
| 24 | `MemoryScope` |
| 25 | `ModelProfile` |
| 26 | `OnboardingView` |
| 27 | `Overview` |
| 28 | `Permission` |
| 29 | `PlanStep` |
| 30 | `Problem` |
| 31 | `RefreshInput` |
| 32 | `RegisterInput` |
| 33 | `RevokedView` |
| 34 | `Role` |
| 35 | `RuleView` |
| 36 | `Severity` |
| 37 | `StepChangeView` |
| 38 | `StepResult` |
| 39 | `StepView` |
| 40 | `TaskDetail` |
| 41 | `TaskEvent` |
| 42 | `TaskList` |
| 43 | `TaskStatus` |
| 44 | `TaskType` |
| 45 | `TaskView` |
| 46 | `TraceEntry` |
| 47 | `UpdateInput` |
| 48 | `UpsertInput` |
| 49 | `UpsertResult` |
| 50 | `UserStatus` |
| 51 | `UserView` |

## 覆盖度自查

| 检查项 | 结果 |
| --- | --- |
| 路径数 | 29 |
| 操作数 | 33 |
| GET / POST / 其它 | 20 / 10 / 3 |
| 数据结构数 | 51 |
