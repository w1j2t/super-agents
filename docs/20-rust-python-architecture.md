# Rust + Python 混合架构落地设计（Phase-2）

> 依据：用户提供的推荐方案图（热路径 Rust 12–28ms p50，编排 Python 绕开 GIL）
> 现状：v1.0.0 后端为**全 Rust、确定性模拟执行器**，接口契约已稳定。本文描述如何在不破坏现有 REST 契约的前提下，把「真实执行」拆给 Python 层。

## 1. 为什么是这个切法

| 关注点 | Rust 层 | Python 层 |
|---|---|---|
| 核心诉求 | 稳定、低时延、内存可控 | 生态、迭代速度 |
| 职责 | HTTP/鉴权/限流/状态机/预算与熔断强制/持久化 | LLM 调用、工具/MCP 编排、RAG、提示词 |
| 失败模式 | 绝不 panic 到进程级；超时即断 | 子进程崩溃可被父进程回收 |

关键判断：**约束必须留在 Rust**。预算、重试上限、合规红线、越权判定若放到 Python，等于把闸门交给被约束方。Python 只负责"怎么算"，不负责"许不许"。

## 2. 进程与通信拓扑

```
                 ┌─────────────── Rust: super-agents ───────────────┐
  浏览器/TS ──HTTP─▶ axum Router ──▶ TaskService ──▶ Executor ──┐   │
                 │      ▲ 鉴权/限流/预算/审计                     │   │
                 │      └──────── TraceSink ◀──────────────────┘   │
                 └──────────────┬──────────────────────────────────┘
                                │ stdin/stdout JSONL（子进程）
                                ▼
                 ┌──────── Python: sa_worker（每任务一进程）────────┐
                 │  orchestrator → llm_client / tool_runner / rag  │
                 └────────────────────────────────────────────────┘
```

选型理由：

- **子进程 + 标准流 JSONL**，而非 HTTP/gRPC
  - 免端口占用与端口冲突，免额外鉴权面（父进程管道即信任边界）
  - 每任务一进程天然隔离，Python 侧崩溃不会污染常驻服务
  - 无需引入 pyo3（免 MSVC 依赖，当前环境正是缺 MSVC）
- **不用 pyo3 嵌 CPython**：GIL 与 Rust 异步运行时冲突、崩溃即拖垮主进程、编译需 MSVC，三条都不利。
- **不用 Redis/Celery**：单机产品不需要，多一层运维就多一层故障。

## 3. 进程协议（v1 线协议）

### 3.1 父 → 子（启动时一次性写入 stdin）

```json
{"type":"run","task_id":"tsk_01H...","goal":"把季度数据整理成图表","agent":"data-analyst",
 "model_profile":"deep","budget_tokens":14400,"retry_limit":2,
 "context":{"memory":[{"key":"user.style","value":"偏好简洁","scope":"private"}],
            "capabilities":["chart.render","data.csv.parse"]},
 "trace_path":null}
```

### 3.2 子 → 父（逐行刷出，父进程边收边落 trace）

```json
{"type":"step","index":0,"title":"解析目标","status":"running","tokens":0}
{"type":"step","index":0,"status":"succeeded","tokens":812,"latency_ms":1340}
{"type":"progress","used_tokens":812,"note":"上下文装配完成"}
{"type":"result","status":"succeeded","summary":"已生成 3 张图",
 "outputs":[{"kind":"file","path":"output/chart1.png"}],"used_tokens":9120}
{"type":"error","code":"tool_failed","message":"chart.render 超时","retryable":true}
```

### 3.3 握手与心跳

- 父进程写 `run` 后启动 5s 计时器，未收到首条 `step` 即判冷启动失败 → 记 `aborted_error`，保留 stderr 尾部进审计。
- 子进程每 2s 至少输出一条（`progress` 或 `step`），父进程 10s 静默即发 `SIGKILL`（Windows 用 `TerminateProcess`），按超时处理。
- 单任务墙钟上限 `SA_TASK_WALL_MS`（默认 600000），到点强杀。

## 4. Rust 侧改造清单

