# 88 前端交接单（写给下一个对话）

> 本文的读者是**下一个对话里的 Agent**，不是闲聊的人类。目标只有一个：
> 你打开这个仓库后，30 分钟内能把前端跑起来并成功提交第一个任务，且不会踩我们已经踩过的坑。
> 本文里的每个数字都由门禁 27（`scripts/check_docs_drift.py`）实时比对代码，写错就变红。

---

## 0. 三十秒现状

| 项 | 状态 |
|----|------|
| 后端 | **已就绪**，29 道门禁全绿，可交付 |
| 前端 | **一行代码都还没有**。`docs/30-ts-frontend-plan.md` 只定了选型和目录，没有实现 |
| 契约 | `docs/openapi.json`：**29 条路径 / 33 个操作 / 51 个 schema** |
| 类型 | `generated/types.generated.ts`：**62 个 interface + 12 个联合类型**（生成物，别手改） |
| 元数据 | **14 个 agent** / **26 项能力** / onboarding 6 步（5 项必填） |

也就是说：**你现在是前端的第一天，后端不用你操心，契约也不会随便变。**

### 0.1 上一位在你之前干了什么（2026-11-16 收尾，知道这些能省你半天）

后端最后两个冲刺已经做完，**结论对你只有三条是有用的**：

1. **契约一个字没动。** 29 条路径 / 33 个操作 / 51 个 schema / 14 个 agent / 26 项能力，
   与前一轮完全一致，你可以放心按 `openapi.json` 生成客户端，不会再变。
2. **别把「编译通过率」这类数字做进 UI 当卖点。** 这些指标的口径本月修了四次（详见 `docs/90-metric-credibility.md`），
   现在能信，但历史上每一个版本的读数看起来都很干净。要在前端展示，请直接读 `/observability/overview` 的实时值，
   **不要抄文档里的百分比**。
3. **门禁现在是 29 道 + 11 项负向演练，全部按预期。** 你改任何东西之前先跑一次
   `python scripts/regress_all.py --tier quick`（15 道，约 24 秒），提交前跑 `--tier full --with-drills`（约 7 分钟）。

两个冲刺的实体结论（要复现就照这里跑）：

| 冲刺 | 做了什么 | 结论 | 证据 |
|---|---|---|---|
| 一 · 量具加固 | 修 `camp-g` 的 GBK 解码崩溃、给判分器加「骗子测试」、新增门禁 28 守子进程解码纪律 | 29 道全绿、11 项演练全按预期；判分器现在能识破 `...` / `pass` / 只写签名等五种假交付 | `docs/90`、`docs/evidence/28-check_subproc_encoding-latest.txt` |
| 二 · 切分策略 | 拿 21 条占位符样本做 A/B：单轮重试 vs 三段切分 | **切分无效且更贵**：救回率 19.0%、成本 2.30x；**单轮重试 47.6%、成本 1.0x**。已落地 `--retry-placeholder`（默认关） | `docs/91`、`docs/evidence/split-ab-holder.md` |

一句话给老板的说法：**「拆开做」这件事被证伪了，「没写好就再写一遍」有效但不该默认开。**

---

## 1. 先读哪几个文件（按此顺序）

1. `docs/openapi.json` —— 机器契约，**唯一权威**。有疑问以它为准，不以任何文档为准。
2. `generated/types.generated.ts` —— 由契约生成，直接 import，别手改。
3. `docs/40-api-contract-ts.md` —— 端点全表、鉴权规则、错误码映射。
4. `docs/30-ts-frontend-plan.md` —— 页面清单（9 条路由）、onboarding 交互、分阶段实施 A1–A5。
5. **本文** —— 实测出来的接口真实形状、以及五个会让你白干的坑。

---

## 2. 后端怎么起

```powershell
# 二进制（现成的，不用编译）
C:\Users\94355\AppData\Local\sa-build\target-gnu\debug\super-agents.exe

# 起服务（默认 127.0.0.1:8088；换端口用 $env:SA_BIND）
$env:SA_DATA_DIR = "C:\Users\94355\Desktop\新建文件夹(7)\temp\sa-dev-data"
C:\Users\94355\AppData\Local\sa-build\target-gnu\debug\super-agents.exe
```

可选环境变量（不设也能跑，只是 LLM 相关能力会走规则骨架）：

