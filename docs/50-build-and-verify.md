# 50 · 构建与验证手册（可复现）

> 目的：任何人（包括另一个 AI agent）拿到这份仓库，都能在**不问我**的前提下，把后端编译出来、跑完单测、拿到和本文档一致的结论。
> 本文档只写**已验证过的事实**和**确定可执行的命令**；未验证的部分会明确标注"未验证"。

---

## 1. 前提检查

| 项 | 要求 | 怎么查 |
|---|---|---|
| 工具链 | `stable-x86_64-pc-windows-gnu`（不是 msvc，本机没装 MSVC C++ 工具链） | `rustup toolchain list` |
| Rust 版本 | 1.85+（`Cargo.toml` 里 `rust-version = "1.85"`） | `cargo --version` |
| 链接器 | `mingw64` 的 `ld.exe`（`D:\dev\msys64\mingw64\bin`） | `where gcc` |
| 智能应用控制 | **必须是关闭状态**，否则编译产物无法执行 | 见下方 §2 |

### 2. 为什么必须关闭「智能应用控制」（SAC）

SAC 处于强制模式时会拦截**所有未经签名且无云端信誉**的可执行文件，包括：

- `rustc` 编译出来的最小 hello world；
- cargo 依赖树里的 `build-script-build.exe`（`proc-macro2` / `quote` / `thiserror` / `httparse` / `getrandom` / `num-traits` 等）。

表现为：

```
error: failed to run custom build command for `proc-macro2 v1.0.107`
Caused by: could not execute process `...\build-script-build` (never executed)
Caused by: 应用程序控制策略已阻止此文件。 (os error 4551)
```

查当前状态（只读）：

```powershell
(Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy').VerifiedAndReputablePolicyState
# 0 = 关闭   1 = 强制(Enforce)   2 = 评估(Evaluation)
```

关闭路径（**只能由本人在 UI 操作，脚本不能代劳**）：

```
Windows 安全中心 → 应用和浏览器控制 → 智能应用控制 → 设置 → 关闭
```

> 微软的规则：SAC 一旦关闭，**只能通过重置或重装 Windows 重新开启**。请自行权衡。
> 关闭后建议重启一次再编译，否则个别情况下策略缓存仍在。

---

## 3. 为什么必须在纯 ASCII 路径下构建

本仓库所在目录名可能含中文与括号（例如 `新建文件夹(7)`）。MinGW 的 `ld.exe` 在处理这类路径时会链接失败。因此标准流程是：**源码留在原目录，构建在 ASCII 副本里做**。

副本位置建议 `%LOCALAPPDATA%\sa-build`（纯 ASCII、无空格、无括号）。

---

## 4. 标准流程（四步）

### 步骤 1：同步源码到 ASCII 副本

在 PowerShell 里执行（把 `$Src` 改成你的实际项目根目录）：

```powershell
$Src = "C:\Users\94355\Desktop\新建文件夹(7)"
$Dst = "$env:LOCALAPPDATA\sa-build"

New-Item -ItemType Directory -Force -Path $Dst | Out-Null
Copy-Item "$Src\Cargo.toml"   "$Dst\Cargo.toml" -Force
Copy-Item "$Src\Cargo.lock"   "$Dst\Cargo.lock" -Force
New-Item -ItemType Directory -Force -Path "$Dst\.cargo" | Out-Null
Copy-Item "$Src\.cargo\config.toml" "$Dst\.cargo\config.toml" -Force
robocopy "$Src\src" "$Dst\src" /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
```

`/MIR` 会让副本与源码完全一致（防止残留旧文件导致"改了却没生效"的假象）。

### 步骤 2：编译并跑单测

```powershell
$env:CARGO_TARGET_DIR = "$env:LOCALAPPDATA\sa-build\target"
Set-Location "$env:LOCALAPPDATA\sa-build"
cargo +stable-x86_64-pc-windows-gnu test --lib
```

首次编译约 2–4 分钟（15 个直接依赖 + 传递依赖），之后增量秒级。

