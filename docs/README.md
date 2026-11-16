# 文档地图（Personal Super Agents）

按阅读顺序排列。前三份是"现在的事实"，后两份是"接下来的路"。

> **如果你是接手做前端的 Agent，只看一份文件：[88-frontend-handoff.md](88-frontend-handoff.md)。**
> 那里写着后端怎么起、契约在哪、五个会让你白干的坑，目标是让你 30 分钟内提交出第一个任务。
> 本文其余部分不用通读——需要查某个细节时再回来。

> **只想核对、不想通读？直接看 [70-audit-handoff.md](70-audit-handoff.md)** —— 那是给第三方 AI/人用的审计交接书：
> 交付物清单、5 分钟证伪路径、20 条"主张→证据→复现"对照、10 条"明确没做"的清单、7 处自检抓出的真缺陷。

| # | 文档 | 内容 | 读者 |
|---|---|---|---|
| 00 | [00-project-plan.md](00-project-plan.md) | 项目总纲：目标、边界、模块划分 | 先读 |
| **01** | [01-overview.md](01-overview.md) | **六部分① 总体定位**：六条设计公理↔门禁映射、五个可插拔槽位、一次请求的完整生命周期、模块地图、六部分导航 | 想搞清"它是什么" |
| **02** | [02-capability-catalog.md](02-capability-catalog.md) | **六部分② 能力清单**：26 项能力的五要素（触发 / 权限边界 / 可观测信号 / 降级策略），**由源码生成** | 想确认"能干什么" |
| **03** | [03-division-of-labor.md](03-division-of-labor.md) | **六部分③ 分工协作**：14 个 agent 的花名册与权限档位、路由打分与派发门槛（打不到信号即拒，不猜类型），**由源码生成** | 想确认"谁干什么" |
| **04** | [04-interaction-spec.md](04-interaction-spec.md) | **六部分④ 交互规范**：鉴权流程、Problem 错误模型与**按操作真实分布的错误码**、任务与 SSE、取消、前端三条铁律 | 写前端 / 联调 |
| **05** | [05-boundary-handling.md](05-boundary-handling.md) | **六部分⑤ 边界处理**：三道闸门、七权限、七条红线、预算降档、熔断阈值、限流配额、不可配置清单 | 想知道"什么会被拒绝" |
| **06** | [06-acceptance-criteria.md](06-acceptance-criteria.md) | **六部分⑥ 验收标准**：29 道门禁逐条说明、命令清单、六部分验收矩阵、抽查项、已知未达标、准入准出 | **想知道"算不算做完"** |
| **07** | [07-api-reference.md](07-api-reference.md) | API 索引（**由 openapi 生成**）：路径 / 操作清单与数据结构一览 | 查接口 |
| 10 | [10-verification-report.md](10-verification-report.md) | **后端验收报告（历史时点）**：**截至 2026-11-11 为 131 单测 + 156 项端到端冒烟（json/memory 双后端各一遍）+ 14 道门禁全绿**；当前口径（29 道）见 `06` / `88`（§11 与 `evidence/regress-all.txt`）；**当前口径以 `evidence/regress-all.txt` 与逐门禁 latest 为准（24 道门禁）**、3 个真实缺陷修复、环境变量清单、已知缺口清单 | 想知道"做完了没" |
| 20 | [20-rust-python-architecture.md](20-rust-python-architecture.md) | Rust 热路径 + Python 编排的落地设计：进程协议、延迟预算、可靠性、4 步迁移路径 | 决定下一步怎么演进 |
| 30 | [30-ts-frontend-plan.md](30-ts-frontend-plan.md) | **TypeScript 前端完整方案**：选型理由、目录结构、数据流分层、双令牌会话、路由与页面、任务状态机、性能预算、5 阶段实施 | 要动手写前端 |
| 40 | [40-api-contract-ts.md](40-api-contract-ts.md) | **API 契约逐字段对照源码**：24 条路径 / 28 个操作、全量 TS 类型、queryKey 规范、失效规则、SSE 预留契约、联调核对清单 | 写前端时放在手边 |
| 50 | [50-build-and-verify.md](50-build-and-verify.md) | **构建与验证手册（可复现）**：SAC 拦截说明、ASCII 副本编译四步、判定标准、失败对策 | 想亲手验证的人 |
| 60 | [60-review-checklist.md](60-review-checklist.md) | **交付核对清单**：给另一个 AI 的作业本，每条断言都带源码位置与证伪方法 | 要审计这份交付的人 |
| 70 | [70-audit-handoff.md](70-audit-handoff.md) | **审计交接书**：交付物清单 / 5 分钟证伪路径 / 20 条主张-证据-复现 / 10 条"没做"清单 / 冲突裁决顺序 | **交给外部 AI 核对时先给这份** |
| 71 | [71-modularity-and-upgrade.md](71-modularity-and-upgrade.md) | **模块化部件图与换件手册**：**3 个真·可插拔部件**（存储 / 执行 / 审计）的端口、装配点、影响面；新增后端的逐步操作（存储 5 处、执行 4 处、审计 2 方法）；换件后 **10 条不变式**；两轮抓到的 3 个真缺陷全记录 | 想换部件 / 想确认"换件不殃及全部" |
| 80 | [80-replay-timeline.md](80-replay-timeline.md) | **任务时间轴回放设计稿**：只用 SSE 当触发脉冲 + `trace` 当底片 + 本地时钟推进，**后端不必存历史**；含折叠算法、断线缺口处理、重试阶梯、失败提示卡、回放倍速、7 条验收清单 | 要写前端时间轴时照抄 |
| 81 | [81-llm-worker-live-check.md](81-llm-worker-live-check.md) | **worker 接入本地模型实测记录**：MiniCPM 2B（Ollama）两次端到端复现、`SA_LLM_*` 与按 agent 超时的正确接法、模型产出质量客观评价、**CPU 推理 100s 量级的事实**、踩到的 5 个坑 | 想确认"模型真接上了没" |
| 82 | [82-multi-node-cluster-drill.md](82-multi-node-cluster-drill.md) | **多节点集群双进程实录**：真起 3 个进程验 8 个场景共 **44 项断言**（互通 / 跨节点负载可见 / 崩溃掉线不污染汇总 / 孤儿心跳清理 / Ctrl+Break 注销 / 单机开关）；含真实 JSON、**4 个取舍的理由**、以及"打磨一个功能"的固定五步 | 想让第二台机器分担工作时 |
| 83 | [83-worker-text-quality.md](83-worker-text-quality.md) | **worker 分词质量打磨**：9 个 worker 共用的 `_common.py` 修掉 9 类切词缺陷 + 3 个潜伏坑（停用词表从未生效 / 三引号里的 `#` 不是注释 / 判据误伤设计）；25 条真实目标同尺子 **17→25**、脏词条目 **12→0**；固化为门禁 16 | 文档标题/关键词冒出碎片时 |
| 84 | [84-feature-gap-audit.md](84-feature-gap-audit.md) | **功能缺口审计**：声明 vs 实现逐项对账——44 项声明指标里 22 项曾在源码零落点、26 项能力按「完整/骨架/文字级」分级、5 处文档失实订正；第 9 节按时间倒序持续追加进展 | 想知道「哪些是纸面功能」 |
| 85 | [85-llm-evidence-and-boundaries.md](85-llm-evidence-and-boundaries.md) | **真模型实测证据与两处刻意边界**：5 份 soak 证据总表（轮数/耗时/采纳率）、JSON 模式归因（-23%，只给方向性结论）、告警「只识别不处置」与压测撞限流的取舍、G1~G8 判据口径 | 想核对「模型到底有没有被采用」 |
| 86 | [86-vertical-agents.md](86-vertical-agents.md) | **垂直 agent 的准入、增补与裁撤**：四道准入（任务类型/可评审产物/对错判据/独立知识库）、本轮 game/finance/legal/data 四域立册依据、`collab-agent` 归并判据、新增 agent 的**十三处同步清单**与 6 条守护测试 | 想加或想删一个 agent 时**先读这份** |
| 87 | [87-cli-client.md](87-cli-client.md) | **CLI 客户端**：七个入口与"无参 = serve"的硬约定、令牌文件与取值优先级、**401 自愈里的真缺陷**（刷新漏查 `tokens` 一层，把已作废的旧 refresh 写回）、8 进程并发刷新的三级退避、`doctor` 十项与退出码三分、三个"看起来正常"的显示坑（显示宽度 / UTC 标签 / PowerShell 编码）、门禁 22 与 23 的分工 | 想用命令行提交任务时 |
| 88 | [88-frontend-handoff.md](88-frontend-handoff.md) | **前端交接单（写给下一个对话）**：30 分钟内跑起前端并成功提交第一个任务的最小路径、必踩的坑、令牌与端口口径；文中每个数字都由门禁 27 实时比对代码，写错就变红 | 另开对话做前端时**先读这份** |
| 89 | [89-agent-design-trends.md](89-agent-design-trends.md) | **Agent 设计趋势与本项目对照**：三条主线（从「能管住」到「会变好」）逐条打分，只挑三件便宜且收益大的事动手；含「裁判必须是确定性代码」这条硬约束与假绿事故复盘 | 想知道「下一步该往哪补」时 |
| 90 | [90-metric-credibility.md](90-metric-credibility.md) | **指标可信度声明**：报表上哪些数字能做决策、哪些不能。camp-g 的编译率为何可信、重判修正后的读数自洽关系、以及**必须打折看的三处**（样本太少 / 两个语言桶未合并 / 三桶在真实会话上为空） | 要拿数字汇报或判断「模型行不行」之前 |
| 91 | [91-split-strategy.md](91-split-strategy.md) | **切分策略：为什么实现了却默认不用**：整包任务「先骨架后填实」的 A/B 实测——纯重跑救回率高于三段切分近 2.5 倍且只花 43% 成本，McNemar 不显著但成本确定；附 `--retry-placeholder` 开关的正确用法与三条「为什么更差」真因 | 想改整包任务切分或加长流程前 |
| 92 | [92-self-evolution-loop.md](92-self-evolution-loop.md) | **自进化闭环**：用本地小模型当探针、真实长跑当显微镜、主 Agent 负责修、再用同一把尺子量回来。含第一代真修了什么、以及怎么防止自己骗自己（假绿事故的三种形状） | 想知道「改完凭什么说变好了」时 |