| 变量 | 作用 | 我们的实测值 |
|------|------|--------------|
| `SA_LLM_BASE` | OpenAI 兼容地址 | `http://127.0.0.1:11434/v1` |
| `SA_LLM_MODEL` | 模型名 | `minicpm5-2b:f16` |
| `SA_LLM_TIMEOUT` | 单次调用超时（秒） | `300` |
| `SA_LLM_MAX_TOKENS` | 单次上限 | `2400` |

就绪探测：`GET /healthz`（存活）、`GET /readyz`（可服务）、`GET /metrics`（Prometheus 文本，**另需静态令牌** `SA_METRICS_TOKEN`，见 §3）。

**起服务前先跑一次只读自检**（不起服务、不写业务状态，10 项一次看完）：

```powershell
C:\Users\94355\AppData\Local\sa-build\target-gnu\debug\super-agents.exe doctor
```

它会直接告诉你：端口是否空闲、数据目录能否写、`runner=sim` 还是 `process`、模型端点配没配、集群心跳看没看到。
`doctor` 报 `runner=sim` 时任务照样成功，但执行是**确定性模拟**——前端别把它当真实执行来验收。

> 没有 LLM 也能开发前端：任务会成功、会返回结果，只是内容质量差。
> **别拿"结果不好"当前端 bug 查**，先看 `/api/v1/observability/overview` 里 `runner_backend` 是不是 `process`（注意带 `/api/v1` 前缀，裸路径是 404）。

---

## 3. 鉴权：哪些接口不用 token

这是实测结果，不是文档抄的：

| 端点 | 是否要 Bearer |
|------|---------------|
| `/healthz` `/readyz` | 不要 |
| `/api/v1/auth/register` `/login` `/refresh` `/logout` | **不要**（logout 走 body 里的 `refresh_token`，不靠 Bearer） |
| `/api/v1/agents` `/agents/{id}` `/capabilities` `/capabilities/{id}` | **不要**（登录页要展示系统里有谁，所以免鉴权） |
| `/metrics` | **不要 access_token，但要另一把钥匙**：静态令牌 `SA_METRICS_TOKEN`（`X-Authorization` 或 `Authorization: Bearer <该令牌>`），没设就是 401 |
| 其余全部（`/tasks/*`、`/memories/*`、`/observability/*`、`/auth/me`、`/auth/password` 等） | **要** |

> 这一张表不是抄来的：2026-11-16 我逐个端点空手打过一遍。`/metrics` 当时返回 `401 Authorization 未授权`，
> 与「运维端点都不需要令牌」的直觉相反 —— 别在登录页把 `/metrics` 当免鉴权探针用。
>
> **注册链路可以自给自足**：`POST /api/v1/auth/register` 字段是 `username` / `email` / `password`（三者必填），
> 建完直接 `POST /api/v1/auth/login`，body 用 `{"account": "<username 或 email>", "password": "..."}`
> 拿 `access_token` + `refresh_token`。**不需要任何预置账号或在数据库里手插一行**，前端联调从零就能跑。
>
> 权威清单在 `docs/40-api-contract-ts.md` §鉴权行，与 `openapi.json` 一致；本文若与它冲突，以它为准。

流程：`POST /api/v1/auth/login` 拿 `access_token` + `refresh_token` → 后续 `Authorization: Bearer <access_token>`。

三个必须按规矩做的事（不做会在真机上偶发登出）：

1. **401 和 403 分开处理**：401 才刷新令牌，403 是权限不足，刷新只会把好令牌刷坏。
2. **刷新单飞**：同时 5 个请求 401 时，只能发一次 refresh，其余排队等结果。
3. **重放仅一次**：刷新后原请求重放一次；再 401 就登出，不要循环。

---

## 4. 坑一：`POST /tasks` 是异步的，不会秒回

`docs/30-ts-frontend-plan.md` 早期版本断言"同步秒回"，**那是错的，已更正**。

实测（`mode` 不传即默认 `async`）：

```
POST /api/v1/tasks  ->  202 Accepted
{ "id": "...", "status": "queued", "plan": [ 四步 ] }
Location: /api/v1/tasks/<id>
```

紧接着 `GET /tasks/<id>` 拿到的是中间态，不是终态：

```json
{ "status": "running", "results": [], "tokens_used": 0,
  "budget_tokens": 15600, "agreement_score": 0.68 }
```

