# TypeScript 前端完整技术方案（Phase-A 单机版）

> 前提：后端已提供 `/api/v1` REST JSON；本前端**不假设任何后端内部结构**，只按契约消费。
> 目标：一个人能维护、十年不烂、把「超级 agents」控制感做出来的桌面级 Web 应用。

## 1. 技术选型（每个"为什么"都写清楚）

| 层 | 选择 | 版本基线 | 为什么是它 |
|---|---|---|---|
| 框架 | **React 19** | ^19 | 生态最大；`useOptimistic`/`useTransition` 天然适配"任务提交后立刻乐观更新" |
| 语言 | **TypeScript** | ^5.7 | 用户明确要求；`strict` 全开 |
| 构建 | **Vite 7** | ^7 | 冷启 <1s；无需 Next.js——本项目**不需要 SSR**（单机私有部署，SEO 无意义） |
| 路由 | **TanStack Router** | ^1 | 类型安全路由（路径参数自动推导），比 React Router 的类型手写强一档 |
| 服务端状态 | **TanStack Query v5** | ^5 | 缓存/失效/重试/分页一站式；与后端 REST 模型完全契合 |
| 客户端状态 | **Zustand** | ^5 | 只存"UI 态"（当前会话、侧栏、主题、草稿），不碰服务端数据 |
| 表单 | **React Hook Form + Zod** | ^7 / ^3 | Zod schema 同时用于运行时校验与 **TS 类型推导**（`z.infer`），一处定义两处受益 |
| 样式 | **Tailwind CSS v4** | ^4 | 零运行时；v4 的 CSS-first 配置减少脚手架噪音 |
| 组件 | **shadcn/ui（Radix 底座）** | 最新 | 源码进仓库，非黑盒依赖；可访问性（a11y）由 Radix 兜底 |
| 图标 | **lucide-react** | 最新 | 体积小、风格统一 |
| 动画 | **Motion（Framer Motion 后继）** | ^12 | 只在**状态转换**处用（任务卡进入/步骤推进），不做全局动效 |
| 测试 | **Vitest + Testing Library + MSW** | ^3 | MSW 用同一套 handlers 同时喂测试与本地开发 mock |
| E2E | **Playwright** | ^1.4x | 覆盖"注册→教程→首个任务"主路径 |
| 代码质量 | **ESLint 9（flat）+ Prettier + tsc --noEmit** | — | CI 三关 |
| 包管理 | **pnpm** | ^10 | 磁盘友好、依赖严格（不hoist，避免幽灵依赖） |

**明确不选**：
- Next.js —— 无 SSR/SEO 需求，App Router 的复杂度换不到收益。
- Redux Toolkit —— 服务端状态已由 Query 承担，剩下的 UI 态用 Zustand 十几行解决。
- 任何 UI 大而全的组件库（AntD/MUI）—— 定制成本高于 shadcn。

## 2. 目录结构（功能切分，不按文件类型切）

```
web/
  src/
    app/
      router.tsx              # TanStack Router 路由树
      providers.tsx           # QueryClient / Theme / Toast
      main.tsx
    shared/
      api/
        client.ts             # fetch 封装：基址、鉴权、错误归一、刷新
        errors.ts             # ApiError 类 + 错误码 → 用户话术映射
        types.gen.ts          # 由 docs/openapi.json 生成（见 §7）
      lib/
        cn.ts / format.ts / storage.ts
      ui/                     # shadcn 生成的原子组件
    features/
      auth/       { api.ts, hooks.ts, pages/LoginPage.tsx, pages/RegisterPage.tsx, store.ts }
      onboarding/ { api.ts, hooks.ts, pages/OnboardingPage.tsx, steps/*.tsx }
      tasks/      { api.ts, hooks.ts, pages/TaskListPage.tsx, TaskDetailPage.tsx,
                    pages/NewTaskPage.tsx, components/TaskCard.tsx, TraceTimeline.tsx,
                    components/BudgetMeter.tsx, store.ts }
      memories/   { api.ts, hooks.ts, pages/MemoryPage.tsx, components/MemoryEditor.tsx }
      agents/     { api.ts, hooks.ts, pages/AgentsPage.tsx, pages/CapabilityPage.tsx }
      observability/ { api.ts, hooks.ts, pages/OverviewPage.tsx, AuditPage.tsx }
    widgets/
      AppShell.tsx  Sidebar.tsx  Topbar.tsx  CommandPalette.tsx
    styles/globals.css
  e2e/
  tests/
```

原则：**feature 内自洽**（api/hooks/pages 同域共存），跨域共享才上提 `shared/`。禁止出现 `components/` 这种按类型横向切的巨型目录。