## 可执行的自检工具

| 脚本 | 作用 | 命令 |
|---|---|---|
| `scripts/check-contract.py` | 把 40 号文档的每个 `interface` 与源码 `pub struct` 字段集合逐个比对，输出缺失/多余/重复字段 | `python scripts/check-contract.py`（退出码 0 = 全一致） |
| `scripts/gen-ts-types.py` | **不读文档、直接读 Rust 源码**生成前端 TS 类型到 `generated/types.generated.ts`；无法识别的类型会打 WARN，不猜 | `python scripts/gen-ts-types.py`（`--check` 只校验不写盘） |
| `scripts/gen-openapi.py` | **读路由表 + handler 签名 + 数据结构**生成 `docs/openapi.json`（OpenAPI 3.1：29 路径 / 33 操作 / 51 schema，错误码按操作真实行为挂载）；零侵入，不改后端 | `python scripts/gen-openapi.py`（`--check` 防漂移，可直接进 CI） |
| `scripts/check-openapi.py` | 把 `openapi.json` 的每个 schema 与源码 `pub struct` 比对：**字段集合 / 可选性（Option 是否被错标 required）/ 枚举取值** | `python scripts/check-openapi.py`（退出码 0 = 0 处不一致） |
| `docs/evidence/round3-summary.txt` | **一页式验证汇总（历史留档）**：4 个一致性检查 rc=0、单测 55 绿、冒烟 98 绿，含中文路径编译坑说明。最新数字见 `regress-all.raw.txt` | — |
| `scripts/smoke.py` | **拉起真实 exe 打真实 HTTP** 的端到端冒烟，**157 项断言**，覆盖鉴权/限流/预算/追踪/过滤/分页/审计/指标 | `python scripts/smoke.py`（退出码 0 = 157/157 通过；`--exe` 指定可执行文件） |
| `scripts/check-modularity.py` | **模块化静态守护**：扫全部 `.rs`，禁止业务层出现具体后端名、禁止越级调 `backends::`/`audit::open`、校验 `StorageKind/RunnerKind/AuditKind::ALL` 与工厂分支同步、环境变量单点读取（仅 `config.rs`） | `python scripts/check-modularity.py`（退出码 0 = 15/15） |
| `scripts/swap_drill.py` | **换件演练（行为级）**：同一套 18 个请求在 json/memory 与 memory/file 两组后端各跑一遍，比对状态码与响应结构签名；同一任务在 sim/process 下比对计划、权限档位、trace 口径、token 记账；另含**硬杀重启后台账可追溯**与坏配置拒启；开跑前先做**端口段体检**（这段口有人应答就自动顺移并 WARN，`D0` 验证检测器本身不失灵） | `python scripts/swap_drill.py`（退出码 0 = 23/23） |
| `scripts/verify_runner.py` | **执行端口实测**：`SA_RUNNER=process` 真跑外部脚本，验证标记真的出现、治理未绕过、缺脚本是可重试失败、非法配置拒启 | `python scripts/verify_runner.py`（退出码 0 = 15/15） |
| `scripts/regress_all.py` | **一键全量门禁（29 道）**：单测 + 冒烟×2 后端 + OpenAPI 语义/未漂移 + TS 同步 + 契约文档 + 执行端口 + 部件矩阵 + 换件演练 + 审计自检 + 响应形状 + 多节点集群 + worker 分词质量 + 异步受理与缺省；判定口径见 `docs/06` | `python scripts/regress_all.py`（退出码 0 = 26 道全绿；`--with-drills` 另跑负向演练，不计入门禁数） |
| `scripts/build-exe.py` | **门禁 0：编译 + 自证二进制新鲜**。`cargo test` 不更新 `super-agents.exe`，于是出现过"12 道全绿、测的是 18:48 旧程序"的假绿；本脚本真编译并校验 exe mtime ≥ 最新源码，落后即红 | `python scripts/build-exe.py`（退出码 0 = 编译成功且新鲜） |
| `scripts/check-prompt-coverage.py` | **门禁 12：需求覆盖对齐**。把首个提示词拆成术语清单，逐条查六部分文档有无落点，并校验源码 26 个能力 id 与设计公理 A1~A6 全覆盖；缺一即红（已做负向演练确认它会红） | `python scripts/check-prompt-coverage.py`（退出码 0 = 全覆盖） |
| `scripts/probe_hallucination.py` | **门禁 13：防幻觉打点实测**。造 5 条长记忆压到上下文裁剪，再抓 `/metrics` 断言 `sa_hallucination_flags_total` 与 `sa_context_truncations_total` 真有值——治的是"规格写了、代码没打点" | `python scripts/probe_hallucination.py --exe <exe>`（退出码 0 = 13/13） |
| `scripts/probe_llm_live.py` | **真模型链路探针**：起真实后端 → 注入 `SA_LLM_*` → 提交任务 → 拉 `trace` 打印时间轴 → 断言 P1/P2/P3（模型名透传 / tokens>0 writes 真走了模型 / 路由命中正确 agent）；支持 `--model` `--exe` `--port` | `python scripts/probe_llm_live.py --model minicpm5-2b:f16`（退出码 0 = 全绿） |
| `scripts/verify_cluster.py` | **门禁 15：多节点集群双进程演练**。真起 3 个进程共用一份心跳目录，验 8 个场景 **44 项**：互通 / 跨节点负载可见 / 汇总=明细求和 / 强杀掉线标 `stale` 且不污染 totals / 6×TTL 孤儿清理 / Ctrl+Break 立刻注销 / `SA_CLUSTER=off` 不落盘。**单进程怎么测都测不到这类问题** | `python scripts\verify_cluster.py`（退出码 0 = 44/44；`--skip-prune` 跳过 30s 清理等待，约 18s） |
| `scripts/probe_worker_text.py` | **门禁 16：worker 分词质量**。25 条真实业务目标（文档 / 数字单位 / 文件整理 / 排障 / 集群 / 周报 / 英文型号 / 否定约束 / 语音…），四项判据分流：`loss` 真词丢失 / `rank` 关键词排序退化 / `dirty` 脏词 / `topic_bad` 主题整句复读。**前 15 道门禁没有一道守"文字处理"**——`_common.py` 退化只会让 doc-agent 的标题悄悄冒出碎片 | `python scripts\probe_worker_text.py`（退出码 0 = 25/25；`--verbose` 打动过程，`--json` 出机读结果） |
| `scripts/probe_async_task.py` | **门禁 17：异步受理与缺省**。`POST /tasks` 的缺省必须是 `async`（202 + `queued` + `X-SA-Exec-Mode: async` + `Location`），显式 `mode=sync` 仍返回 201 + 终态；顺带守 202 的语义完整性：事件流必须有 `running` 中间态且 `seq` 严格递增、并发上限 429 不留幽灵、取消收敛 `cancelled`、**SSE 断点续传**（带 `Last-Event-ID` 只补后续帧 / 不重推快照 / 非法锚点退回首次连接 / 空补发主动 EOF / 锚点超前不挂连接）。**24 项**。历史动机：缺省曾与 `openapi` 声明的 `default: async` 相反，真模型同步提交 129.9s 撞上客户端 30s 超时，任务成功却被判失败 |
| `scripts/probe_llm_soak.py` | **门禁 18：模型路径真采用**。本地小模型真跑多轮（model / fast / guard 三分组），判据落在"**产出是否被采用**"而不是"接口是否 200"：`outline_source=model` 且 `model.used=true`、model 组每轮 `tokens>0`、fast 组路由逐轮命中、guard 组坏输入被拒且不建单、全程 5xx 为 0、任务台账无幽灵。缺模型时返回 **rc=3（SKIP）**，`regress_all.py` 单列 skipped 且**不显示为 PASS**。`--workers-dir` 可指向改坏副本做负向演练 | `python scripts\probe_llm_soak.py --model-rounds 12 --fast-rounds 24`（`--probe-model-rounds 1` 先探单轮耗时） |
| `scripts/probe_alerting.py` | **门禁 19：可观测告警行为**。阈值压到必然命中，逐段断言**数字**而非接口形状：命中 → 去重窗口内抑制 → 过窗口复发 → 风暴只留严重级别 → 指标真落点；三阶段各起独立进程（策略只能启动时经环境变量给定，同进程内改参数等于假门禁）。`--drill-no-dedup` 是负向演练入口（去重关掉后 A8/B3 必须转红） | `python scripts\probe_alerting.py`（退出码 0 = 全过，约 12s；`--drill-no-dedup` 期望红） |
| `scripts/probe_topic_guard.py` | **门禁 20：主题取词质量**。12 条真实任务原文喂给三个垂直 worker 的取词函数，判"取出来的是人能用的词"：命中域内对象词 / 不是半句 / 不是碎片 / 不是视角词顶替 / 兜底串不被误判；七条句法判据（含标点、指示量词开头、超长含虚词、补语动量收尾、含疑问词、连词开头、两字趋向尾）。**关键词与域词表都与线上同源**（`_common.keywords` + `_topic_guard.DOMAINS`）——第一版探针自带词表，12/12 全绿而真实产出是 `段剧情`/`上线`，绿的是探针自己 | `python scripts/probe_topic_guard.py`（退出码 0 = 12/12；`--drill-no-guard` 拿掉守卫，期望成片变红） |
| `scripts/probe_vertical_verdict.py` | **门禁 21：垂直域判据**。三个垂直域（narrative / market / qa）各自的判据表是否**在**（A 结构）、是否**用得上**（B 该抓就抓）、**会不会串域**（C 跨域隔离）、**主题是否可读**（D），共 24 项。门禁 20 只守"主题取得对"，主题对只是前提，判据表退化了不会有任何一道门禁变红 | `python scripts/probe_vertical_verdict.py`（退出码 0 = 24/24，约 0.8s；`--drill` 清空叙事词表，B 组必须转红） |
| `scripts/probe_cli.py` | **门禁 22：CLI 外壳行为**。守一条从没写下来的约定：**无子命令 = serve**（前 21 道全部以 `[exe]` 无参启动）。另验 `--help` 列全 serve/doctor、未知子命令被拒绝而非静默当 serve、`doctor` 的文本与 `--json` 逐项同数同码、退出码 0/1/2 真能分开、跑完端口释放不留残留，共 22 项 | `python scripts/probe_cli.py --exe <exe>`（退出码 0 = 22/22，约 5s；`--fast` 只跑 A/C 组，`--drill` 换 `where.exe` 须 10 项全红） |
| `scripts/probe_cli_http.py` | **门禁 23：CLI 客户端行为**。真起服务、真登录、真提交，57 项断言分六组：登录落盘与覆盖 / **401 → 自动刷新 → 重试并回写**（含 8 进程并发刷新自愈）/ 表格按**显示宽度**对齐且 UUID 完整 36 位、时间标 UTC / `--json` 可被 `json.loads` 直接解析 / 连不上必须 rc=1 而非崩栈。抓出的真缺陷：刷新时漏查 `tokens` 嵌套一层，把**已作废的旧 refresh 写回文件**，下一条命令必 401 | `python scripts/probe_cli_http.py --exe <exe>`（退出码 0 = 53/53，约 17s；`--fast` 只砍并发项，`--drill` 须 51 项全红） |
| `scripts/probe_lazy_stack.py` | **门禁 24：懒加载三件套接缝**。压缩（`_compact.py`）/ 检索（`_rag.py`）/ 取网页（`_web.py`）三个零依赖模块各有自检，但**零件全绿不等于装起来能用**：本道只测接缝——构造不读盘不建索引、首次使用才加载、上限不被绕过、抓取→入索引→检索→压缩→引用可回读整条链，13 项 | `python scripts/probe_lazy_stack.py`（退出码 0 = 13/13，约 13s；`--drill` 三种坏法须各自变红） |
| `scripts/probe_sched.py` | **门禁 25：自动任务调度**。守第一件"会自己按时间动起来"的东西，防三类**沉默型退化**：①把执行顺手写进 `due()`（派发条数照对、心跳却被拖死，故用 AST 扫描 + 运行时桩 + 哨兵文件三重自证"它不动手"）；②`mark()` 只改内存不落盘（同实例连查天然幂等，重启后才暴露）；③漏跑诊断恒返回空（一个功能两个答案）。另守日历边界 8 例（月末 31 / 闰日 / 周日 0≡7 / 跨年首分钟）、CLI 与库同库且原子写无 `.tmp` 残留、坏行与非法 cron 不影响派发，19 项 | `python scripts/probe_sched.py`（退出码 0 = 19/19，约 0.3s；`--drill` 三种坏法须分别红在 G2 / G3 / G4c） |