**正确建模**：`提交 → 拿 id → 订阅 SSE → 收到终态事件后一次性取详情`。
不要写成"提交后直接渲染结果"，也不要写"轮询 1 秒一次"（契约支持轮询，但 SSE 已就绪，轮询纯属浪费）。

`mode=sync` 确实存在，但实测一次同步任务 **129.9 秒**，而浏览器/axios 默认 30 秒超时 → 任务明明成功，前端却判失败。**别用同步模式。**

---

## 5. 坑二：SSE 的 `seq` 会跳号，那不是丢帧

帧格式（逐字节实测，合规）：

```
id: 0
event: status

id: 299
event: status

: ping
```

- `content-type: text/event-stream`、`cache-control: no-cache`，`: ping` 是心跳注释帧。
- `id:` 是 `seq`，**全站共享的进程内原子自增号**（`AtomicU64`），不是任务内序号。
  每个订阅者收到全站事件后按 `task_id` 过滤，所以**你看到的 seq 从 0 直接跳到 299 是设计如此**。
- 判断"跟丢了"只有两个依据：① 收到重复的快照帧；② `sa_sse_replay_gap_total` 指标增长。
  **绝不能因为 seq 不连续就重连或报错** —— 这是最容易写错的一处。

续传：

- 断线重连带上 `Last-Event-ID: <最后收到的 seq>`。
- 服务端 `replay(task_id, after_seq)`：若缓冲里最早一条的 seq > `after_seq + 1`，说明中间有缺口，
  它会**回推一份完整快照**并计数，而不是假装接上。
- `Last-Event-ID` 为 0 或缺失 → 视为首连，直接给快照。
- 任务已终态且续传为空 → 服务端直接 EOF 释放订阅位（前端要能处理"连上就结束"）。

---

## 6. 坑三：列表接口是同一个信封

所有列表接口统一返回：

```json
{ "total": 42, "count": 20, "limit": 20, "offset": 0, "has_more": true, "items": [ ... ] }
```

`items` 是数组，其余是分页与计数。**别逐端点写解析**，写一个泛型 `list<T>()` 就够。

---

## 7. 坑四：onboarding 的"6 步"和"5 项必填"不是一回事

实测 `GET /api/v1/onboarding`：

```json
{ "status": "...", "total_required": 5, "completed_required": 0, "skipped": [],
  "next_step": "...", "next_action": "...",
  "steps": [ { "key": "...", "order": 1, "title": "...", "summary": "...",
               "action": "...", "required": true, "est_seconds": 30,
               "completed": false, "completed_at": null } ] }
```

- `steps` 长度 **6**，其中 `required: true` 的 **5** 项 —— 引导页要展示 6 张卡，但进度条按 5 算。
- `complete` 是**幂等**的：重复调用返回 `changed: false`，不要把它当错误。
- `reset` 需要二次确认，会清空已完成记录。

---

## 8. 坑五：契约里的 `OnboardingStatus` 是前端自建类型

源码里没有这个 enum。`docs/40-api-contract-ts.md` 里标了它是"前端自建"。
写 TS 时自己定义，别去 `types.generated.ts` 里找，找不到不是生成器坏了。

---

## 9. 实测接口形状速查（不是抄 openapi，是真打出来的）

| 端点 | 真实返回要点 |
|------|--------------|
| `GET /api/v1/agents` | 免鉴权；每个 agent 含 `id/kind/name/domain/...`，共 14 个 |
| `GET /api/v1/capabilities` | 免鉴权；共 26 项，含 `id/name/...` |
| `GET /api/v1/onboarding` | 见上节；`steps` 6 项 / 必填 5 项 |
| `GET /api/v1/cluster` | `nodes[]`（每节点 `agents_total: 14`）、`step_concurrency: 1`、`async_capacity: 8`、`sse_subscribers`、`events_dropped`、`tasks` 按状态计数 |
| `GET /api/v1/observability/overview` | `agents_total: 14`、`capabilities_total: 26`、`runner_backend: process`、`limiter_enforcing: true` |
| `GET /api/v1/observability/components` | `count: 5`、`restart_required_for_change: true`；每项含 `slot/active/available/env_var/contract/health/notes` |
| `GET /api/v1/tasks/{id}` | 异步期 `status: running` / `results: []` / `tokens_used: 0` / `budget_tokens: 15600` / `agreement_score: 0.68` |
| `GET /api/v1/tasks/{id}/trace` | 异步初期是 `[]`，别当接口坏了 |