## 3. 数据流分层（三层，不许越界）

```
UI 组件 ──(只读 props / 触发事件)
   │
   ├─ TanStack Query（服务端状态：缓存、失效、重试）
   │       └─ features/*/api.ts（唯一允许发请求的地方）
   │
   └─ Zustand（客户端状态：UI 开关、草稿、主题）
```

硬规则：
1. 组件**禁止**直接 `fetch`。
2. Query 的 `queryKey` 统一从 `features/*/hooks.ts` 导出常量，禁止散落字符串。
3. 变更后**显式** `invalidateQueries`，不靠"轮询刷新一切"。

## 4. 鉴权与会话（双令牌 + 静默刷新）

### 4.1 令牌存放

| 令牌 | 存放 | 理由 |
|---|---|---|
| access（30 min） | **内存变量**（Zustand 非持久化） | 不落 localStorage，抗 XSS 窃取；刷新页面靠 refresh 换新 |
| refresh（7 d） | `localStorage` | 单机私有部署可接受；若将来上公网，改 httpOnly Cookie + CSRF Token |

### 4.2 client.ts 核心逻辑（伪码）

```ts
let accessToken: string | null = null;
let refreshing: Promise<string> | null = null;

async function request<T>(path: string, init: RequestInit & { auth?: boolean } = {}): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(accessToken && init.auth !== false ? { Authorization: `Bearer ${accessToken}` } : {}),
      ...init.headers,
    },
  });

  if (res.status === 401 && init.auth !== false && getRefreshToken()) {
    refreshing ??= refreshAccessToken().finally(() => { refreshing = null; });
    accessToken = await refreshing;          // 并发 401 只刷一次
    return request<T>(path, init);           // 原样重放
  }
  if (!res.ok) throw await toApiError(res);
  return res.status === 204 ? (undefined as T) : res.json();
}
```

要点：
- **单飞刷新**（`refreshing` 承诺共享），避免 10 个并发请求同时刷。
- 重放**仅一次**，二次 401 直接登出并提示。
- 刷新令牌是**轮换式**（旧令牌重放即 401，已实测），新 refresh 必须落盘覆盖。
- 401 与 403 语义分开：401 未认证 → 刷新/登录；403 已认证但越权 → 提示"无权限"，不跳登录。

### 4.3 冷启动流程

```
读取 localStorage.refresh
  ├─ 无 → /login
  └─ 有 → POST /auth/refresh
        ├─ 成功 → 写内存 access → GET /auth/me
        │        └─ onboarding 未完成 → 强制跳 /onboarding（不可绕过）
        └─ 失败 → 清空 → /login
```

## 5. 路由与页面清单

| 路径 | 页面 | 关键点 |
|---|---|---|
| `/login` `/register` | 登录/注册 | 注册 409/422 需**字段级**错误定位（422 `details` 已含字段名） |
| `/onboarding` | 教程 6 步 | 见 §6 |
| `/` | 概览 | 4 个数字卡（任务数/成功率/今日 token/可用能力）+ 最近任务 |
| `/tasks` | 任务列表 | 无限滚动，状态筛选（`succeeded/aborted_*/running`） |
| `/tasks/new` | 新建任务 | 目标输入 + agent 选择 + 模型档位（默认 auto）+ 预算（可选，显示默认值） |
| `/tasks/:id` | 任务详情 | 左：轨迹时间线；右：预算计量条、memory_refs、model_profile |
| `/memories` | 记忆 | 三级作用域分栏（private/shared/system），system 只读提示"需管理员" |
| `/agents` | Agent 目录 | 14 个卡片，显示模式（只读/读写）与能力数 |
| `/capabilities` | 能力清单 | 26 项，按五要素（输入/输出/权限/计费/失败模式）展开 |
| `/observability` | 概览+审计 | 需 admin 角色；审计表支持按事件类型筛选 |

路由守卫三层：`未登录 → /login` → `未完成教程 → /onboarding` → `role 不足 → 403 页`。

## 6. 新手教程（onboarding）交互设计

后端契约：6 步，必填 5 项（`profile/agents/memory/first_task/boundaries`），支持 `complete`（幂等）、`skip`、`reset`。

- **左侧固定步骤导轨**，显示 `required 0/5` 进度；已完成打勾。
- **每步一屏**，只做一件事；右下角"下一步"，左上角"跳过"（软提醒）。
- 幂等：重复点击 `complete` 返回 `changed=false`，前端**不报错**、不重复推进。
- **首个任务步骤直接复用 `POST /tasks`**，成功后把返回的 `task_id` 写进该步 payload —— 用户教程里就真的跑成一个任务，这是最好的"惊喜点"。
- `reset` 需二次确认（会清空进度，属中风险操作）。