### 步骤 3：判定通过的标准

期望输出结尾形如：

```
test result: ok. 80 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out
```

> 数量说明：基线 52 → 上轮 55 → **本轮 80**（新增 25 项：执行端口治理归属、审计部件可插拔、
> file 审计重启回读/坏行跳过/容量有界、写后落盘窗口、runner/audit 工厂登记一致性等）。
> **如果跑出来不是 80，说明仓库被改过，先 `git diff` 或对比 `docs/60-review-checklist.md` 再下结论。**

### 步骤 4：单独复跑新增的 3 项（最快确认改动生效）

```powershell
cargo +stable-x86_64-pc-windows-gnu test --lib -- task_submit_quota custom_step_runner authz_denial_precedes --nocapture
```

---

## 5. 端到端冒烟（123 项）：**已入库、已跑绿**

脚本：`scripts/smoke.py`（拉起真 exe、打真 HTTP、走真落盘）。

它做的事：复制一份独立数据目录 → 用 `Popen` 拉起编译好的 exe → 轮询 `/readyz` 直到 200 → 按业务顺序打真实 HTTP
（注册 / 登录 / 刷新 / 改密 / onboarding 全流程 / 记忆增删改查·过滤·分页 / 任务提交·轮询·追踪·过滤·分页 / 限流打满 / 预算耗尽 / 越权 / 审计查询 / 指标抓取）→
逐项断言状态码与字段 → `finally` 里终止进程并回收数据目录 → 打印 `合计 N 项 | 通过 N | 失败 0`。

一条命令（无需手动起服务）：

```powershell
cd "C:\Users\94355\Desktop\新建文件夹(7)"
python scripts\smoke.py
# 若 exe 不在默认位置（%LOCALAPPDATA%\sa-build\target\debug\super-agents.exe）：
python scripts\smoke.py --exe "<你的 exe 绝对路径>"
```

**期望结果**（2026-11-09 实跑，原始输出见 `docs/evidence/regress-all.raw.txt`）：

```
合计 123 项 | 通过 123 | 失败 0
[INFO] sa_http_requests_total 是否出现: False   （口径来源 obs/metrics.rs::inc_http；若 False 说明该方法尚无生产调用点）
```

> 那条 `[INFO]` 是**如实报告**：`sa_http_requests_total` 这个指标**在源码里定义了、但没有生产调用点**，
> 所以 `/metrics` 里不会出现它。这不是漏测，是当前实现的已知状态（见 `10-verification-report.md` §10 的缺口表）。
> 冒烟脚本故意把它打成 INFO 而不是 PASS，就是为了不让人误以为"指标齐全"。

覆盖范围（按断言编号区间，由 `scripts/smoke.py` 里的 `Sxx` 标签机械统计，**合计 123，与最终输出一致**）：

| 编号区间 | 断言数 | 覆盖内容（括号内为代表性断言） |
|---|---|---|
| S01–S09 | 10 | 就绪轮询、`/healthz`、`/readyz` 语义、`/metrics` 三态鉴权、Prometheus 文本类型、`sa_uptime_seconds`、**存储后端可识别**（S01b） |
| S10–S25 | 16 | 首个用户 201 且为 owner、`onboarding_status`、`expires_in`、重复注册 409、弱口令 422、错误口令 401、refresh 轮换与旧令牌复用 401、refresh 冒充 access 401 |
| S26–S37 | 12 | 教程总览、初始 `not_started`、必做 5 / 共 6、完成幂等、未知步骤 422、跳过、重置 |
| S38–S51 | 19 | 记忆 201 / 同 key 覆盖 200 且 `version` 递增、详情、更新、非法 `scope` 422、删除 204、删后 404；**`q` 关键字命中/排除、能命中 tags、`scope` 过滤真生效、分页两页不重叠**（S44b–S44f） |
| S52–S58 | 7 | agents 目录 9 个、详情、未知 agent 404、capabilities 26 项、未知 capability 404 |
| S59–S71 | 24 | 提交 201、同步执行到 `succeeded`、plan 每步带 `agent_id`、trace 含 `execute` 且 tokens>0、空目标 422、未知任务 404、预算过低 422、预算 200 触发 `aborted_budget`；**`status` 过滤、分页两页不重叠、`aborted_budget` 可被过滤**（S68/S71d） |
| S72–S77 | 8 | 第二用户为 member、跨用户读任务 403、member 可提交、访问管理员概览 403、可写 `shared`、写 `system` 必须 403、看不到他人私有记忆 |
| S78–S83 | 18 | 概览 200、`agents_total=9`、审计留痕（含拒绝）、三个计数器出现；**`audit_backend`/`runner_backend` 部件可视化（S80b/S80c）、`decision=deny` 过滤、非法 decision 422、metrics 带 `backend="sim"` 标签、404 兜底也被计数** |
| S84–S86 | 3 | 登出 200、`revoked_sessions≥1`、登出后 refresh 失效 401 |
| S87–S92 | 6 | 连续提交命中 429（实测第 56 次触发）、`Retry-After=59`、成功次数 ≤60、限流按用户隔离、限流计数器出现、**已限流用户即使请求非法也先返 429**（限流先于校验，已知行为） |

