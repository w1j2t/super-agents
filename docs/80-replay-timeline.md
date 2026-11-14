# 80 · 任务时间轴回放（设计稿）

> 一句话：**后端不用存历史**。把 SSE 当"该刷新了"的脉冲，把 `trace` 当"发生了什么的"底片，把本地时钟当"正在发生"的推进器，前端就能把任何一次任务放成一条可拖拽、可回刷、可对比的时间轴。

写给谁：老板自己后续实现前端时照抄。本文所有结论都来自 2026-11-11 的真实运行数据（见附录），不是推演。

---

## 1. 先划清边界：手上到底有什么料

| 料 | 来源 | 粒度 | 特点 |
|---|---|---|---|
| 事件流 | `GET /api/v1/tasks/{id}/events`（SSE） | **任务级** | 只有状态跳变，**没有步骤信息**；首帧 `seq=0` 是快照 |
| 轨迹 | `GET /api/v1/tasks/{id}` 里的 `trace[]` | **步骤级** | 持久化在任务记录里，任务结束后仍可拿到全量 |
| 计划 | 同上 `plan[]` | 步骤级 | 带 `est_tokens`（可做"预期 vs 实际"双轨） |
| 心跳 | SSE 的 `keep-alive: ping` | 连接级 | 证明"管道还活着"，与任务死活无关 |

三条硬约束，先认清，省得白写代码：

1. **SSE 断点续传已具备**：重连带 `Last-Event-ID: <最后收到的 id>`，服务端从环形台账补发后续帧，**不会重推 `seq=0` 快照**；补发为空且任务已终态则主动 EOF。所以"缺口"只剩一种成因——台账已滚出窗口，此时服务端退化为快照重发并计 `sa_sse_replay_gap_total`。前端仍应保留"回查 `trace`"的兜底路径，但不必假设事件流天然不连续。
   （旧版本此处写"没有断点续传"，与 `api/handlers/task_events.rs` 实现不符，已订正；契约由门禁 17 的 A7 系列守护。）
2. **事件里没有步骤**：想画步骤条，数据只可能来自 `trace`。SSE 的唯一价值是**告诉你"该再拉一次 trace 了"**，不要试图从事件里榨出步骤信息。
3. **`trace.at` 是"写入时刻"，约等于该次尝试的结束时间，不是开始时间**。
   `phase="sandbox", outcome="started"` 这条记录，实测与它紧随其后的 `retryable_error` 相差 **30 微秒** —— 它记的是"沙箱这一步结束"，不是"开始"。
   👉 由此得到本文最重要的一条算法原则（见 §3）。

---

## 2. 时间轴怎么成像：把散点折成条段

`trace` 是一串离散点。要画成时间轴，必须折叠成"条段"：

```
条段(segment) = 一个 (step_index, attempt) 组合
  起点 = 该组合第一条 trace 的 at
  终点 = 该组合最后一条 trace 的 at（或下一条 trace 的 at）
  颜色 = outcome（ok / retryable_error / fatal_error / 失败）
  标签 = agent_id + phase + detail 摘要
```

派生指标（全部可由前端自行算出）：

| 指标 | 算法 | 用途 |
|---|---|---|
| 单步真实耗时 | 本步**首条** trace.at − 上一步**末条** trace.at | 找出"到底哪一步慢" |
| 单次尝试耗时 | 同 step 内，attempt k 的 at − attempt k-1 的 at | 重试阶梯的每一级宽度 |
| 收尾开销 | `finished_at` − 末条 trace.at | 判断是不是编排层自己拖慢 |
| 静默时长 | `now` − 末条 trace.at | 进度条"自己走"的依据 |
| 重试次数 | 同 step 内 attempt 的最大值 | 阶梯高度 |

**不要**做的事：把 `sandbox.started` 与 `execute` 配对算耗时 —— 那样会算出 0.00003 秒的假数据。

---

## 3. 四个核心做法（这套设计的价值都在这）

### 3.1 静默期让进度自己走（解决"界面像死机"）

实测：`doc-agent` 接本地模型后，**一次调用 111.8 秒**。这 112 秒里 SSE 一条事件都没有——后端没跑完就不会广播。用户看到的就是"点了提交，界面定住了"。

做法：

- 时间轴对"进行中的条段"启用 200ms 本地 tick，显示 `进行中 01:23`；
- 若能拿到该 agent 的单步超时配置，显示 `01:23 / 05:00` 成比例进度；拿不到就只显示已耗时（诚实优于假精确）；
- 静默 > 30s 时，该条段进入"呼吸"态，附一句人话：**"该步骤通常较慢（本地模型推理），已 45s"**。

