# 82 · 多节点集群：双进程实录与打磨记录

> **一句话**：`GET /api/v1/cluster` 已在**两台真进程**上跑通——互相看得见、跨节点负载可见、
> 崩溃掉线不污染汇总、孤儿心跳自动清理、Ctrl+Break 立刻注销。共 **44 项断言**，
> 已固化为 `regress_all.py` 的第 16 道门禁（编号 15）。
>
> 证据：[`evidence/cluster-drill-latest.txt`](evidence/cluster-drill-latest.txt)、
> [`evidence/cluster-view-captured.json`](evidence/cluster-view-captured.json)
> 复现：`python scripts\verify_cluster.py`

---

## 1. 这个功能要回答的三个问题

集群视图不是为了"看起来像分布式系统"，它只回答三句人话：

| 问题 | 对应字段 | 如果不答会怎样 |
|---|---|---|
| 还有谁活着？ | `nodes[]`、`stale`、`offline_nodes` | 任务都堆在一台机上，另一台空转，人不知道 |
| 谁在扛活？ | 每节点 `tasks`、`async_running` | 只看汇总会把负载倾斜平均掉，看不清是哪台在忙 |
| 死掉的那台算不算数？ | `totals` 只累加 `stale=false` | 陈旧值混进汇总，"集群在跑几个任务"永远不下降 |

第三句是最容易做错的一句，也是本项目唯一一处**宁可少报、不可虚报**的地方。

---

## 2. 边界先划清（明确不做什么）

| 做了什么 | 没做什么 | 为什么不做 |
|---|---|---|
| 共享目录心跳（`SA_CLUSTER_DIR`） | 节点间 HTTP 互探 | 省掉 http client 依赖，且**失败是单向的**：读不到 B 只影响"少看见一个节点"，不会因为 B 卡住把 A 的 `/cluster` 一起挂住 |
| 被动接收心跳 | 主动服务发现 / 一致性哈希 | 个人部署 2–3 台，`read_dir` 就够；引入注册中心是给运维加负担 |
| 给**建议**的负载数字 | 自动把任务派到空闲节点 | 任务当前在本机执行器上跑；跨机分发要先解决代码/产物同步，属下一步 |
| 单节点也返回 `nodes[]` 数组 | 单节点时省略数组 | 形状提前定死，加第 2 台不破坏前端与 OpenAPI |

代价写在明处：共享目录要求**目录对所有节点可见**，跨网段 / 跨机房不适用。
真要跨机房，换的只是 `src/services/cluster_registry.rs` 这一个文件，`/cluster` 的响应形状不动。

---

## 3. 真实响应（2026-11-12 抓取，非示意）

### 3.1 单节点

```json
{
  "nodes": [{
    "node_id": "node-a", "role": "all-in-one",
    "uptime_seconds": 3, "step_concurrency": 1,
    "async_running": 0, "async_capacity": 8,
    "sse_subscribers": 0, "events_dropped": 0,
    "agents_total": 9,
    "tasks": { "total": 0, "succeeded": 0, "...": "8 个状态字段" },
    "source": "local",
    "updated_at": "2026-11-12T14:05:36.876314300Z",
    "stale": false
  }],
  "totals": { "nodes": 1, "offline_nodes": 0, "agents_total": 9, "async_capacity": 8, "tasks": { "total": 0 } }
}
```

### 3.2 双节点（从 A 看出去，B 上刚提交了 1 个任务）

```json
{
  "nodes": [
    { "node_id": "node-a", "source": "local", "tasks": { "total": 0 }, "stale": false },
    { "node_id": "node-b", "source": "peer",  "tasks": { "total": 1, "succeeded": 1 }, "stale": false }
  ],
  "totals": { "nodes": 2, "offline_nodes": 0, "agents_total": 18, "async_capacity": 16,
              "tasks": { "total": 1, "succeeded": 1 } }
}
```

读三个数就够判断这份数据是否可信：

- `agents_total: 18` = 9 + 9，**明细求和等于汇总**；
- `totals.tasks.total: 1`，而 A 自己 `0`、B 是 `1` —— **别人的活没记在自己头上**；
- `node-a` 的 `totals.tasks.total == 0` 说明 A 没偷数，`node-b.source == "peer"` 说明它确实来自心跳文件而非本地采集。

---

## 4. 八个场景与真实观测值

`scripts/verify_cluster.py` 真起 3 个进程（A、B、关掉心跳的 off），TTL=5s、心跳周期=1s。