> 以下为 2026-11-09 的历史留档（**当时的单测/冒烟/门禁数已全部过时，勿据此判断当前状态**）；
> 当前口径以 `docs/evidence/regress-all.raw.txt` 为准（**29 道门禁**），一条命令可复跑：`python scripts/regress_all.py`：
> 下列数字只描述当时那一次运行：
> - `cargo test`：**80 passed / 0 failed**（含沙箱记账归属、审计部件、重启回读、坏行跳过、容量有界等守护）
> - `smoke.py`：**123 项断言，123 通过 / 0 失败**，且 **json / memory 两套存储后端各跑一遍全绿**
> - `check-contract.py`：21 个 interface 全部一致，0 差异
> - `check-openapi.py`：40 个 struct schema + 9 个枚举 schema，0 处不一致（字段/可选性/枚举取值）
> - `gen-ts-types.py` / `gen-openapi.py`：40 interface + 9 联合类型、24 路径 / 28 操作 / 50 schema，`--check` 幂等
> - `verify_runner.py`：**15/15**（真跑外部进程）
> - `swap_drill.py`：**23/23**（换件后上层零感，含审计部件与重启可追溯、端口段体检 D0）；`check-modularity.py`：**15/15**（静态守护）
> - 留档：`docs/evidence/regress-all.txt`、`docs/evidence/regress-all.raw.txt`、`docs/evidence/modularity.txt`

