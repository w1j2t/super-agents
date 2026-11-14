#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen-openapi.py —— 从 Rust 源码生成 docs/openapi.json（零侵入，不改后端代码）

为什么要这个文件（而不是嘴上说"有 OpenAPI"）：
  前端和第三方 agent 核对接口时，最怕"文档说的和后端跑的不一样"。
  这里不手写契约，而是直接解析两处唯一真源：
    · 路由表   → src/api/mod.rs 的 `.route(...)` / `.nest(...)`
    · 入参出参 → src/api/handlers/*.rs 的 handler 签名（Json<T> / Query<T> / Path<T>）
    · 数据结构 → src/**/*.rs 的 `pub struct` / `pub enum`（含 serde rename_all）
  后端一改，重跑一次，openapi.json 自动跟上；配 --check 可进 CI 防漂移。

与 utoipa 的关系（诚实交代，不冒充）：
  本仓库**没有**引入 utoipa。utoipa 需要给 25 个 handler 逐个加 `#[utoipa::path]`
  与 `ToSchema` derive，属于侵入式改造且会引入大量依赖；当前阶段选择"源码解析生成"，
  取得同样的可核对产物，代价是脚本本身也要跟着源码演进（由 check-contract.py 兜底）。

用法：
  python scripts/gen-openapi.py                # 写出 docs/openapi.json
  python scripts/gen-openapi.py --out x.json
  python scripts/gen-openapi.py --check        # 只比对，不写盘；有差异退出码 1

已知边界（脚本打印 WARN，绝不瞎猜）：
  · 只处理"纯单元变体"的 enum；带数据变体跳过并告警；
  · 错误响应统一建模为 components.schemas.Problem，具体状态码见 docs/10-verification-report.md；
  · 返回 impl IntoResponse / Response 的端点（healthz、metrics）只登记路径与方法，不建模响应体。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
SRC = ROOT / "src"

WARN: list[str] = []
# 未导出到契约的类型（未被任何接口引用，属内部实现细节）
DROPPED: list[str] = []