> 一点说明：上表的"断言数"是**标签数**，与脚本打印的"合计 123 项"同一口径（脚本每 `rec()` 一次算一项）。

### 5.1 起服务手工验证（可选）

```powershell
$env:SA_DATA_DIR = "$env:LOCALAPPDATA\sa-build\data-smoke"
$env:SA_METRICS_TOKEN = "0123456789abcdef0123456789abcdef"   # ≥24 字符，否则 /metrics 会被配置校验拒绝启动
cargo +stable-x86_64-pc-windows-gnu run --bin super-agents
# 另开一个窗口（/readyz 与 /metrics 挂在根路径，不带 /api/v1 前缀）：
curl.exe -s -o NUL -w "%{http_code}" http://127.0.0.1:8088/readyz
```

> 环境变量全集见 `10-verification-report.md`；本节用到的是 `SA_DATA_DIR`、`SA_BIND`（默认 `127.0.0.1:8088`，见 `src/config.rs:50`）、`SA_METRICS_TOKEN`。

## 5.2 契约生成与漂移检查（新增，两条命令）

```powershell
python scripts\gen-openapi.py            # → docs/openapi.json（24 路径 / 28 操作 / 50 schema）
python scripts\gen-openapi.py --check    # 与磁盘上的文件比对，退出码 1 = 源码改了但没重新生成
python scripts\gen-ts-types.py           # → generated/types.generated.ts
python scripts\gen-ts-types.py --check   # 同上，校验模式
```

> 这两条命令**不编译 Rust**、不启服务，纯静态解析源码，因此可以放进任何 CI 前置步骤，耗时 < 1 秒。
> 判据：`--check` 退出码 0 表示"磁盘上的契约文件与源码完全一致"，这就是"文档有没有报假"的机器判据。

---

## 6. 常见失败与对策

| 现象 | 原因 | 对策 |
|---|---|---|
| `os error 4551 应用程序控制策略已阻止此文件` | SAC 强制模式 | 见 §2 |
| `ld: cannot open output file ...` 或链接阶段莫名失败 | 构建路径含中文/括号 | 严格按 §3 用 ASCII 副本 |
| `error: linker 'cc' not found` | 用了 msvc 工具链 | 显式加 `+stable-x86_64-pc-windows-gnu` |
| 改了源码但测试行为没变 | 副本没同步 | 重跑 §4 步骤 1（`/MIR`） |
| `could not find Cargo.toml` | 当前目录不是副本根 | `Set-Location $env:LOCALAPPDATA\sa-build` |

---

## 7. 项目结构与构建相关的两个约定

1. `.cargo/config.toml` 里锁定了目标工具链与链接器参数，**不要删**，否则中文路径问题会立刻复现。
2. `target/` 永不入库、不入交接包；它可以从零重建（本手册 §4 就是重建流程）。