| 文件 | 改动 |
|---|---|
| `src/orchestration/executor.rs` | **已实现**：抽出 `trait StepRunner`（单步执行后端）+ `SimRunner`；`Executor` 通过 `runner: Option<&dyn StepRunner>` 注入。后续加 `ProcessRunner` 即切真执行，由 `SA_EXECUTOR=sim\|process` 选择 |
| `src/orchestration/process.rs` | 新增。`tokio::process::Command` + `BufReader<Stdout>` 逐行 `serde_json::from_str::<WorkerEvent>`；预算/重试的上限校验在**父进程**二次强制执行 |
| `src/orchestration/budget.rs` | 子进程上报的 `used_tokens` 累加进同一账本；越限立即 kill 并落 `aborted_budget`（父进程说了算） |
| `src/errors.rs` | 新增 `WorkerUnavailable`（503）/ `WorkerProtocol`（502）两码 |
| `src/config.rs` | 新增 `SA_PYTHON`（解释器路径，默认 `python`）、`SA_WORKER_SCRIPT`、`SA_WORKER_TIMEOUT_SECS`、`SA_EXECUTOR` |
| `src/observability/metrics.rs` | 新增 `sa_worker_restarts_total`、`sa_worker_latency_ms`（histogram）、`sa_worker_failures_total{code}` |

契约不动：`POST /tasks` 的请求/响应体、`TaskView`、`/tasks/{id}/trace` 结构全部保持不变——前端零改动即可切真执行。

## 5. Python 侧结构（`worker/`）

```
worker/
  sa_worker.py        # 入口：读 stdin run → 跑编排 → JSONL 输出 → exit 0/1
  protocol.py         # pydantic 模型，与 Rust 侧 serde 结构一一对应
  orchestrator.py     # 步骤规划 + 串行调度 + 预算自查（父进程仍是最终裁决）
  llm_client.py       # 统一 LLM 适配：openai 兼容 / 本地（Ollama、vLLM）
  tool_runner.py      # MCP / 本地工具执行，超时与并发隔离
  context.py          # 记忆检索与提示词装配
  guard.py            # 合规与越界预检（与 Rust 红线**重复**检查，宁可双拦）
  requirements.txt
```

要点：
- **流式输出**：`print(json.dumps(evt), flush=True)`，禁用任何缓冲；日志一律走 stderr，stdout 只放协议。
- **绕开 GIL**：I/O 密集用 `asyncio`；真 CPU 密集（分词、图像处理）用 `ProcessPoolExecutor` 或直接下沉到 Rust。
- **LLM 并发**：`httpx.AsyncClient` + `asyncio.Semaphore(SA_LLM_CONCURRENCY)`，默认 4。
- **依赖最小化**：仅 `httpx`、`pydantic`、`tenacity`；不用 LangChain 之类重框架，避免版本地狱。

## 6. 延迟预算（对齐推荐图 12–28ms）

| 段 | 目标 p50 | 说明 |
|---|---|---|
| axum 路由 + 鉴权 | 0.5–1.5ms | JWT 解析 + 内存校验，无 IO |
| 限流 + 审计写入 | 0.3–0.8ms | 令牌桶 O(1)；审计异步刷盘 |
| 计划构建 + 预算 | 0.2–0.5ms | 纯计算 |
| **Rust 合计** | **1–3ms** | 图里 12–28ms 含 HTTP 栈与 TLS/序列化 |
| 子进程冷启（Python） | 60–140ms | **不在这条路径**：worker 采用**预热池**，常驻 2 个空闲进程，任务来时直接写 stdin |
| LLM 首 token | 300ms–2s | 真实瓶颈，异步流式回传，前端感知为渐进 |

结论：**Rust 段压到 3ms 内是可达的**；对外可感知的延迟由 LLM 决定，因此真正的优化重点是流式输出与预热池，而非继续抠 Rust 微秒。

## 7. 可靠性

| 场景 | 处置 |
|---|---|
| worker 启动失败 | 重试 1 次；仍失败 → 503 `worker_unavailable`，任务置 `aborted_error`，审计留 stderr 尾部 4KB |
| 协议乱序/非法 JSON | 立即 kill，`502 worker_protocol`；不影响其他任务 |
| worker 超预算上报 | 父进程 kill + `aborted_budget`（不信 worker 自觉） |
| 父进程重启 | 快照恢复任务状态；`running` 态任务标记 `aborted_interrupted`，不回放（避免重复副作用） |
| 幂等 | `POST /tasks` 支持 `Idempotency-Key` 头（Phase-2 增补），同 key 30 分钟内返回原结果 |