## 三句话总结

1. **后端已可验收**：能跑（**174 单测 + 157 项端到端冒烟，双后端各一遍全绿**）、能观测（概览直接报当前部件）、能控权、数据能扛硬杀重启、无硬编码密钥；真实缺陷（预算假性超支、`/metrics` 抓取、任务视图缺档位、沙箱记账错位、落盘窗口丢数据、审计只写不回读）已修并回归。
2. **架构切口已验证**：约束留在 Rust（预算是父进程说了算），算力交给 Python（子进程 JSONL 协议）；`trait StepRunner` 已就位，加一个 `ProcessRunner` 即可切真执行、治理层不动。**前端尚未编写**（用户自写 TS），因此契约先行、切真执行时前端无需改动。
3. **前端类型不再靠手写**：`generated/types.generated.ts` 由源码生成，后端一改就重跑一次，杜绝"文档说一套、代码是另一套"。

## 三个改进项的真实现状（不是计划，是台账）

| 项 | 状态 | 代码位置 | 验证状态 |
|---|---|---|---|
| P0 抽 `trait StepRunner`（原计划名 `trait Executor`，见下方命名说明） | **代码已完成** | `src/orchestration/executor.rs`：`StepRequest` / `StepOutcome` / `trait StepRunner` / `SimRunner`；`Executor.runner: Option<&dyn StepRunner>` | **已验证**：新增 2 项测试随全量跑绿（`custom_step_runner_replaces_backend_and_keeps_governance`、`authz_denial_precedes_step_runner`） |
| P0 任务提交限流 60 次/h/用户 | **代码已完成** | `src/api/handlers/task.rs`：`pub const TASK_SUBMIT_QUOTA`、`create()` 第 0 步 `limiter.check`；`src/security/ratelimit.rs`：`task_submit_quota_is_60_per_hour_per_user` | **已验证**：该测试随全量跑绿 |
| P1 引入 `utoipa` 导出 `openapi.json` | **已达成目标，未用 utoipa** | `scripts/gen-openapi.py` → `docs/openapi.json`（3.1）+ `scripts/gen-ts-types.py` → `generated/types.generated.ts` | 两个产物都由源码生成、已实跑、`--check` 可进 CI；**没有** Swagger UI / 运行时校验。取舍见 `60-review-checklist.md` §E1 |

