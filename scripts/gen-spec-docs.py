#!/usr/bin/env python3
"""从 Rust 源码生成规范文档的"事实部分"：02 能力清单、03 分工协作。

设计原则与 gen-ts-types.py 一致：**文档不手抄，直接读源码**。
一旦 capability.rs / agent.rs 改了而文档没重新生成，`--check` 会失败。

用法：
    python scripts/gen-spec-docs.py            # 生成/覆盖 docs/02-*.md docs/03-*.md
    python scripts/gen-spec-docs.py --check    # 只校验是否漂移，漂移则退出码 1
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
DOCS = ROOT / "docs"

STR = r'"((?:[^"\\]|\\.)*)"'
ARR = r'&\[((?:"(?:[^"\\]|\\.)*"\s*,?\s*)+)\]'

HEADER = (
    "> 本文件的事实部分由 `scripts/gen-spec-docs.py` 从源码生成，**请勿手改**。\n"
    "> 改完 `src/domain/*.rs` 后执行 `python scripts/gen-spec-docs.py`；\n"
    "> CI 或提交前用 `python scripts/gen-spec-docs.py --check` 校验是否漂移。\n"
)


def unescape(s: str) -> str:
    return s.replace('\\"', '"').replace("\\n", " ").replace("\\\\", "\\")


def blocks(text: str, marker: str, anchor: str) -> list[str]:
    """按 `Marker {` ... `\n    },` 切出每个字面量块。

    anchor 用于跳过 struct 定义区：只在 `anchor` 之后的文本里切块，否则
    `pub struct X { ... }` 会被当成一条数据，解析出 "AgentKind" 之类的假条目。
    """
    pos = text.find(anchor)
    if pos >= 0:
        text = text[pos:]
    out = []
    for m in re.finditer(re.escape(marker) + r" \{", text):
        start = m.end()
        end = text.find("\n    },", start)
        if end < 0:
            continue
        body = text[start:end]
        if re.search(r'\bid:\s*"', body):
            out.append(body)
    return out


def s1(body: str, field: str) -> str:
    m = re.search(rf'\b{field}:\s*{STR}', body)
    return unescape(m.group(1)) if m else ""


def arr(body: str, field: str) -> list[str]:
    m = re.search(rf'\b{field}:\s*{ARR}', body, re.S)
    if not m:
        return []
    return [unescape(x) for x in re.findall(STR, m.group(1))]


def const_map(text: str) -> dict[str, list[str]]:
    """解析 `const NAME: &[T] = &[...];`，用于展开 ALL_TASK_TYPES / WORKER 等别名。"""
    out: dict[str, list[str]] = {}
    for m in re.finditer(r"const (\w+): &\[(\w+)\] = &\[(.*?)\];", text, re.S):
        name, _ty, body = m.group(1), m.group(2), m.group(3)
        out[name] = re.findall(r"(?:\w+::)?(\w+)", body)
    return out


def enum_tail(body: str, field: str, table: dict[str, list[str]]) -> list[str]:
    """取出 `field: Xxx` / `field: &[A::B, ...]`，展开常量别名。"""
    if not body:
        return []
    m = re.search(rf'\b{field}:\s*&\[(.*?)\]', body, re.S)
    if m:
        return re.findall(r"(?:\w+::)?(\w+)", m.group(1))
    m = re.search(rf'\b{field}:\s*(\w+)\s*,', body)
    if not m:
        return []
    tok = m.group(1)
    return table.get(tok) or [tok.split("::")[-1]]


def ename(body: str, field: str) -> str:
    m = re.search(rf'\b{field}:\s*(?:\w+::)?(\w+)\s*,', body)
    return m.group(1) if m else ""


def num(body: str, field: str) -> str:
    m = re.search(rf'\b{field}:\s*(\d+)', body)
    return m.group(1) if m else ""


def cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


# ---------------------------------------------------------------- 能力清单


def build_capabilities() -> str:
    text = (SRC / "domain/capability.rs").read_text(encoding="utf-8")
    caps = []
    for b in blocks(text, "CapabilitySpec", "pub const CAPABILITIES"):
        caps.append(
            {
                "id": s1(b, "id"),
                "name": s1(b, "name"),
                "category": s1(b, "category"),
                "trigger": s1(b, "trigger"),
                "owners": arr(b, "owner_agents"),
                "boundary": s1(b, "permission_boundary"),
                "signals": arr(b, "observability_signals"),
                "end": s1(b, "degradation_or_abort"),
            }
        )

    lines = [
        f"# 02 能力清单（{len(caps)} 项内置机制）",
        "",
        HEADER,
        "每项能力都必须具备五要素：**触发条件 / 负责 agents / 权限边界 / 可观测信号 / 降级或终止条件**。",
        "缺任一要素即视为规格不完整，`tests/spec_matrix.rs` 会直接判失败。",
        "",
        "## 0. 原文 25 条 → 本表条目（逐条对齐）",
        "",
        "首个提示词点名的 25 条能力，逐条落到下表的具体条目上。**原文哪一条找不到落点，门禁 12 直接红。**",
        "",
        "| 原文序号 | 原文术语 | 本表条目（能力 ID） | 说明 |",
        "| --- | --- | --- | --- |",
        "| 1 | 自动化 | `automation` | — |",
        "| 2 | 懒加载 | `lazy_loading` | — |",
        "| 3 | 全可视化 | `full_visualization` | — |",
        "| 4 | 权力分配 | `power_allocation` | — |",
        "| 5 | 安全 | `security` | 与原文第 19 条「安全与治理」为两件事：本条指原子权限与红线，条 19 指治理体系 |",
        "| 6 | 错误纠错 | `error_correction` | — |",
        "| 7 | 自净化 | `self_purification` | — |",
        "| 8 | 正确性校验 | `correctness_check` | — |",
        "| 9 | 合规检查 | `compliance_check` | — |",
        "| 10 | 对话共享记忆 | `shared_memory` | — |",
        "| 11 | 全链路监控 | `full_chain_monitoring` | — |",
        "| 12 | 按任务模型调度 | `model_scheduling` | 档位 fast / standard / deep |",
        "| 13 | 工具调用 | `tool_invocation` | — |",
        "| 14 | 流程编排 | `orchestration` | — |",
        "| 15 | 循环报错处理 | `loop_error_handling` | 重试上限 + 失败步熔断 `3/3` |",
        "| 16 | 数据飞轮 | `data_flywheel` | — |",
        "| 17 | 评估驱动检查与工具增强（即 RAG） | `evaluation_driven` **+** `rag_tool_augmentation` | **原文合写一条，实现拆为两条**：评估驱动=质量闭环，RAG=检索增强。故条目数 26 而原文 25 |",
        "| 18 | 可观测与告警 | `observability_alerting` | — |",
        "| 19 | 安全与治理 | `security_governance` | 见条 5 说明 |",
        "| 20 | 组织与组合管理 | `org_composition` | — |",
        "| 21 | 成本与算力优化 | `cost_compute_optimization` | — |",
        "| 22 | 上下文优化 | `context_optimization` | — |",
        "| 23 | 按任务 agents 调配 | `agent_dispatching` | — |",
        "| 24 | 防幻觉 | `anti_hallucination` | — |",
        "| 25 | 沙箱 | `sandbox` | — |",
        "",
        "## 一览表",
        "",
        "| 编号 | 能力 ID | 名称 | 分类 | 负责 agents |",
        "| --- | --- | --- | --- | --- |",
    ]
    for i, c in enumerate(caps, 1):
        owners = "、".join(f"`{o}`" for o in c["owners"]) or "—"
        lines.append(f"| C{i:02d} | `{c['id']}` | {cell(c['name'])} | {cell(c['category'])} | {owners} |")

    categories: list[str] = []
    for c in caps:
        if c["category"] not in categories:
            categories.append(c["category"])

    for cat in categories:
        lines += ["", f"## 分类：{cat}", ""]
        for i, c in enumerate([x for x in caps if x["category"] == cat], 1):
            idx = caps.index(c) + 1
            lines += [
                f"### C{idx:02d} {c['name']} `{c['id']}`",
                "",
                f"- **触发条件**：{c['trigger']}",
                f"- **负责 agents**：{'、'.join('`' + o + '`' for o in c['owners']) or '—'}",
                f"- **权限边界**：{c['boundary']}",
                f"- **可观测信号**：{'、'.join('`' + s + '`' for s in c['signals']) or '—'}",
                f"- **降级/终止**：{c['end']}",
                "",
            ]

    lines += [
        "## 覆盖度自查",
        "",
        "| 检查项 | 结果 |",
        "| --- | --- |",
        f"| 能力总数 | {len(caps)} |",
        f"| 分类数 | {len(categories)} |",
        f"| 五要素齐全 | {'是' if all(c['trigger'] and c['owners'] and c['boundary'] and c['signals'] and c['end'] for c in caps) else '否'} |",
        f"| 无 owner 的能力 | {sum(1 for c in caps if not c['owners'])} |",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- 分工协作


def build_agents() -> str:
    text = (SRC / "domain/agent.rs").read_text(encoding="utf-8")
    table = const_map(text)
    agents = []
    for b in blocks(text, "AgentSpec", "pub const AGENTS"):
        agents.append(
            {
                "id": s1(b, "id"),
                "kind": ename(b, "kind"),
                "name": s1(b, "name"),
                "domain": s1(b, "domain"),
                "summary": s1(b, "summary"),
                "vk": arr(b, "vertical_knowledge"),
                "docs": arr(b, "doc_refs"),
                "skills": arr(b, "skills"),
                "perms": enum_tail(b, "permissions", table),
                "sandbox": s1(b, "sandbox_profile"),
                "model": ename(b, "default_model"),
                "types": enum_tail(b, "task_types", table),
                "keywords": arr(b, "keywords"),
                "conc": num(b, "max_concurrency"),
            }
        )

    # worker 名单从 AGENTS 现算，不写死：写死的那一版在增删 agent 后立刻变成假文档，
    # 而 `--check` 又只比对生成结果，发现不了这种"文档自己骗自己"。
    _workers = "/".join(
        a["id"][: -len("-agent")]
        for a in agents
        if a["id"] not in ("super-agent", "guard-agent")
    )

    lines = [
        f"# 03 分工协作（{len(agents)} 个 agent）",
        "",
        HEADER,
        "边界原则：**唯一入口是 super-agent**，子 agent 只做自己域内的事；",
        "跨域需求由 super-agent 拆步后顺序/并行派发，禁止子 agent 越权直连第三方。",
        "",
        "## 0. 原文五项要求 → 本文落点",
        "",
        "首个需求对「分工协作」点名了五件事。逐条给出落点，避免只写了花名册却漏了调度与分权。",
        "",
        "| 原文要求 | 本文落点 | 实现/证据 |",
        "| --- | --- | --- |",
        "| 定义职责 | §2 各自职责 | 每个 agent 的 `summary` / `task_types` / `skills` 均来自 `src/domain/agent.rs` |",
        "| 定义编排 | §4 典型协作链路 | `src/orchestration/executor.rs`：每步过 RBAC → 红线 → 预算 → 执行 → 重试/熔断 |",
        "| 按任务模型调度 | §1「默认模型」列 + §3「计划置信度」 | `ModelProfile` = fast / standard / deep；agent 自带 `default_model`；预算不足按 deep→standard→fast 降档（`docs/05` §4） |",
        "| 按任务 agents 调配 | §3「工人打分 / 派发门槛 / 并发上限」 | `routing.rs`：`MIN_DISPATCH_SCORE = 0.45`（该类型下无 agent 可承接即判越界，不静默降级）；`max_concurrency` 超出排队而非失败 |",
        "| 权力分配 | §5 权限档位 + §1「权限档」列 | 七项原子权限（`security/rbac.rs`）三档分组，权限不可临时提权（`docs/05` §2） |",
        "",
        "## 1. 花名册",
        "",
        "| ID | 名称 | 类型 | 领域 | 默认模型 | 并发 | 权限档 | 沙箱画像 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for a in agents:
        lines.append(
            f"| `{a['id']}` | {cell(a['name'])} | {a['kind']} | {cell(a['domain'])} | "
            f"{a['model']} | {a['conc']} | {'/'.join(a['perms']) or '—'} | `{a['sandbox']}` |"
        )

    lines += ["", "## 2. 各自职责", ""]
    for a in agents:
        lines += [
            f"### `{a['id']}` {a['name']}",
            "",
            f"- **职责**：{a['summary']}",
            f"- **可承接任务类型**：{'、'.join(a['types']) or '—'}（由 `TaskType` 驱动路由）",
            f"- **技能**：{'、'.join('`' + s + '`' for s in a['skills']) or '—'}",
            f"- **垂直知识**：{'、'.join(a['vk']) or '—'}",
            f"- **参考文档**：{'、'.join('`' + d + '`' for d in a['docs']) or '—'}",
            f"- **调度关键词**：{'、'.join(a['keywords']) or '—'}",
            "",
        ]

    lines += [
        "## 3. 调度与打分（源码实况，`src/orchestration/routing.rs`）",
        "",
        "| 环节 | 规则 |",
        "| --- | --- |",
        "| 任务类型识别 | 按 `TASK_KEYWORDS` 命中数排序，取第一名；置信度 `0.55 + 0.45×hits/(hits+1.6)`。**命中数为 0 时不猜**：直接 422，message 给出两条补救（显式指定 `task_type`，或把 goal 写成「产出物＋领域」） |",
        "| 工人打分 | `score = min(1.0, 0.5 + 0.15×min(关键词命中,2) + 0.1×[目标含领域词])` |",
        "| 派发门槛 | `MIN_DISPATCH_SCORE = 0.45`：该任务类型下若没有任何 agent 达标，判为越界并返回 422 说明缺什么能力（**不静默降级**） |",
        "| 计划置信度 | `clamp(0.45×worker_score + 0.35×routing_conf + 0.2, 0, 0.98)` |",
        "| 并发上限 | 每个 agent 的 `max_concurrency`，超出排队而非失败 |",
        "",
        "## 4. 典型协作链路",
        "",
        "```text",
        "用户目标 → super-agent(理解/拆步/选人)",
        f"        → worker agent({_workers})",
        "        → 每步过 guard-agent 合规与权限校验",
        "        → super-agent 收敛结果 + 交付校验 → 落库",
        "```",
        "",
        "单步执行体通过 process runner 拉起，注入的环境变量即交接协议：",
        "",
        "| 变量 | 含义 |",
        "| --- | --- |",
        "| `SA_TASK_ID` / `SA_STEP_INDEX` / `SA_STEP_NAME` / `SA_ATTEMPT` | 步骤身份与重试序号 |",
        "| `SA_AGENT_ID` / `SA_AGENT_NAME` / `SA_PERMISSION` | 执行者与权限档 |",
        "| `SA_GOAL` / `SA_CONTEXT_SUMMARY` / `SA_TASK_TYPE` | 目标、上下文摘要、任务类型 |",
        "| `SA_LLM_BASE` / `SA_LLM_MODEL` / `SA_LLM_API_KEY` / `SA_LLM_TIMEOUT` | 模型接入点（缺省走模拟） |",
        "| `SA_SANDBOX` | 沙箱画像名，与花名册最后一列对应 |",
        "",
        "## 5. 权限档位",
        "",
        "| 档位 | 内容 | 使用方 |",
        "| --- | --- | --- |",
        "| `ORCH_TRUSTED` | Read/Write/SandboxExec/MemoryWriteShared/MemoryWriteSystem/CrossAgent/Admin | super-agent 等编排者 |",
        "| `WORKER` | Read/Write/SandboxExec/MemoryWriteShared/CrossAgent | 普通子 agent |",
        "| `READONLY` | Read | 只读类 agent（如 guard-agent） |",
        "",
        "七项原子权限语义见 `src/security/rbac.rs`：`Read` / `Write` / `SandboxExec` / "
        "`MemoryWriteShared` / `MemoryWriteSystem` / `CrossAgent` / `Admin`。",
        "",
    ]
    return "\n".join(lines)


def build_api_index() -> str:
    """从 docs/openapi.json 生成 07 API 参考索引（路径-方法-摘要-鉴权）。"""
    import json

    spec = json.loads((DOCS / "openapi.json").read_text(encoding="utf-8"))
    paths = spec.get("paths", {})
    rows = []
    for path in sorted(paths):
        for method in ("get", "post", "put", "patch", "delete"):
            op = paths[path].get(method)
            if not op:
                continue
            tags = op.get("tags") or ["—"]
            auth = "否（公开）" if op.get("security") == [] else "是"
            rows.append(
                (
                    method.upper(),
                    path,
                    cell(op.get("summary") or op.get("operationId") or "—"),
                    tags[0],
                    auth,
                )
            )
    schemas = spec.get("components", {}).get("schemas", {})
    methods = {}
    for m, *_ in rows:
        methods[m] = methods.get(m, 0) + 1

    lines = [
        f"# 07 API 参考索引（{len(rows)} 个操作 / {len(schemas)} 个数据结构）",
        "",
        HEADER,
        f"真源是 `docs/openapi.json`（由 `scripts/gen-openapi.py` 从路由与 handler 生成），"
        f"本文件只是它的可读索引。前端 TS 类型用 `scripts/gen-ts-types.py` 从同一份 openapi 生成。",
        "",
        f"- `info.version`：{spec.get('info', {}).get('version', '—')}",
        f"- 服务地址：`{', '.join(s.get('url', '') for s in spec.get('servers', [])) or '—'}`",
        "- 错误响应统一为 **Problem JSON**（`components.schemas.Problem` 及变体），语义见 `docs/10-verification-report.md`",
        "",
        "## 操作清单",
        "",
        "| 方法 | 路径 | 摘要 | 分组 | 需鉴权 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for m, p, s, tag, auth in rows:
        lines.append(f"| {m} | `{p}` | {s} | {tag} | {auth} |")

    lines += [
        "",
        "## 数据结构（components/schemas）",
        "",
        "| # | 名称 |",
        "| --- | --- |",
    ]
    for i, name in enumerate(sorted(schemas), 1):
        lines.append(f"| {i} | `{name}` |")

    lines += [
        "",
        "## 覆盖度自查",
        "",
        "| 检查项 | 结果 |",
        "| --- | --- |",
        f"| 路径数 | {len(paths)} |",
        f"| 操作数 | {len(rows)} |",
        f"| GET / POST / 其它 | {methods.get('GET', 0)} / {methods.get('POST', 0)} / "
        f"{sum(v for k, v in methods.items() if k not in ('GET', 'POST'))} |",
        f"| 数据结构数 | {len(schemas)} |",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    check = "--check" in sys.argv
    DOCS.mkdir(exist_ok=True)
    targets = {
        DOCS / "02-capability-catalog.md": build_capabilities(),
        DOCS / "03-division-of-labor.md": build_agents(),
        DOCS / "07-api-reference.md": build_api_index(),
    }
    drift = []
    for path, content in targets.items():
        content = content.rstrip() + "\n"
        if check:
            old = path.read_text(encoding="utf-8") if path.exists() else ""
            if old.rstrip() + "\n" != content:
                drift.append(str(path.relative_to(ROOT)))
        else:
            path.write_text(content, encoding="utf-8")
            print(f"[gen] {path.relative_to(ROOT)}  {len(content.splitlines())} 行")
    if check:
        if drift:
            print("[drift] 以下文档与源码不一致，请重新生成：")
            for d in drift:
                print("   -", d)
            return 1
        print("[ok] 02/03 与源码一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