def _load_ts_gen():
    """复用 gen-ts-types.py 里的解析工具（block / split_top / preceding_attrs / camel_to）。"""
    spec = importlib.util.spec_from_file_location("gts", HERE / "gen-ts-types.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gts = _load_ts_gen()

# 只把这些目录里的类型当作对外契约；内部结构（store/metrics/executor）不进 schema
ONLY_DIRS = ("domain", "handlers", "security", "services", "obs")
# src 根目录下同样承载公开契约的文件（部件视图 ComponentInfo 就定义在 ports.rs）
EXTRA_FILES = ("ports.rs",)
# 内部实现结构（不是对外契约）。可插拔部件的实现类必须在这里登记，
# 否则生成器会把 File/AtomicBool/VecDeque 这类内部字段当成 API schema，污染契约。
SKIP_STRUCTS = {"Claims", "Metrics", "Store", "Db", "AuditSink", "MetricsSnapshot", "Bucket", "Hist", "RateLimiter",
                "AuthService", "Config",
                "MemoryAuditSink", "FileAuditSink", "AuditKind"}


# ---------------------------------------------------------------- 基础解析
def balanced(text: str, i: int, open_ch: str = "(", close_ch: str = ")") -> int:
    """从 text[i] 处的开括号起，返回到匹配闭括号的下标。"""
    depth = 0
    while i < len(text):
        c = text[i]
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def parse_routes() -> list[tuple[str, str, str, str]]:
    """→ [(full_path, METHOD, module, handler_fn)]"""
    text = (SRC / "api" / "mod.rs").read_text(encoding="utf-8")
    start = text.find("pub fn router(")
    body = text[start:]

    # 切出 v1 子路由块与前缀
    m_v1 = re.search(r"let\s+(\w+)\s*=\s*Router::new\(\)", body)
    prefix_of: dict[str, str] = {}
    v1_span = (-1, -1)
    if m_v1:
        v1_name = m_v1.group(1)
        v1_start = m_v1.end()
        # v1 块结束于根 Router::new()
        m_root = re.search(r"Router::new\(\)\s*\n", body[v1_start:])
        v1_end = v1_start + (m_root.start() if m_root else len(body) - v1_start)
        v1_span = (v1_start, v1_end)
        for m in re.finditer(r'\.nest\(\s*"([^"]+)"\s*,\s*(\w+)\s*\)', body):
            prefix_of[m.group(2)] = m.group(1)
        prefix_of.setdefault(v1_name, "")
        prefix_of[v1_name] = _nest_prefix_for(body, v1_name)

    def collect(seg: str, base: str):
        out = []
        for m in re.finditer(r"\.route\s*\(", seg):
            lp = m.end() - 1
            rp = balanced(seg, lp)
            args = seg[lp + 1 : rp]
            parts = gts.split_top(args)
            if len(parts) < 2:
                continue
            pm = re.search(r'"([^"]+)"', parts[0])
            if not pm:
                continue
            path = pm.group(1)
            chain = ",".join(parts[1:])
            for hm in re.finditer(r"\b(get|post|put|patch|delete|head|options)\s*\(\s*(?:handlers::)?(\w+)::(\w+)\s*\)", chain):
                method = hm.group(1).upper()
                out.append((base + path, method, hm.group(2), hm.group(3)))
        return out

    routes = []
    if v1_span[0] >= 0:
        routes += collect(body[v1_span[0] : v1_span[1]], prefix_of.get(m_v1.group(1), ""))
    rest = body[: v1_span[0]] + body[v1_span[1] :] if v1_span[0] >= 0 else body
    routes += collect(rest, "")
    return routes


def _nest_prefix_for(body: str, router_var: str) -> str:
    for m in re.finditer(r'\.nest\(\s*"([^"]+)"\s*,\s*(\w+)\s*\)', body):
        if m.group(2) == router_var:
            return m.group(1)
    return ""


def parse_handlers() -> dict[str, dict]:
    """→ {(module, handler_fn): {...}}；用模块+函数做键，避免 memory::list 与 task::list 互相覆盖"""
    out: dict[str, dict] = {}
    for p in sorted((SRC / "api" / "handlers").glob("*.rs")):
        text = p.read_text(encoding="utf-8")
        for m in re.finditer(r"pub async fn (\w+)\s*\(", text):
            lp = m.end() - 1
            rp = balanced(text, lp)
            args = text[lp + 1 : rp]
            brace = text.find("{", rp)
            ret = text[rp + 1 : brace]
            rbrace = balanced(text, brace, "{", "}")
            fbody = text[brace : rbrace + 1]
            info = {
                "module": p.stem,
                "json": _first_generic(args, "Json"),
                "query": _first_generic(args, "Query"),
                "path_param": _first_generic(args, "Path"),
                "auth": bool(re.search(r"\bAuthUser\b", args)),
                "metrics_auth": bool(re.search(r"\bMetricsAuth\b", args)),
                "ret": re.sub(r"\s+", " ", ret).strip(),
                "body": fbody,
            }
            out[(p.stem, m.group(1))] = info
    return out


def _first_generic(args: str, wrapper: str) -> str | None:
    m = re.search(wrapper + r"<", args)
    if not m:
        return None
    i = m.end() - 1
    j = balanced(args, i, "<", ">")
    return re.sub(r"\s+", "", args[i + 1 : j])


# ---------------------------------------------------------------- schema 建模
def parse_schemas() -> dict[str, dict]:
    """→ {Name: {"kind":"object","fields":[(name, rust_type, required)]} | {"kind":"enum","values":[...]}}"""
    reg: dict[str, dict] = {}
    for p in sorted(SRC.rglob("*.rs")):
        if not any(part in ONLY_DIRS for part in p.parts) and p.name not in EXTRA_FILES:
            continue
        rel = p.relative_to(ROOT).as_posix()
        txt = p.read_text(encoding="utf-8", errors="replace")

        for m in re.finditer(r"pub enum (\w+)\s*\{", txt):
            name = m.group(1)
            if name in SKIP_STRUCTS:
                continue
            body, _ = gts.block(txt, m.end())
            attrs = gts.preceding_attrs(txt, m.start())
            style = "snake_case"
            ms = re.search(r'rename_all\s*=\s*"([^"]+)"', attrs)
            if ms:
                style = ms.group(1)
            values, ok = [], True
            for v in gts.split_top(body):
                v = re.sub(r"#\[[^\]]*\]", "", v)
                v = "\n".join(ln for ln in v.splitlines() if not ln.strip().startswith("//")).strip()
                if not v:
                    continue
                mm = re.match(r"^(\w+)\s*$", v)
                if not mm:
                    ok = False
                    break
                values.append(gts.camel_to(mm.group(1), style))
            if not ok or not values:
                if not ok:
                    WARN.append(f"{rel}: enum {name} 带数据变体，未纳入 schema")
                continue
            reg[name] = {"kind": "enum", "values": values}

        for m in re.finditer(r"pub struct (\w+)\s*(?:<[^>]*>)?\s*\{", txt):
            name = m.group(1)
            if name in SKIP_STRUCTS:
                continue
            body, _ = gts.block(txt, m.end())
            fields = []
            for seg in gts.split_top(body):
                raw = seg.strip()
                if not raw:
                    continue
                ma = re.match(r"^((?:#\[[^\]]*\]\s*)*)(.*)$", raw, re.S)
                attrs_part, rest = (ma.group(1), ma.group(2)) if ma else ("", raw)
                rest = "\n".join(ln for ln in rest.splitlines() if not ln.strip().startswith("//")).strip()
                # 属性后面可能还跟着"文档注释 + 属性"的组合（例如 /// 说明 换行 #[serde(default)]），
                # 这里把残留的属性前缀一并剥掉，否则该字段会被静默跳过 → 契约漏字段。
                rest = re.sub(r"^(?:#\[[^\]]*\]\s*)+", "", rest, flags=re.S).strip()
                mm = re.match(r"^(?:pub(?:\([^)]*\))?\s+)?(\w+)\s*:\s*(.+)$", rest, re.S)
                if not mm:
                    continue
                fname, ftype = mm.group(1), re.sub(r"\s+", " ", mm.group(2)).strip()
                required = not ftype.startswith("Option<") and "skip_serializing_if" not in attrs_part
                # `#[serde(flatten)]` 的字段在 JSON 里**不占独立键名**：它内部的所有字段
                # 会被平铺到父对象上。契约若照源码字面写成 `{"task": {...}, "trace": [...]}`，
                # 前端就会去读 `data.task.goal`，而真实响应是 `data.goal` —— 拿到 undefined
                # 且不报错，是排查成本最高的一类契约偏差（实测 GET /tasks/{id} 就踩过）。
                # 所以这里必须把 flatten 标记带出去，由 build_component 做内联展开。
                flatten = bool(re.search(r"serde\s*\([^)]*flatten", attrs_part))
                fields.append((fname, ftype, required, flatten))
            if fields:
                reg[name] = {"kind": "object", "fields": fields, "rel": rel}
    return reg


PRIM_INT = {
    "u8": (0, 255), "u16": (0, 65535), "u32": (0, 4294967295),
    "usize": (0, None), "i8": (None, None), "i16": (None, None),
    "i32": (None, None), "i64": (None, None),
}


def js(t: str, reg: dict[str, dict]) -> dict:
    """Rust 类型 → JSON Schema 片段（3.1）。"""
    t = t.strip()
    t = re.sub(r"^&\s*(?:'\w+\s*)?", "", t)
    t = re.sub(r"^'\w+\s+", "", t)

    m = re.match(r"^Option<(.*)>$", t)
    if m:
        return {"anyOf": [js(m.group(1), reg), {"type": "null"}]}
    m = re.match(r"^Vec<(.*)>$", t)
    if m:
        return {"type": "array", "items": js(m.group(1), reg)}
    m = re.match(r"^\[(.*)\]$", t)
    if m:
        return {"type": "array", "items": js(m.group(1), reg)}
    m = re.match(r"^(?:HashMap|BTreeMap)<(.*)>$", t)
    if m:
        kv = gts.split_top(m.group(1))
        if len(kv) == 2:
            return {"type": "object", "additionalProperties": js(kv[1], reg)}
        return {"type": "object"}
    m = re.match(r"^(?:Arc|Rc|Box|Mutex|RwLock|RefCell)<(.*)>$", t)
    if m:
        return js(m.group(1), reg)
    # 元组 → 定长数组（serde 序列化为 JSON 数组）
    if t.startswith("(") and t.endswith(")"):
        parts = [x for x in gts.split_top(t[1:-1]) if x.strip()]
        return {"type": "array", "prefixItems": [js(x, reg) for x in parts],
                "minItems": len(parts), "maxItems": len(parts)}

    if t.startswith("DateTime<") or t.startswith("NaiveDateTime"):
        return {"type": "string", "format": "date-time"}
    if t in ("String", "str", "&str"):
        return {"type": "string"}
    if t == "bool":
        return {"type": "boolean"}
    if t in PRIM_INT:
        lo, hi = PRIM_INT[t]
        s: dict = {"type": "integer"}
        if lo is not None:
            s["minimum"] = lo
        if hi is not None:
            s["maximum"] = hi
        return s
    if t in ("u64", "i64", "f64", "f32"):
        if t in ("f64", "f32"):
            return {"type": "number"}
        return {"type": "integer", "format": "int64"}
    if t in ("serde_json::Value", "Value", "JsonValue"):
        return {}
    short = t.split("::")[-1]
    if short in reg:
        return {"$ref": f"#/components/schemas/{short}"}
    if re.match(r"^[A-Za-z_][\w]*$", short) and short[:1].isupper():
        WARN.append(f"未知命名类型 `{short}` → 已退化为 object（可能来自未纳管模块）")
        return {"type": "object"}
    WARN.append(f"未识别类型 `{t}` → 已退化为空 schema")
    return {}


# 字段级契约提示表：Rust 侧为了向前兼容把某些"实际是枚举"的字段声明成 `Option<String>`
# （运行时再 `parse`，非法值走 422），但契约里必须给出取值枚举。
# 否则前端只能当自由字符串处理，既拿不到编辑器补全，也写不出正确的请求体。
# 键为 (结构体名, 字段名)；只覆盖"源码里确实被解析成有限枚举"的字段，不为省事乱加。
FIELD_HINTS: dict[tuple[str, str], dict] = {
    ("CreateTaskInput", "mode"): {
        "type": "string",
        "enum": ["sync", "async"],
        "default": "async",
        "description": (
            "执行模式。`async`（默认）：立即返回 202 与初始快照，后台执行，进度用 "
            "`GET /tasks/{id}/events`（SSE）或 `GET /tasks/{id}` 查询，可用 `POST /tasks/{id}/cancel` 取消；"
            "`sync`：同步执行完成后才返回 201 与终态快照——慢模型单步可达分钟级，"
            "绝大多数 HTTP 客户端会先超时，仅在确定步骤极快（如 sim 后端）时使用。"
        ),
    },
    # 注册用 `username`、登录用 `account`，这不是笔误：登录接受"用户名或邮箱"，
    # 所以字段名故意更宽。不写清楚，前端必有人把 account 填成 email-only。
    ("RegisterInput", "username"): {
        "type": "string",
        "description": "登录名，全局唯一；长度与字符集见 422 的 details。",
    },
    ("LoginInput", "account"): {
        "type": "string",
        "description": "**用户名或邮箱**（注册时填的 `username` 或 `email` 均可）。主字段。",
    },
    # 别名不是"又一个字段"，而是同一个登录名的另一种叫法：前端复用注册表单对象、
    # 或手里只有邮箱时，不该为改个键名去排查一次 401。
    ("LoginInput", "username"): {
        "type": "string",
        "deprecated": True,
        "description": "`account` 的兼容别名（复用注册字段名时可直接传）。三者取第一个非空值。",
    },
    ("LoginInput", "email"): {
        "type": "string",
        "deprecated": True,
        "description": "`account` 的兼容别名（只有邮箱时可直接传）。三者取第一个非空值。",
    },
}

# 需要从源码枚举里"现取"的字段提示：值为回调，入参是 schema 注册表。
# 好处是枚举成员永远跟着源码走，不会因为手抄枚举而漂移。
FIELD_HINTS_DYNAMIC = {
    ("CreateTaskInput", "task_type"): (
        "TaskType",
        "任务类型；缺省时按 goal 文本的关键词打分判定，打不到任何信号**不会猜**——"
        "直接 422 并在 message 里说明补救方式（显式指定本字段，或把 goal 写成「产出物＋领域」）",
    ),
    ("CreateTaskInput", "model_profile"): ("ModelProfile", "模型档位，影响预算与重试策略；缺省 standard。"),
}

# 结构体级的"组合必填"约束：字段各自可选，但彼此之间互斥/必选其一，
# 单靠 `required` 表达不了（写成 required 就等于要求前端把三个别名全填一遍）。
# 键为结构体名，值为若干条"必须同时出现"的字段组，任一组满足即可。
ANYOF_REQUIRED: dict[str, list[list[str]]] = {
    "LoginInput": [
        ["account", "password"],
        ["username", "password"],
        ["email", "password"],
    ],
}

ANYOF_DESC: dict[str, str] = {
    "LoginInput": (
        "登录请求。`account` / `username` / `email` 是同一个登录名的三种叫法，"
        "任给其一即可（服务端按此顺序取第一个非空值），无需重复填写。"
    ),
}

# 端点级错误文案覆盖：同一个状态码在个别端点上不止一种含义，通用文案会说错。
# 键为 (method, 完整路径)，值为 {状态码: 文案}。
ERROR_DESC_OVERRIDE = {
    ("post", "/api/v1/tasks"): {
        "422": "参数校验未通过或请求被业务拒绝（后两种 details 为空，原因在 message 里）。三种来源："
               "① 字段校验失败（details 带字段级原因）；"
               "② 未识别出任务类型——未传 task_type 且 goal 未命中任何类型关键词，后端不猜类型；"
               "③ 超出能力边界——命中越界信号或没有子 agent 能承接该类型，message 带原因与建议",
    },
}

# 返回类型是 `Result<Response, ApiError>` 的端点：状态码与响应体都在 handler 体里手拼，
# 类型正则推导不出来，只能用显式登记表补上（登记的是**已核实的真实形状**，不是猜测）。
# 新增条目时须同时给出：真实返回语句位置 + 为什么导出成这个 schema。
#
# 键格式必须是 `f"{method.lower()} {full}"`，与下方 route_key 逐字一致。
# 历史事故（2026-11-14 复盘）：这里曾写成 tuple `("post", "/api/v1/tasks")`，
# 而判断侧是 `route_key in RESPONSE_SCHEMA_OVERRIDE`（字典的 in 测的是键），
# 字符串永远不等于 tuple → 条件恒为 False，整张登记表**一登记就没生效过**。
# 更糟的是它不会报错：门禁 6 只比对"现算版 vs 仓库版"，两边同时缺 schema
# 反而一比就一致，于是假绿跨了一整轮打磨；门禁 14 那边又把"该状态码无 schema"
# 记成 PASS，两道防线一起失灵。所以这里加两道防再犯：键格式启动自检 + 门禁 14 补判。
RESPONSE_SCHEMA_OVERRIDE = {
    # task.rs::create → task_response(): (StatusCode::CREATED|ACCEPTED, headers, Json(detail_of(task)))
    "post /api/v1/tasks": "TaskDetail",
}


def _assert_override_keys(reg: dict) -> None:
    """登记表键格式自检：宁可启动即报错，也不要静默降级成"没登记"。

    校验两件事：键是字符串、形如 `小写方法 空格 斜杠开头路径`。
    tuple / 大写方法 / 缺空格这些写法都会被拒，因为它们 100% 匹配不上 route_key。
    """
    bad = []
    for k in reg:
        if not isinstance(k, str):
            bad.append((k, "键不是字符串（曾经写成 tuple 就是这类）"))
            continue
        parts = k.split(" ")
        if len(parts) != 2 or not parts[1].startswith("/") or parts[0] != parts[0].lower():
            bad.append((k, "应形如 'post /api/v1/tasks'"))
    if bad:
        # 退出码 2 专用于"脚本自身配置错"，与 1（生成结果有差异）区分开：
        # 同一码两种因，排查时会被引向错误方向——假绿事故就是这么来的。
        print(f"[FATAL] RESPONSE_SCHEMA_OVERRIDE 键格式不合法，会被静默忽略：{bad}", flush=True)
        raise SystemExit(2)


def _nullable_enum_hint(reg: dict[str, dict], enum_name: str, desc: str) -> dict | None:
    """源码里写成 `Option<String>`、实为枚举的字段：产出 `string|null` 的可空枚举。"""
    spec = reg.get(enum_name)
    if not spec or spec["kind"] != "enum":
        return None
    return {"anyOf": [{"type": "string", "enum": spec["values"]}, {"type": "null"}], "description": desc}


def build_component(name: str, spec: dict, reg: dict[str, dict], _stack: set | None = None) -> dict:
    if spec["kind"] == "enum":
        return {"type": "string", "enum": spec["values"]}
    _stack = set() if _stack is None else _stack
    if name in _stack:  # 防御：自引用 flatten 会无限展开，兜底成空对象而不是崩掉生成器
        WARN.append(f"{name}: #[serde(flatten)] 出现自引用，已截断")
        return {"type": "object"}
    _stack = _stack | {name}

    props: dict = {}
    required: list = []
    for fname, ftype, req, flat in spec["fields"]:
        if flat:
            # flatten：把目标类型的 properties / required 直接并进父对象，
            # 这样契约里的形状与真实响应 JSON 逐键对齐（而非多一层嵌套）。
            inner = re.sub(r"^Option<(.*)>$", r"\1", ftype).strip()
            inner = re.sub(r"^(?:Arc|Rc|Box)<(.*)>$", r"\1", inner).strip()
            sub_spec = reg.get(inner)
            if sub_spec and sub_spec["kind"] == "object":
                sub = build_component(inner, sub_spec, reg, _stack)
                props.update(sub.get("properties", {}))
                for r in sub.get("required", []):
                    if r not in required:
                        required.append(r)
            else:
                WARN.append(
                    f"{name}.{fname}: #[serde(flatten)] 的目标类型 {inner} 未建模，契约会缺这片字段"
                )
            continue
        hint = FIELD_HINTS.get((name, fname))
        if hint is None:
            dyn = FIELD_HINTS_DYNAMIC.get((name, fname))
            if dyn is not None:
                hint = _nullable_enum_hint(reg, dyn[0], dyn[1])
        if hint is not None:
            props[fname] = dict(hint)
        else:
            props[fname] = js(ftype, reg)
        if req:
            required.append(fname)
    out = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    combo = ANYOF_REQUIRED.get(name)
    if combo:
        out["anyOf"] = [{"required": list(g)} for g in combo]
        if name in ANYOF_DESC:
            out["description"] = ANYOF_DESC[name]
    return out


# ---------------------------------------------------------------- 主流程
def op_id(method: str, path: str) -> str:
    words = [w for w in re.split(r"[/{}\-_]+", path) if w and w != "api" and not re.match(r"^v\d$", w)]
    camel = words[0] + "".join(w.capitalize() for w in words[1:]) if words else "root"
    return method.lower() + camel[0].upper() + camel[1:]


def build() -> dict:
    # 第一件事就是验登记表键格式：这张表失效过一次且毫无声息，不能靠人眼复查。
    _assert_override_keys(RESPONSE_SCHEMA_OVERRIDE)
    routes = parse_routes()
    handlers = parse_handlers()
    reg = parse_schemas()

    paths: dict[str, dict] = {}
    used_ops: set[str] = set()
    for full, method, module, fn in routes:
        h = handlers.get((module, fn))
        if h is None:
            WARN.append(f"路由 {method} {full} 找不到 handler 源码（{module}::{fn}）")
            continue
        oid = op_id(method, full)
        while oid in used_ops:
            oid += "_x"
        used_ops.add(oid)

        op: dict = {
            "tags": [full.strip("/").split("/")[0] if not full.startswith("/api/") else full.split("/")[3]],
            "operationId": oid,
            "summary": f"{module}::{fn}",
        }

        # 路径参数
        params = []
        for pname in re.findall(r"\{(\w+)\}", full):
            params.append({
                "name": pname, "in": "path", "required": True,
                "schema": {"type": "string"},
            })
        # 查询参数
        if h["query"]:
            qname = h["query"].split("::")[-1]
            qspec = reg.get(qname)
            if qspec and qspec["kind"] == "object":
                for fname, ftype, req, _flat in qspec["fields"]:
                    params.append({
                        "name": fname, "in": "query", "required": req,
                        "schema": js(ftype, reg),
                    })
            else:
                WARN.append(f"{fn}: Query<{qname}> 未在 schema 中建模，参数未展开")
        if params:
            op["parameters"] = sorted(params, key=lambda x: (x["in"], x["name"]))

        # 请求体
        if h["json"]:
            sch = js(h["json"], reg)
            op["requestBody"] = {
                "required": True,
                "content": {"application/json": {"schema": sch}},
            }

        # 安全
        if h["metrics_auth"]:
            op["security"] = [{"metricsToken": []}]
            op["description"] = "静态令牌鉴权：请求头 Authorization: Bearer $SA_METRICS_TOKEN（长度 ≥ 24）"
        elif h["auth"]:
            op["security"] = [{"bearerAuth": []}]
        else:
            op["security"] = []

        route_key = f"{method.lower()} {full}"
        # 分流型接口必须在契约里把"两条路怎么走"讲清。只写一句"提交任务"，
        # 前端极可能按同步实现并配 30s 超时，长任务必然被判超时失败（实测踩过）。
        if route_key == "post /api/v1/tasks":
            op["description"] = (
                "提交任务。**默认 `mode=async`**：立即返回 202 与初始快照，后台执行，"
                "进度走 `GET /tasks/{id}/events`（SSE，支持 `Last-Event-ID` 断点续传）"
                "或轮询 `GET /tasks/{id}`，中途可用 `POST /tasks/{id}/cancel` 取消。"
                "`mode=sync` 才会同步执行完成后返回 201 与终态快照——耗时随步骤数与模型耗时增长"
                "（慢模型单步可达两分钟以上），客户端超时必须设长，不建议常规使用。"
                "两种模式的响应体结构完全一致（平铺的 TaskDetail），差异只在 `status` 是否已终态。"
            )

        # 响应
        ret = h["ret"]
        out_schema = None
        # 一个 handler 可能按入参分支返回**不同的成功状态码**（POST /tasks：sync→201、async→202）。
        # 早先只认单一状态码，另一条分支在契约里根本不存在，前端照着契约写就永远等不到 202。
        # 这里从 handler 函数体里把真实出现的成功状态码全部收集起来，而不是只看第一个。
        SUCCESS_TOKENS = [
            ("StatusCode::CREATED", "201"),
            ("StatusCode::ACCEPTED", "202"),
            ("StatusCode::OK", "200"),
        ]
        status_list = [code for tok, code in SUCCESS_TOKENS if tok in h["body"]]
        # SSE 端点：返回 text/event-stream。若不单独识别，契约里会写成"200 + 空 content"，
        # 前端照契约根本不知道该按什么读。这里把帧形状、事件名、心跳、终止条件一并写进契约。
        is_sse = "Sse<" in ret
        if not status_list:
            status_list = ["200"]
        status = status_list[0]
        m = re.search(r"Result<\s*\(?\s*(?:StatusCode\s*,\s*)?Json<(.+?)>\s*\)?\s*,\s*ApiError\s*>", ret)
        if m:
            out_schema = js(m.group(1), reg)
        elif re.search(r"Result<\s*StatusCode\s*,\s*ApiError\s*>", ret):
            status_list = ["204"]
            status = "204"
        elif is_sse:
            out_schema = {"$ref": "#/components/schemas/TaskEvent"}
            op["x-stream"] = {
                "type": "sse",
                "event": "status",
                "frameSchema": "TaskEvent",
                "heartbeatSeconds": 15,
                "snapshotSeq": 0,
                "endsOnTerminalStatus": True,
                # 断点续传已实现（api/handlers/task_events.rs：带 Last-Event-ID 从环形台账
                # 补发 seq > 锚点的帧，不重推 seq=0 快照；非法值退回首连；补发为空且任务
                # 已终态时立即 EOF；锚点超前同理快速 EOF；台账已滚出窗口则退化为快照重发
                # 并计 sa_sse_replay_gap_total）。早先这里写 False 是"未实现"时代的记录，
                # 会让前端照着契约放弃续传，已订正。
                "reconnectHeaderLastEventId": True,
                "reconnectHeaderName": "Last-Event-ID",
                "reconnectReplayMetric": "sa_sse_replay_total",
                "reconnectGapMetric": "sa_sse_replay_gap_total",
            }
            op["description"] = (
                "Server-Sent Events 长连接：一律 `event: status`，`data` 为 TaskEvent JSON，"
                "每帧带 `id: <seq>`。首帧是连接瞬间的快照（`seq = 0`）；任务进入终态后服务端主动结束流（EOF）；"
                "空闲时每 15s 一条心跳。"
                "**断点续传**：断线重连时带请求头 `Last-Event-ID: <最后收到的 seq>`，"
                "服务端只补发该锚点之后的帧（`seq` 严格大于锚点），不重推快照帧；"
                "锚点非法（非数字/负数）按首次连接处理、退回首帧快照；"
                "补发为空且任务已终态时立即 EOF，不会挂住连接。"
                "唯一退化场景：中间帧已滚出服务端环形台账，此时退化为重发快照并累加 "
                "`sa_sse_replay_gap_total`，前端应据 `seq` 跳变回查 `GET /tasks/{id}` 补齐。"
            )
        elif route_key in RESPONSE_SCHEMA_OVERRIDE:
            # 少数 handler 的返回类型是 `Result<Response, ApiError>`（自己拼状态码 + 头 + JSON），
            # 正则认不出 JSON 实体。POST /api/v1/tasks 就是这种：sync 分支在 task_response() 里
            # 拼 `(StatusCode::CREATED, headers, Json(detail_of(task)))`，async 分支拼 202，
            # 两者响应体都是平铺的 TaskDetail。若不显式登记，契约会写成"201/202 无响应体"——
            # 前端据此就会以为提交拿不到任务快照，只能再打一次 GET。
            name = RESPONSE_SCHEMA_OVERRIDE[route_key]
            out_schema = {"$ref": f"#/components/schemas/{name}"}
        else:
            m2 = re.search(r"Json<(.+?)>\s*$", ret)
            if m2:
                out_schema = js(m2.group(1), reg)
            elif "IntoResponse" in ret or ret == "Response":
                op["description"] = (op.get("description", "") + " 响应体非 JSON（健康检查 / Prometheus 文本）。").strip()

        # 多成功码时逐码给出**可区分的**说明：契约里两个 2xx 都写"成功"等于没写，
        # 前端仍然不知道什么条件下会拿到 202。
        MULTI_STATUS_DESC = {
            "201": "同步执行完成（`mode=sync`），返回终态快照",
            "202": "已受理，后台执行（`mode=async`）；用 `GET /tasks/{id}/events` 看直播，可 `POST /tasks/{id}/cancel` 取消",
            "200": "成功",
        }
        responses: dict = {}
        for st in status_list:
            if st == "204":
                responses[st] = {"description": "成功，无响应体"}
            elif is_sse:
                # SSE 的媒体类型不是 application/json，写错前端会按 JSON 解析直接报错
                responses[st] = {
                    "description": "事件流（SSE），终态后 EOF",
                    "content": {"text/event-stream": {"schema": out_schema}},
                }
            else:
                content = {"application/json": {"schema": out_schema}} if out_schema else {}
                desc = MULTI_STATUS_DESC.get(st, "成功") if len(status_list) > 1 else "成功"
                responses[st] = {"description": desc, "content": content}
        # 错误码按操作的**真实行为**挂载，不再"每个接口都挂七个"。
        # 挂错比不挂更坏：把 429 挂到 /healthz 上，前端就会对着健康检查写退避逻辑。
        limited = {
            "post /api/v1/tasks": "任务提交配额耗尽（默认 60 次/小时/用户），或 async 后台队列已满（并发上限）",
            "post /api/v1/auth/register": "注册配额耗尽（10 次/小时/IP）",
            "post /api/v1/auth/login": "登录配额耗尽（10 次/分钟）",
            "post /api/v1/auth/refresh": "刷新配额耗尽（30 次/分钟）",
        }
        errors: list = []
        if full not in ("/healthz", "/readyz"):  # 健康检查没有入参，也没有实体
            errors.append(("400", "请求格式错误"))
            if op.get("security") != []:
                errors += [("401", "未认证或令牌无效"), ("403", "权限不足")]
            if route_key in ("post /api/v1/auth/login", "post /api/v1/auth/refresh"):
                errors.append(("401", "账号或密码错误 / 刷新令牌失效"))
            if "{" in full:
                errors.append(("404", "资源不存在"))
            if route_key == "post /api/v1/auth/register":
                errors.append(("409", "冲突（用户名或邮箱已占用）"))
            if route_key == "post /api/v1/tasks/{id}/cancel":
                errors.append(("409", "任务已处于终态，无法取消"))
            if route_key in limited:
                errors.append(("429", limited[route_key] + "，带 Retry-After"))
            errors.append(("422", "参数校验未通过（details 带字段级原因）"))
            # 少数端点的同一个状态码不止一种含义，通用文案会把它们说错。
            # POST /tasks 的 422 有三个真实来源：字段校验失败 / 未识别出任务类型 /
            # 超出能力边界（后两者 details 为空、原因在 message 里）。前端要靠这段决定
            # "提示用户补什么"，写窄了等于让人对着无效提示反复改 goal。
            for code, desc in ERROR_DESC_OVERRIDE.get((method.lower(), full), {}).items():
                errors = [(c, desc if c == code else d) for (c, d) in errors]
        seen: set = set()
        for code, desc in errors:
            if code in seen:
                continue
            seen.add(code)
            responses[code] = {
                "description": desc,
                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Problem"}}},
            }
        op["responses"] = responses
        paths.setdefault(full, {})[method.lower()] = op

    # Problem 是所有错误响应的统一形状（src/error.rs::ProblemBody）
    problem = {
        "type": "object",
        "required": ["code", "message"],
        "properties": {
            "code": {"type": "string", "description": "错误码，如 unauthorized / conflict / unprocessable_entity"},
            "message": {"type": "string"},
            "details": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "字段级校验明细，仅 422 等场景出现",
            },
        },
    }

    # ---- 只导出被接口真正引用到的 schema ----
    # 早先的做法是"全量导出 + 手抄黑名单 SKIP_STRUCTS"，属于治标：源码里新增一个内部结构体
    # （比如指标导出器 JsonExporter / NoopExporter），它会悄无声息地混进公开契约，
    # 前端照着生成类型就被误导了。现在从 paths 出发做 $ref 可达性分析，
    # 没被任何接口引用到的类型一律不导出，新加实现类也不会再污染契约。
    def collect_refs(node, out: set) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                out.add(ref.rsplit("/", 1)[-1])
            for v in node.values():
                collect_refs(v, out)
        elif isinstance(node, list):
            for v in node:
                collect_refs(v, out)

    reachable: set = set()
    collect_refs(paths, reachable)
    built: dict[str, dict] = {}
    pending = list(reachable)
    while pending:
        name = pending.pop()
        if name in built or name not in reg:
            continue
        comp = build_component(name, reg[name], reg)
        built[name] = comp
        found: set = set()
        collect_refs(comp, found)
        pending.extend(n for n in found if n not in built)
    for name in sorted(set(reg) - set(built)):
        DROPPED.append(name)

    schemas = {"Problem": problem}
    for name in sorted(built):
        schemas[name] = built[name]

    doc = {
        "openapi": "3.1.0",
        "info": {
            "title": "Personal Super Agents Backend API",
            "version": "1.0.0",
            "description": (
                "本文件由 scripts/gen-openapi.py 从 Rust 源码自动生成，请勿手工编辑。\n"
                "真源：src/api/mod.rs（路由）、src/api/handlers/*.rs（入参出参）、src/**/*.rs（数据结构）。\n"
                "注意：本仓库未使用 utoipa；错误响应统一为 Problem JSON，具体状态码语义见 docs/10-verification-report.md。"
            ),
        },
        "servers": [{"url": "http://127.0.0.1:8088", "description": "本地默认监听 127.0.0.1:8088（src/config.rs:50，可用 SA_BIND 覆盖）"}],
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT",
                               "description": "POST /api/v1/auth/login 取得的 access_token，默认有效期 1800 秒"},
                "metricsToken": {"type": "http", "scheme": "bearer",
                                 "description": "静态指标令牌 SA_METRICS_TOKEN（≥24 字符）"},
            },
            "schemas": schemas,
        },
        "paths": {k: paths[k] for k in sorted(paths)},
    }
    return doc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--out", default="docs/openapi.json")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    if not SRC.exists():
        print(f"[FAIL] 找不到 {SRC}")
        return 1

    doc = build()
    text = json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    out_path = pathlib.Path(a.root) / a.out

    n_paths = len(doc["paths"])
    n_ops = sum(len(v) for v in doc["paths"].values())
    n_schemas = len(doc["components"]["schemas"])

    if a.check:
        old = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
        same = old.strip() == text.strip()
        print(f"CHECK: {'一致' if same else '有差异（请重新生成）'} | paths={n_paths} operations={n_ops} schemas={n_schemas}")
        return 0 if same else 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    print(f"已生成 {out_path}")
    print(f"paths {n_paths} 条 | operations {n_ops} 个 | schemas {n_schemas} 个 | 行数 {len(text.splitlines())}")
    if DROPPED:
        print(f"\n未导出（未被任何接口引用，属内部实现）：{len(DROPPED)} 个 -> {', '.join(DROPPED)}")
    if WARN:
        print("\nWARN（未建模项，不猜测）：")
        for w in sorted(set(WARN)):
            print("  -", w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