> 这里有个能救命的小判据：**用 SSE 的 ping 区分"在跑"和"断了"**。
> - 有 ping、无 status 事件 → **任务在执行，别催**；
> - ping 也停了 → **连接断了**，立刻走重连 + 回查。
>
> 很多"看起来卡死"的界面纠纷，本质是没区分这两种情况。

### 3.2 重试画成阶梯，不画成一行

失败样本（真实）：`doc-agent` 三次尝试，每次都在 20s 被强杀：

```
doc-agent ███ 20.0s ✗
doc-agent ███ 20.2s ✗
doc-agent ███ 20.4s ✗   结论：3/3 均超时（20s 上限），非"慢"，是"被砍"
```

三根平行短条比一行红字信息量高一个量级：用户一眼看出"不是等了很久，而是被反复打断"，从而知道该去调超时，而不是去查模型。

### 3.3 失败提示卡：把踩过的坑前置到界面上

维护一张纯前端规则表，匹配 `detail` 文本 / `outcome` / `agent_id`，命中就在时间轴上挂提示卡：

| 命中条件 | 提示卡内容 |
|---|---|
| `detail` 含 `脚本执行超时（>20s）` 且 agent=`doc-agent` | 该 agent 接了外部模型，默认单步 20s 不够。设 `SA_RUNNER_TIMEOUT_DOC_AGENT=<秒>` 后重启后端。**注意是下划线：写成 `DOC-AGENT` 后端不认且不报错** |
| 同一步 `retryable_error` ≥ 3 次 | 外部依赖可能不在线（模型服务 / 端口 / 密钥），给出一条 `curl <SA_LLM_BASE>/models` 的自检命令 |
| `agent=guard-agent` 且 `outcome` 非 ok | 合规拦截，展示 `detail` 里的规则名，并说明"阻断权在治理层，不在 worker" |
| 单步耗时 > 60s 且成功 | 提示"该步为耗时主因（占本次任务 X%）"，并给出是否值得优化 |

这一条把时间轴从"日志查看器"升级成"半个诊断助手"——**这是我认为最值得做的部分**，因为踩坑知识现在只存在于文档和记忆里，用户现场永远看不到。

### 3.4 回放模式（事后复盘 / 演示 / 调优对比）

任务跑完后 `trace` 是完整的，用绝对时间戳直接做 `1x / 4x / 16x` 回放 + 可拖拽进度条：

- **演示**：给谁看都能 30 秒讲清"这套系统怎么跑"；
- **排查**："哪一步慢"用回放最直观；
- **回归对比**：同一 goal 两次运行的时间轴并排，直接看出"这次为什么慢了"。

实现成本极低：trace 已在任务记录里，纯前端按时间戳重放即可，不需要后端加任何存储。

---

## 4. 断线缺口：诚实比流畅重要

```
事件到达 → 记下 seq
  期望 seq = 上次 seq + 1
  ┌ 相等      → 正常追加
  ├ 跳号      → 缺口：立刻拉一次 trace + 详情合并，缺口段画虚线斜纹，
  │              悬浮提示"此处 N 条事件未收到（已按 trace 补齐）"
  └ 收到 seq=0 → 这是重连后的快照帧，不是新事件：丢弃旧的推测，以快照为准
```

设计原则一句话：**能补齐的补齐，补不齐的明说**。缺口段用虚线与实线段区分开，用户会信任这条时间轴；掩盖缺口，一次断网就会让整条轴变得不可信。

幂等要求：同一份 `trace` 折叠两次必须得到同一条时间轴（按 `(step_index, attempt, at)` 去重合并，增量 append 而非整体替换）。这决定了"刷新页面不闪、不丢步骤"。

---

## 5. 渲染草图

```
任务 30d3a9a1 ─ writing ─ succeeded ─ 529 tok ─ 一致性 0.89 ─ 总耗时 112.2s
──────────────────────────────────────────────────────────────────
00:00.0 ┃▇▇ super-agent  意图解析        ok      94 tok     0.0s
00:00.1 ┃▇▇ doc-agent     ████████████████████████ ok  278 tok  111.9s  ← 耗时主因 99.7%
01:51.9 ┃▇▇ guard-agent  合规扫描          ok       63 tok     0.1s
01:52.0 ┃▇▇ super-agent  汇总              ok       94 tok     0.1s
        └─ 后三步合计 0.2s，占 0.2% ─┘
```