> **为什么第三项换方案**：utoipa 需要给 28 个操作补约 375 行注解，漏一条就会产出"看起来有、实际过期"的契约，比没有更危险；
> 而生成器零侵入、随源码自动同步，对"只有自己一个前端"的阶段更划算。两者不冲突：将来端点冻结要上 Swagger UI，utoipa 与生成器可并存。

### 验证环境说明（原"尚未运行"的原因已消除）

2026-11-09 之前的记录写的是"被智能应用控制（SAC）拦截、无法运行"。
现 `VerifiedAndReputablePolicyState = 0`，已在 `%LOCALAPPDATA%\sa-build`（ASCII 副本）完整跑通：

```
cargo +stable-x86_64-pc-windows-gnu test --lib
test result: ok. 80 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 5.10s
```

端到端冒烟**已重跑**（脚本已入库）：`python scripts/smoke.py` → **123 项通过 / 0 失败**，json 与 memory 两套后端各一遍。原始输出留档在 `docs/evidence/regress-all.raw.txt`，可直接复核。

### 为什么是 `trait StepRunner` 而不是 `trait Executor`

`Executor` 这个名字在本项目里已被"治理外壳"占用（鉴权 → 预算 → 重试 → 熔断 → 状态收敛）。
如果把"执行动作"的抽象也叫 `Executor`，两者会撞名且语义含混。
真正需要被替换的是**单步执行后端**，所以抽象命名为 `StepRunner`，`Executor` 保持不变。
这是对原计划的**修正**而非偏离：目标（可替换后端、治理层不动）完全达成。