| 场景 | 验什么 | 真实观测 |
|---|---|---|
| C1 单节点起步 | 只有自己、`source=local`、`offline_nodes=0`、目录里只有自己一份心跳 | 8 项通过 |
| C2 双节点互通 | 两侧都看见 2 节点；**自己永远排第一**；B 在 A 眼里是 `peer` 且不 stale；汇总=明细求和 | 14 项通过（`agents_total` 18=18、`async_capacity` 16=16） |
| C3 跨节点负载 | B 上提交 3 个任务，A 侧 `totals.tasks.total` 必须等于 3；A 自己仍为 0 | 5 项通过（实测 3 / 3 / 0） |
| C4 心跳实时性 | 再提交 2 个，一个周期内汇总涨到 5；`updated_at` 被推进 | 2 项通过（3→5，13:58:53 → 13:58:56） |
| C5 崩溃掉线 | **强杀** B：文件还在、B 标 `stale=true`、`offline_nodes=1`、`nodes` 仍为 2，但 `totals.tasks.total` 回到 0 | 6 项通过（`agents_total` 9，不含离线的 B） |
| C6 遗物清理 | 超过 6×TTL 的孤儿文件被物理删除，节点从列表消失 | 3 项通过（`nodes=['node-a']`） |
| C7 优雅退出 | Ctrl+Break（用户按 Ctrl+C 同一条路径）→ `rc=0`，心跳文件立刻消失 | 2 项通过 |
| C8 单机开关 | `SA_CLUSTER=off`：`/cluster` 仍 200，只报自己，不落盘、也不读别人的 | 4 项通过 |

合计 **44 项 / 全部通过**，耗时 48.5s（其中 33s 是 C6 在等清理线）。

C5 特意用强杀而不是 kill -15：`TerminateProcess` 不给进程任何收尾机会，正是断电 / 蓝屏的最坏情况。
**能被 TTL 兜住的，就不该依赖优雅退出**——优雅退出是加分项，不是正确性前提。两条路都验，才敢说这个功能不脆。

---

## 5. 四个"看起来可以更简单"的决定

1. **`stale` 节点仍留在 `nodes[]`，但逐出 `totals`**
   报表口径上这是"不干净"的做法，但两件事必须分开：看板要显示"某某节点 8 秒前失联"（所以要留），
   "集群正在跑几个任务"不能因为一台机断电而永远停在 5（所以要逐出）。混在一起做，两个目标互相拆台。

2. **`local` 节点永远排第一，peer 按 `node_id` 排序**
   `read_dir` 的顺序随文件系统变。不排序的话，前端每刷新一次节点顺序就换一次，做 diff 会误判"节点换了"。

3. **心跳先写 `.tmp` 再 `rename`**
   读侧可能在任何时刻撞上写侧。直接覆盖会读到半截 JSON，表现是"某个节点时有时无"——这类间歇性故障最费时间。

4. **坏文件静默跳过，只留 debug 日志**
   一个写坏的心跳文件不能让整个 `/cluster` 变 500。那会把"某个节点写坏了"升级成"所有人都看不见集群"。

---

## 6. 怎么复现

```powershell
# 只跑演练（约 50s）
python scripts\verify_cluster.py

# 快速版：跳过 6×TTL 清理等待（约 18s）
python scripts\verify_cluster.py --skip-prune

# 保留临时数据目录，便于复查心跳文件
python scripts\verify_cluster.py --keep

# 只想快跑回归时（门禁 15 走 fast 分支：跳过 6×TTL 等待，逻辑断言一条不少，
# 44 项 → 41 项，48.6s → 23.1s；证据单独落 docs\evidence\fast\，不覆盖正式证据）
python scripts\regress_all.py --fast
```

每次全量回归都会带这一道：

```
15 多节点集群双进程   rc=0   48.5s   合计 44 项 | 通过 44 | 失败 0
```

---

## 7. 下一步（从"看得见"到"派得动"）

当前集群是**观测面**，不是**调度面**。要让第二台真分担工作，缺的是：

1. 任务的**目标节点**字段 + 提交时路由（`POST /tasks` 带 `node_id`，或"最闲节点"策略）；
2. 跨机执行的**产物回传**（本机执行器写出的文件怎么让提交方拿到）；
3. 节点级**配额与熔断**（现在是每进程各算各的，全局 429 口径还没统一）。

这三件都不改 `/cluster` 的形状——协议先定死，正是为了这时候只填不破。

---

## 8. 本文档顺带沉淀的"打磨一个功能"的固定动作

后续每个功能都照这条链走，不再临时决定：

1. **先定边界**：这个功能回答什么、明确不做什么、代价是什么；
2. **端到端硬证据**：真起进程跑，断言精确到数字（不是"能通"）；
3. **固化门禁**：脚本进 `regress_all.py`，改坏了自动红；
4. **补契约**：`docs/40-api-contract-ts.md` + `openapi.json` + `types.generated.ts` 三处同步；
5. **写取舍**：为什么不做更简单的那个方案，留给下一个人（可能就是三个月后的自己）。