失败形态：

```
00:00.0 ┃▇▇ super-agent  意图解析      ok
00:20.0 ┃▇▇ doc-agent    ██ 20.0s ✗ 超时(20s)  ┐
00:40.2 ┃▇▇ doc-agent    ██ 20.2s ✗ 超时(20s)  ├ 重试阶梯 3/3
01:00.6 ┃▇▇ doc-agent    ██ 20.4s ✗ 超时(20s)  ┘
01:00.6 ┃   ⚠ 提示卡：单步 20s 不够用；设 SA_RUNNER_TIMEOUT_DOC_AGENT=300
01:00.7 ┃▇▇ guard-agent  合规扫描      ok
```

---

## 6. 前端实现要点

**状态机**

```
idle → snapshot(收到 seq=0) → streaming → (缺口 → resync → streaming)
                                        → terminal(status 终态)
                                        → replay(终态后，可拖拽/倍速)
```

**三个数据源的分工**

| 源 | 职责 | 频率 |
|---|---|---|
| SSE | 触发刷新、判断连接死活 | 长连接 |
| `GET /tasks/{id}` | 拉全量 `trace` 作为底片 | 事件触发 + 缺口触发 |
| 本地 tick(200ms) | 推进"进行中"条段 | 定时器 |

**伪代码**

```ts
let lastSeq = -1
let segments: Segment[] = []

es.addEventListener('status', (e) => {
  const ev = JSON.parse(e.data)            // {seq, status, at, summary, ...}
  if (ev.seq === 0) { lastSeq = 0; resync(); return }   // 快照帧

  if (ev.seq !== lastSeq + 1) markGap(lastSeq + 1, ev.seq - 1)  // 缺口，标虚线
  lastSeq = ev.seq

  if (TERMINAL.includes(ev.status)) { resync().then(enterReplay) }
  else resync()                                          // 顺手刷一次底片
})

async function resync() {
  const detail = await api.task(id)        // 注意：实际返回是平铺结构，见 §8 坑位
  segments = foldTrace(detail.trace)       // 幂等折叠
  render(segments)
}
```

**折叠函数的关键**（直接照抄）

```ts
function foldTrace(trace: TraceEntry[]): Segment[] {
  const sorted = [...trace].sort((a, b) => +new Date(a.at) - +new Date(b.at))
  const out: Segment[] = []
  let cur: Segment | null = null
  for (const t of sorted) {
    if (!cur || cur.stepIndex !== t.step_index || cur.attempt !== t.attempt) {
      cur = { stepIndex: t.step_index, attempt: t.attempt, agent: t.agent_id,
              start: null, end: t.at, events: [] }
      out.push(cur)
    }
    if (!cur.start) cur.start = prevEndOf(out, cur)   // ← 起点取"上一段终点"，不用 t.at
    cur.end = t.at
    cur.events.push(t)
  }
  return out
}
// 理由：trace.at 是完成时刻，用 t.at 当起点会得到 0 宽度的条段
```

---

## 7. 加分项（按性价比排序，有余力再做）

1. **宽度即耗时**：条段宽度按真实耗时线性映射，别做成等宽卡片。一眼看出瓶颈在哪。
2. **可粘贴的文本时间轴**：把折叠结果导成一段等宽文本（就是 §5 那种），能直接贴进 issue / 文档 / 聊天，不用截图。成本极低，传播性极强。
3. **双跑对比**：同一 goal 两次运行并排，左旧右新，差值和耗时差异自动高亮。调优时一次顶十次。
4. **预期 vs 实际双轨**：`plan[].est_tokens` 与实际 `tokens` 画两条轨道，偏差大的步骤标黄——能直接暴露"估算模型不准"的步骤。
5. **IndexedDB 缓存 trace**：离线也能回放最后一次任务；多标签页之间还能复用。

---

## 8. 踩坑清单（都是今天真踩到的，实现前先看一眼）