## 7. 类型来源：以 OpenAPI 为唯一真相（**现状已就绪**）

**2026-11-09 更新：后端已导出 OpenAPI 3.1 文件，不再需要 utoipa，也不再需要手写类型。**

| 事实 | 位置 | 怎么用 |
|---|---|---|
| 接口契约 | `docs/openapi.json`（29 路径 / 33 操作 / 51 schema，2026-11-16 复核） | 前端跑 `openapi-typescript` 从**本地文件**生成类型，不依赖服务启动 |
| TS 类型（另一条独立来源） | `generated/types.generated.ts`（62 interface + 12 联合类型） | 与上一条互相印证：两边接口名一致、字段一致才算对 |

```bash
pnpm add -D openapi-typescript
# 方式 A：从本地契约文件生成（推荐，离线可用、CI 可用）
pnpm openapi-typescript ..\docs\openapi.json -o src/shared/api/types.gen.ts
# 方式 B：连服务现拿（仅当后端已起，注意后端没有 /api-docs 端点，这个地址是给未来 Swagger UI 预留的）
# pnpm openapi-typescript http://127.0.0.1:8088/api-docs/openapi.json -o src/shared/api/types.gen.ts
```

> **别踩这个坑**：后端**没有** `/api-docs/openapi.json` 这个 HTTP 端点（`src/api/mod.rs` 里没挂）。
> `openapi.json` 是仓库里的**静态文件**，只能从磁盘读。上一版本本节写的 `http://.../api-docs/openapi.json` 是当时的设想，**已作废**。

**后端一改，前端怎么知道**：

```powershell
cd "C:\Users\94355\Desktop\新建文件夹(7)"
python scripts\gen-openapi.py --check   # 退出码 1 → 契约文件过期，提醒你重新生成并重跑 openapi-typescript
```

把这条命令挂到前端的 `predev` / `prebuild` 里，就形成"后端契约漂移 → 前端构建前必报"的闭环。

**仍然建议加 Zod**：`openapi-typescript` 只出**编译期**类型，运行时拿到脏数据（比如后端升级、mock 数据、代理改写）不会报错。对 `TaskDetail`、`AuthResult`、`OnboardingView` 这三个"前端主干依赖"的响应做 Zod 解析，成本很低、收益明显。

## 8. 关键交互：任务执行状态机

```ts
// 与后端 domain::TaskStatus 的 serde(rename_all = "snake_case") 逐字对应
type TaskStatus =
  | 'queued' | 'running' | 'succeeded' | 'partially_succeeded'
  | 'failed' | 'aborted_budget' | 'cancelled';
```

> **2026-11-16 实测更正（此前本节的"同步返回终态"已作废）**：`POST /tasks` 默认 `mode=async`，**返回 202 + 任务对象**（`status: "queued"`，`plan` 已展开为若干步），真正的执行在后台；随后用 `GET /tasks/{id}/events` 订阅 SSE 拿状态流。前端应按"提交→拿 id→订阅事件流"建模，不要再写"提交后秒回终态"。
>
> 实测帧序列（单次 curl 即可复现）：`id: 0` / `event: status` / `data: {...}` / 空行，随后 `: ping` 心跳帧，`content-type: text/event-stream`、`cache-control: no-cache`。
>
> **一个必须知道的坑**：`seq` 是**全站共享的进程内单调号**（`src/orchestration/events.rs` 的 `AtomicU64`），不是"本任务第几帧"。实测同一任务首帧 `seq=0`、第二个数据帧直接是 `seq=299`——这**不是丢帧**，中间是别人的事件占号。所以前端**不能**用 `seq` 是否 `+1` 来判断漏帧；判断"跟丢了"的唯一信号是重连后收到重复的快照帧，或观测 `sa_sse_replay_gap_total` 增长。`Last-Event-ID` 传最后收到的 `seq` 即可，服务端按 `> seq` 补发。

- **v1 就这么写**：进 `/tasks/:id` 立刻连 SSE，`status` 为终态（`succeeded`/`failed`/`partially_succeeded`/`aborted_budget`/`cancelled`）时服务端主动 EOF，前端关连接即可。
- 轮询只是**兜底**：SSE 连不上（代理不支持长连接）时退回 `GET /tasks/{id}`，`refetchInterval: 1500`，终态自动停。
- 预算计量条：`used_tokens / budget_tokens`，≥80% 变琥珀色，`aborted_budget` 时直接展示原因文案。
- 状态语义：`partially_succeeded` 表示部分步骤成功（**不是**失败，UI 用中性灰蓝而非红）；`failed` 才是整体失败。