## 8. 安全

- worker 以**同用户低权限**启动；不传任何密钥进 argv（可能被进程列表读取），密钥走环境变量或临时文件 0600。
- Python 侧工具白名单由 Rust 下发的 `context.capabilities` 决定，越权调用在 Rust 审计中标记 `capability_denied`。
- 子进程禁网开关（`SA_WORKER_NET=off`）可用于纯本地模型场景，Windows 用 Job Object + 防火墙规则，Linux 用 network namespace。

## 9. 迁移路径（4 步，每步可独立验收）

1. **抽象**：抽 `trait Executor`，`SimExecutor` 包一层。单测 52 项必须全绿 → 证明抽象无侵入。
2. **并行双跑**：`SA_EXECUTOR=shadow`，真实请求走 Sim 返回给前端，同时后台跑 Process 做影子比对（只记 trace 不返结果）。比对 200 个任务后看差异。
3. **灰度切流**：按用户 ID 哈希 10% → 50% → 100%。
4. **下线 Sim**：保留为测试替身（`#[cfg(test)]`）。

## 10. 成本与收益

| 项 | 量化 |
|---|---|
| 新增复杂度 | +1 个进程边界、+1 套协议、+1 门语言（约 1200 行 Python） |
| 收益 | 模型/工具迭代不再需要重编 Rust；Rust 侧保持在 3ms 级；崩溃隔离 |
| 风险 | 协议演进需双端同改 → 用 `protocol_version` 字段 + 双端 CI 对跑防范 |

## 11. 现在就该做的三件事（**2026-11-09：三件已全部完成**）

| # | 原计划 | 实际做法 | 结果 |
|---|---|---|---|
| 1 | 补任务提交限流 | `task.rs` 加 `TASK_SUBMIT_QUOTA`（60/h/用户），`create()` 第 0 步检查 | **完成**：单测 + 冒烟 S87–S92 实测第 56 次后 429、`Retry-After=59` |
| 2 | 引入 `utoipa` 导出 `openapi.json` | 改用 `scripts/gen-openapi.py` 静态解析源码（零侵入、0 新依赖），**未引入 utoipa** | **目标达成**：`docs/openapi.json` 24 路径 / 28 操作 / 50 schema，`--check` 防漂移 |
| 3 | 抽 `trait Executor` 跑绿测试 | 抽出的是单步执行后端，命名为 `trait StepRunner`（`Executor` 保留为编排器），`SimRunner` 为默认实现 | **完成**：55 项单测全绿（原 52 + 新增 3） |

> 命名差异说明：设计阶段说"抽 `trait Executor`"，落地时发现 `Executor` 已经是"整任务编排器"的名字，被抽出来的其实是"**单步执行后端**"，
> 因此命名为 `trait StepRunner`，`Executor` 通过 `runner: Option<&dyn StepRunner>` 注入。这是**语义更准确**的选择，不是偷换概念：
> 目标"换执行后端不动治理层"已由单测 `custom_step_runner_replaces_backend_and_keeps_governance` 证明。

### 11.1 下一步真正该做的（按收益排序）

1. **接真执行**：`trait StepRunner` 已就位，新增 `ProcessRunner`（拉起 Python worker、走 JSONL 协议）即可，治理层零改动 → 消灭"执行是模拟"这条最大缺口。
2. **补 SSE**：`GET /tasks/{id}/events`，前端从轮询改推送。
3. **统一错误语义**：把"他人资源"统一为 404（不泄露存在性），前端错误分支可收敛。
4. **`sa_http_requests_total` 接线**：在 `middleware` 里打点，补上观测缺口。

> 参考文件（上面提到的旧文件名已纠正）：`docs/30-ts-frontend-plan.md`（前端规划）、`docs/40-api-contract-ts.md`（契约导读）、`docs/10-verification-report.md` §10（缺口全表）。