`restart_required_for_change: true` 的含义：可插拔部件（runner 等）换件要重启进程才生效 ——
**前端展示"切换后端"按钮时，必须提示"需重启"**，否则用户会以为没生效。

---

## 10. 门禁怎么跑（改前端基本不用跑，改契约必须跑）

```powershell
$py = "D:\Program Files\Tencent\Marvis\MarvisAgent\1.60.3400.795\runtime\python311\python.exe"
& $py scripts/regress_all.py --tier quick      # 约 21s，改纯逻辑自查
& $py scripts/regress_all.py --tier api        # 约 80s，动了接口/存储层
& $py scripts/regress_all.py --tier full       # 约 420s，交付口径，29 道
& $py scripts/regress_all.py --with-drills     # 加跑负向演练，验证门禁本身会不会红
```

如果你（前端）改了契约相关的东西，至少跑这五道：
`4 契约一致性`、`5 OpenAPI 与 TS 同步`、`6 OpenAPI 未漂移`、`7 TS 契约文档一致`、`27 文档数字防漂移`。

**改完契约的正确姿势**：改 Rust 源码 → `scripts/gen-openapi.py` → `scripts/gen-ts-types.py` → 跑门禁。
顺序反了（先手改 TS）就会被第 5 道当场抓住。

---

## 11. 已知缺口（写在这里，免得你再发现一遍）

1. **前端目录不存在**。`docs/30-ts-frontend-plan.md` 里写的是计划目录结构，实际要你创建。
2. **`docs/30` 的 A1–A5 阶段表**里"轮询换 SSE"这条已作废 —— SSE 已就绪，v1 直接连事件流。
3. **搜索类能力**：`/capabilities` 里能看到，但派发链路是否完整没有端到端验证过，先别做成主路径。
4. **`agreement_score`（实测 0.68）** 是多个 agent 结论的一致度，不是置信度，别当成"把握有多大"展示给用户。
5. **`budget_tokens: 15600`** 是本次任务的 token 预算上限，超出会被截断，前端要能显示"预算已用 X%"。

---

## 12. 建议的第一步（照做就行）

1. `GET /healthz` 通 → `POST /auth/login` 拿 token → `GET /agents`（免鉴权，先验连通）。
2. 搭 Vite + TS 骨架，把 `types.generated.ts` 拖进去，写一个泛型 `list<T>()` 信封解析。
3. 先做**任务列表 + 详情**两页（这是全系统的价值主轴），SSE 一条事件流接完。
4. 再做 onboarding 与 agent/capability 展示（纯读，几乎没风险）。
5. 最后做 observability（图表多，但都是只读数据）。

做完第 3 步就能给用户看东西了，别按页面顺序做。

---

## 附：本文每句话的可信度分级

写交接单最容易犯的错，是把「文档里抄的」和「我真跑过的」混在一起，让下一棒分不清该信谁。
所以这里明码标价：

| 级别 | 内容 | 依据 |
|---|---|---|
| **A 实测过** | 默认监听 `127.0.0.1:8088`、换端口用 `SA_BIND`；`/healthz` `/readyz` `/api/v1/agents` `/api/v1/capabilities` 空手可打；`/metrics` 空手返回 401；裸 `/observability/overview` 返回 404；`doctor` 10 项输出 | 2026-11-16 直接起进程、直接发请求 |
| **B 契约保证** | 29 条路径 / 33 个操作 / 51 个 schema；注册字段 `username`+`email`+`password`；登录用 `account` 字段；`POST /tasks` 返回 202 + `status: queued` | `docs/openapi.json`（门禁 6 与 14 双向守着：契约与代码不一致会红，响应形状与契约不一致也会红） |
| **C 他人验证** | SSE 帧序列、`mode=async` 默认值、取消语义、刷新单飞的三条铁律 | 上一轮联调记录，本轮未复跑 |
| **D 没验过** | 前端真跑起来之后的行为、浏览器里的 CORS 细节、Vite 代理配置 | 前端一行代码都还没有，无可验 |

**用法**：C 级内容当"很可能对但要自己确认"，D 级内容当"没人知道"。发现 A 级错了请直接改本文并注明日期 —— A 级写错会误导下一棒去查不存在的东西（本轮就修掉了两处：把 `8088` 写成 `8080`、把 `/metrics` 写成免鉴权）。