## 9. 错误处理与话术映射

后端统一 `application/problem+json` 风格错误体（含 `code`、`message`、可选 `details`）。

| 场景 | 后端 | 前端表现 |
|---|---|---|
| 字段校验 | 422 + `details` | 逐字段红字（RHF `setError`） |
| 越界任务（合规红线） | 422 | 顶部横幅："该请求超出系统边界" + 展示 `message` 原文（**不**当普通校验错） |
| 重复注册 | 409 | 用户名/邮箱字段级提示 |
| 未认证 | 401 | 静默刷新，失败才跳登录 |
| 无权限 | 403 | 页内 403 卡，不跳转 |
| 限流 | 429 | Toast 倒计时（读 `Retry-After`） |
| worker 不可用（Phase-2） | 503 | 任务卡显示"执行器繁忙，已排队"，可手动重试 |
| 其他 5xx | 500 | 通用兜底 + "复制诊断信息"按钮（含 traceId） |

> 注意：当前后端把"越界"与"字段校验"都映射为 `unprocessable_entity`，前端**必须按 `message` 区分**，并已记入验收报告缺口清单，Phase-2 后端细分错误码后此处同步简化。

## 10. 性能预算

| 指标 | 目标 |
|---|---|
| 首屏 JS（gzip） | < 180 KB |
| LCP（本地部署） | < 1.2s |
| 路由切换 | < 100ms（路由级 `lazy` 分包） |
| 列表 500 条滚动 | 55fps+（虚拟滚动 `@tanstack/react-virtual`） |

手段：路由级代码分割、`shadcn` 按需引入、图标按名导入、图片不发（纯图标 UI）、避免 barrel 文件导致 tree-shaking 失效（**每个 feature 的 index.ts 只导出公开 API**）。

## 11. 工程与交付

```jsonc
// package.json scripts
{
  "dev": "vite",
  "build": "tsc -b && vite build",
  "preview": "vite preview",
  "test": "vitest run",
  "test:watch": "vitest",
  "e2e": "playwright test",
  "lint": "eslint .",
  "typecheck": "tsc --noEmit",
  "gen:api": "openapi-typescript ../docs/openapi.json -o src/shared/api/types.gen.ts"
}
```

- `tsconfig`：`"strict": true, "noUncheckedIndexedAccess": true, "exactOptionalPropertyTypes": true` —— 后两项能提前抓出一大批 `undefined` 事故。
- `vite.config.ts`：`server.proxy` 把 `/api/v1` 代理到 `127.0.0.1:8088`，开发期免 CORS（后端 `SA_CORS_ORIGINS` 留空即可）。
- CI 三关：`typecheck` → `lint` → `test`，全绿才允许合并。
- 部署：`pnpm build` 产物为纯静态，**由 Rust 后端用 `tower-http::ServeDir` 托管** `/`，单进程单端口即完整产品 —— 这正是"单机私有部署"最省心的形态。

## 12. 分阶段实施（每阶段可独立验收）

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| **A1 骨架**（1–2 天） | Vite+React+TS+Tailwind+shadcn 初始化；client.ts（含单飞刷新）；路由与 AppShell | 能登录、能看到 `/auth/me`、刷新页面不掉登录 |
| **A2 主链路**（2–3 天） | 注册→教程 6 步→新建任务→任务详情轨迹 | Playwright 跑通 E2E 主路径 |
| **A3 全功能**（2–3 天） | 记忆三作用域、agents、capabilities、observability | 所有路由无空白页；错误态/空态/加载态齐全 |
| **A4 打磨**（1–2 天） | 命令面板（⌘K）、键盘可达、深浅主题、任务列表虚拟滚动 | Lighthouse a11y ≥ 95；500 条列表不掉帧 |
| **A5 接真执行**（Phase-2 后） | ~~轮询换 SSE~~、worker 状态可视化 | **SSE 已就绪（2026-11-16 实测合规）**，v1 直接连事件流，无需后补 |

## 13. 三个容易被忽略但决定长期成败的点

1. **错误话术集中管理**：`shared/api/errors.ts` 一张映射表，禁止文案散落组件内。改文案只改一处，且可整体本地化。
2. **空态即设计**：新用户第一次登录，9 个页面里 8 个是空的。空态必须给"下一步做什么"的按钮，否则用户会以为坏了。
3. **命令面板（⌘K）**：这是"超级 agents"该有的气质——一个输入框直达任何 agent / 任何任务 / 任何设置。成本不高（`cmdk` 库），惊喜感强。