| # | 坑 | 现象 | 正确做法 |
|---|---|---|---|
| 1 | 按 agent 覆盖超时的键名 | 写成 `SA_RUNNER_TIMEOUT_DOC-AGENT` → 不报错、不生效，每步 20s 被砍、重试 3 次、任务 `partially_succeeded` | `SA_RUNNER_TIMEOUT_DOC_AGENT`（agent id 大写 + `-` 换 `_`） |
| 2 | `GET /tasks/{id}` 返回结构 | **平铺**（顶层就是 `id/status/trace/results`），与 `openapi.json` 里的 `TaskDetail{task, trace, memory_refs}` **不一致** | 取值写兼容函数（先看有没有 `task` 包一层）；这条建议尽快对齐契约 |
| 3 | 注册 vs 登录字段名 | 注册要 `username`，登录要 `account` | 两边各写各的，别照着一个抄 |
| 4 | `POST /tasks` 是同步执行 | 接模型后请求挂 100s+，客户端超时了任务其实还在跑 | 客户端超时必须 > 单步超时 × 步数；或后续改异步（见 §9） |
| 5 | `trace.at` 语义 | 当成"开始时间"用会算出 0.00003s 的假耗时 | 见 §3，一律用"相邻完成点做差" |

---

## 9. 想让体验再上一层，后端可选改这三处（不改也能跑）

| 建议 | 成本 | 收益 |
|---|---|---|
| ~~事件里带上 `last_seq` 或支持 `Last-Event-ID` 回补~~ **已完成** | 中 | 已实现 `Last-Event-ID` 补发（门禁 17 A7 系列守），"缺口虚线"不再是必然 |
| `trace` 支持 `?since=<seq/时间>` 增量拉取 | 低 | 长任务 trace 上千条时，不必每次全量拉 |
| `POST /tasks` 改 `202 + task_id`，进度全靠 SSE | 中 | 客户端不再挂长请求，前端体验质变 |

**明确不建议**：为"回放"专门建一张事件历史表。`trace` 已经持久化在任务记录里，回放原料齐了，再存一份只是给自己加同步负担。

---

## 10. 验收清单

- [ ] 断网 5s 再恢复：时间轴出现虚线缺口，自动补齐，步骤一条不少
- [ ] 长步骤（>60s）静默期：耗时数字持续递增，不出现"卡死"观感
- [ ] 断开连接（不只是断网）：能凭 ping 消失判定"连接断了"，而不是"任务卡了"
- [ ] 三次重试：阶梯显示 3 级，结论行写明"3/3 超时"
- [ ] 失败提示卡：用 `doc-agent` 20s 超时的真实样本能命中（样本见附录 B）
- [ ] 回放：4x 回放的总时长 ≈ 真实总耗时 / 4（误差 < 1s）
- [ ] 刷新页面：时间轴与刷新前完全一致（折叠幂等）

---

## 附录 A · 成功样本（2026-11-11，模型 `minicpm5-2b:f16`）

```
11:10:05.743  step0  super-agent  execute  ok                tok=94
11:11:57.712  step1  doc-agent    sandbox  started           （实为结束时刻）
11:11:57.712  step1  doc-agent    execute  ok                tok=278
11:11:57.826  step2  guard-agent  execute  ok                tok=63
11:11:57.920  step3  super-agent  execute  ok                tok=94
finished_at   11:11:57.920        总 tokens=529
```

按 §2 算法还原的耗时：

| 步骤 | 间隔 | 说明 |
|---|---|---|
| super-agent 意图解析 | 0.0s | 首步，无前序 |
| **doc-agent（模型）** | **111.969s** | worker 自报 111.8s，误差 0.2s ✓ 算法成立 |
| guard-agent | 0.114s | 规则扫描 |
| super-agent 汇总 | 0.094s | |
| 收尾 | 0.000s | finished_at 与末条 trace 同刻 |

## 附录 B · 失败样本（同日，键名写成连字符时）

```
11:00:20.684  step0  super-agent  ok
11:00:40.697  step1  doc-agent    attempt1  retryable_error  脚本执行超时（>20s）已强制终止
11:01:00.921  step1  doc-agent    attempt2  retryable_error  （距上次 20.224s）
11:01:21.342  step1  doc-agent    attempt3  retryable_error  （距上次 20.421s）
11:01:21.342  step1  doc-agent    step      failed
11:01:21.500  step2  guard-agent  ok
11:01:21.617  step3  super-agent  ok
```

这条样本是"重试阶梯"的黄金测试用例：三级宽度应呈现 20.0 / 20.2 / 20.4 的微弱递增（递增部分是每次重试的退避与开销）。

> 原始响应留证：`docs/evidence/2026-11-11-llm-doc-agent-live.json`

---

## 相关文档

- `40-api-contract-ts.md` —— 类型来源
- `71-modularity-and-upgrade.md` —— 可插拔槽位
- `81-llm-worker-live-check.md` —— 本次实测记录与复跑方法
- 源码注释：`src/domain/event.rs`（事件语义）、`src/api/handlers/task_events.rs`（SSE 契约）
