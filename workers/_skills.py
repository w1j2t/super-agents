"""Skill 市场（本地离线版）：把"技能"当成可发现、可校验、可装卸的薄资源。

为什么不做成在线商店
------------------
老板的机器经常离线，且这个项目的脾气是"零依赖、可自证"。所以市场分两层：

  · **发现层**：扫本地 skills 根目录 + 注册表，读每个 skill 的元数据，拼出清单；
  · **装载层**：真正 import/执行只在 `activate()` 时发生，`list/search/info` 一律不执行技能代码。

这条界线很重要：逛市场不该等于把陌生代码跑一遍。所以元数据用**静态解析**（frontmatter + 目录约定），
只有老板明确"我要用它"时才 `activate`。

Skill 目录约定（尽量宽容，三种都认）
-----------------------------------
    skills/<name>/SKILL.md        ← 带 YAML frontmatter（name/description/version/tags/requires）
    skills/<name>/skill.md        ← 同上，大小写不敏感
    skills/<name>/*.py            ← 没有 SKILL.md 时，从模块 docstring 第一行 + 文件名兜底

市场还负责三件容易被忽略的事
--------------------------
1. **冲突检测**：同名不同版本并存时给出提示，避免"到底加载了哪个"说不清；
2. **依赖检查**：`requires` 里的模块不在就标 `missing`，而不是等 activate 时才炸；
3. **评分**：按 有元数据 / 有描述 / 有版本 / 有示例 / 自检通过 五项打分，帮老板一眼看出哪些是"能用的"。

懒加载契约
----------
`SkillMarket(...)` 构造不扫盘、不读文件；`stats()["scanned"]=False` 可自证。
第一次 `refresh()`/`list()` 才扫，且扫到的模块只做 `ast.parse` 静态检查，**不 import**。

用法
----
    python workers/_skills.py --list
    python workers/_skills.py --search 文档
    python workers/_skills.py --info doc
    python workers/_skills.py --selftest
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
import time
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def market_roots() -> list[str]:
    """引入的外部货架：marketplace/<vendor>/skills。

    自建技能与搬来的技能**分开放**，不是为了好看，是为了出问题能一眼分清
    "我写的那条坏了"还是"搬来的那条水土不服"——这两类故障的修法完全不同。
    """
    base = os.path.join(ROOT, "marketplace")
    out: list[str] = []
    if not os.path.isdir(base):
        return out
    for vendor in sorted(os.listdir(base)):
        p = os.path.join(base, vendor, "skills")
        if os.path.isdir(p):
            out.append(p)
    return out


#: 自建在前、外来的在后：同名冲突时自建的优先被选中（refresh 里按 score 排，
#: 但同分时 sorted 顺序决定先来后到，自建排在前面才不会被外来件顶掉）。
SKILL_ROOTS = [os.path.join(ROOT, "skills")] + market_roots()

#: 市场清单文件（Anthropic 的 .claude-plugin/marketplace.json）位置
MANIFEST_REL = os.path.join(".claude-plugin", "marketplace.json")

FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
KV_RE = re.compile(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$")


def parse_frontmatter(text: str) -> dict[str, Any]:
    """极简 YAML frontmatter 解析：只认 key: value 与 [a, b] 列表。

    不引 PyYAML 是刻意的——市场要在"什么都没装"的机器上也能跑起来，
    元数据格式也就没必要复杂到需要第三方解析器。
    """
    m = FM_RE.match(text or "")
    if not m:
        return {}
    out: dict[str, Any] = {}
    lines = m.group(1).splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.strip()
        i += 1
        if not line or line.startswith("#"):
            continue
        kv = KV_RE.match(line)
        if not kv:
            continue
        key, val = kv.group(1).strip(), kv.group(2).strip()

        # YAML 块标量：`description: >-` / `|` 之后凡是更深缩进的行都归这个键。
        # 这条不是洁癖：Anthropic 的技能清单一律用折叠块写描述，旧实现把 ">-"
        # 当成描述本身（2 个字符），于是 claude-api 这类大技能在市场里显示成空壳，
        # 检索也就永远搜不到它——搬了一屋子货，标牌全是空白。
        if val in ("|", ">", "|-", ">-", "|+", ">+"):
            base = len(raw) - len(raw.lstrip())
            buf: list[str] = []
            while i < len(lines):
                nxt = lines[i]
                if not nxt.strip():
                    if buf:
                        buf.append("")
                    i += 1
                    continue
                if len(nxt) - len(nxt.lstrip()) <= base:
                    break
                buf.append(nxt.strip())
                i += 1
            while buf and not buf[-1]:
                buf.pop()
            out[key] = ("\n" if val.startswith("|") else " ").join(buf).strip()
            continue

        # 块列表：`tags:` 后面跟若干 "- x" 行
        if val == "":
            items: list[str] = []
            j = i
            while j < len(lines):
                nxt = lines[j].strip()
                if nxt.startswith("- "):
                    items.append(nxt[2:].strip().strip("'\""))
                    j += 1
                elif not nxt:
                    j += 1
                else:
                    break
            if items:
                out[key] = items
                i = j
                continue

        if val.startswith("[") and val.endswith("]"):
            out[key] = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
        else:
            out[key] = val.strip("'\"")
    return out


def read_manifest(path: str) -> dict[str, Any] | None:
    """读 vendor 的 marketplace.json。坏清单返回 None 而不是抛异常：
    货架塌了只该降级成"看不见分组"，不该把整个市场扫描一起打挂。
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


#: 需求侧说法 → 技能侧写法。
#: 搬来的技能是英文写的（"any time a .pptx file is involved"），老板嘴里说的是"做个 PPT"。
#: 中间不接这层翻译，检索就永远漏掉最该命中的那一个——这不是算法不够聪明，
#: 是两种语言压根没接上线。
QUERY_ALIASES: dict[str, tuple[str, ...]] = {
    "ppt": ("pptx", "powerpoint", "slide", "presentation"),
    "幻灯片": ("pptx", "slide", "presentation"),
    "演示": ("pptx", "presentation", "slide"),
    "表格": ("xlsx", "spreadsheet", "excel"),
    "excel": ("xlsx", "spreadsheet"),
    "word": ("docx", "document"),
    "文档": ("docx", "document", "pdf"),
    "设计": ("design", "canvas", "art"),
    "前端": ("frontend",),
    "网页": ("web", "frontend", "artifact"),
    "测试": ("testing", "webapp", "test"),
    "品牌": ("brand",),
    "技能": ("skill", "mcp"),
    "动图": ("gif",),
}


def expand_query(q: str) -> list[str]:
    """把一句人话扩成若干可搜的词，原词永远排第一（排序时占优先位）。"""
    terms = [q]
    low = (q or "").lower().strip()
    if not low:
        return terms
    for key, vals in QUERY_ALIASES.items():
        if key in low or (len(low) >= 2 and low in key):
            for v in vals:
                if v not in terms:
                    terms.append(v)
    return terms


class Skill:
    """一条技能元数据。构造期只吃已读到的文本，不碰磁盘。"""

    def __init__(self, name: str, path: str, meta: dict[str, Any],
                 body: str, kind: str) -> None:
        self.name = name
        self.path = path
        self.meta = meta
        self.body = body
        self.kind = kind                      # "skill"（有 SKILL.md）或 "module"（只有脚本）
        self.description = str(meta.get("description") or "").strip()
        self.version = str(meta.get("version") or "").strip()
        self.tags = meta.get("tags") or []
        if isinstance(self.tags, str):
            self.tags = [t.strip() for t in self.tags.split(",") if t.strip()]
        self.requires = meta.get("requires") or []
        if isinstance(self.requires, str):
            self.requires = [t.strip() for t in self.requires.split(",") if t.strip()]

    # -- 可诊断性 -----------------------------------------------------------
    def missing_requires(self) -> list[str]:
        """`requires` 里哪些模块本机没有。用 find_spec 只探不导入。"""
        import importlib.util
        miss = []
        for mod in self.requires:
            try:
                if importlib.util.find_spec(str(mod)) is None:
                    miss.append(str(mod))
            except (ImportError, ValueError, ModuleNotFoundError):
                miss.append(str(mod))
        return miss

    def score(self) -> dict[str, Any]:
        """五项打分。分数不是装饰：它直接对应"这个技能能不能立刻用"。"""
        checks = {
            "has_meta": bool(self.meta),
            "has_desc": len(self.description) >= 8,
            "has_version": bool(re.match(r"^\d+\.\d+", self.version or "")),
            "has_body": len(self.body.strip()) >= 40,
            "deps_ok": not self.missing_requires(),
        }
        return {"score": sum(1 for v in checks.values() if v), "checks": checks}

    def info(self) -> dict[str, Any]:
        s = self.score()
        return {"name": self.name, "kind": self.kind, "path": self.path,
                "description": self.description, "version": self.version,
                "tags": self.tags, "requires": self.requires,
                "missing_requires": self.missing_requires(),
                "score": s["score"], "checks": s["checks"]}


class SkillMarket:
    """本地市场。构造零副作用；扫盘发生在首次 list/search/info。"""

    def __init__(self, roots: list[str] | None = None) -> None:
        self.roots = list(roots or SKILL_ROOTS)
        self._skills: dict[str, Skill] = {}
        self._loaded = False
        self.scan_ms = 0.0
        self.conflicts: list[dict[str, Any]] = []
        self.lazy_loads = 0

    # -- 扫描 ---------------------------------------------------------------
    def refresh(self) -> dict[str, Any]:
        t0 = time.perf_counter()
        found: dict[str, list[Skill]] = {}
        for root in self.roots:
            if not os.path.isdir(root):
                continue
            for entry in sorted(os.listdir(root)):
                d = os.path.join(root, entry)
                if not os.path.isdir(d):
                    continue
                sk = self._read_one(d, entry)
                if sk is not None:
                    found.setdefault(sk.name, []).append(sk)

        self._skills = {}
        self.conflicts = []
        for name, group in found.items():
            group.sort(key=lambda s: (-s.score()["score"], s.path))
            self._skills[name] = group[0]
            if len(group) > 1:
                # 同名并存要说出来，否则"我改的那个怎么没生效"会浪费半天
                self.conflicts.append({"name": name,
                                       "chosen": group[0].path,
                                       "shadowed": [g.path for g in group[1:]]})
        self._loaded = True
        self.lazy_loads += 1
        self.scan_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {"skills": len(self._skills), "conflicts": len(self.conflicts),
                "ms": self.scan_ms}

    def _read_one(self, d: str, dirname: str) -> Skill | None:
        for cand in ("SKILL.md", "skill.md", "Skill.md"):
            p = os.path.join(d, cand)
            if os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                except OSError:
                    return None
                meta = parse_frontmatter(text)
                name = str(meta.get("name") or dirname).strip()
                return Skill(name, p, meta, text, "skill")
        # 没有 SKILL.md：退一步，用目录里的 .py 兜底，docstring 首行当描述
        pys = [f for f in sorted(os.listdir(d)) if f.endswith(".py")]
        if not pys:
            return None
        p = os.path.join(d, pys[0])
        body, desc = "", ""
        try:
            src = open(p, "r", encoding="utf-8", errors="replace").read()
        except OSError:
            return None
        try:
            tree = ast.parse(src)          # 只解析，不执行——逛市场不等于跑陌生代码
            doc = ast.get_docstring(tree) or ""
            desc = (doc.strip().splitlines() or [""])[0]
            body = doc
        except SyntaxError:
            desc = ""
        return Skill(dirname, p, {"description": desc}, body, "module")

    # -- 查询 ---------------------------------------------------------------
    def list(self, tag: str = "") -> list[dict[str, Any]]:
        if not self._loaded:
            self.refresh()
        items = [s.info() for s in self._skills.values()]
        if tag:
            items = [i for i in items if tag in i["tags"]]
        return sorted(items, key=lambda x: (-x["score"], x["name"]))

    def search(self, query: str) -> list[dict[str, Any]]:
        if not self._loaded:
            self.refresh()
        q = (query or "").lower()
        if not q:
            return self.list()
        terms = expand_query(q)
        hits = []
        for s in self._skills.values():
            head = " ".join([s.name, s.description, " ".join(map(str, s.tags))]).lower()
            hay = head + " " + s.body[:2000].lower()
            # 三级排序：名字命中 > 描述/标签命中 > 仅正文命中。
            #   只分两级的后果实测过——搜"门禁"时三个技能全落在正文级，顺序由目录名决定，
            #   真正讲门禁的那个反而不在首位。
            matched = 1
            if q in hay:
                rank = 0 if q in s.name.lower() else (1 if q in head else 2)
            else:
                # 别名翻译命中记 3+ 子级：能救回漏网的那一个，但不许抢直接命中的位置。
                #   子级仍要分名字/描述/正文，并且命中词数多的优先——否则「前端设计」
                #   会把名字里凑巧带 art 的技能排到 frontend-design 前面，
                #   那就成了翻译反而帮倒忙。
                sub: int | None = None
                matched = 0
                for t in terms[1:]:
                    in_name, in_head, in_hay = t in s.name.lower(), t in head, t in hay
                    if not (in_name or in_head or in_hay):
                        continue
                    matched += 1
                    lv = 0 if in_name else (1 if in_head else 2)
                    sub = lv if sub is None or lv < sub else sub
                rank = None if sub is None else 3 + sub
            if rank is None:
                continue
            hits.append((rank, -matched, -s.score()["score"], s.name, s.info()))
        hits.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
        return [h[4] for h in hits]

    def info(self, name: str) -> dict[str, Any] | None:
        if not self._loaded:
            self.refresh()
        s = self._skills.get(name)
        return s.info() if s else None

    def activate(self, name: str) -> dict[str, Any]:
        """到这一步才真正解析/准备执行。仍然只做 ast 编译，不 import——由调用方决定执行。"""
        if not self._loaded:
            self.refresh()
        s = self._skills.get(name)
        if not s:
            return {"ok": False, "error": "skill 不存在: %s" % name}
        miss = s.missing_requires()
        if miss:
            return {"ok": False, "error": "缺依赖: %s" % ",".join(miss), "missing": miss}
        if not s.path.endswith(".py"):
            return {"ok": True, "kind": s.kind, "path": s.path,
                    "note": "说明型技能：无代码可装载，按 SKILL.md 指示执行"}
        try:
            compile(open(s.path, "r", encoding="utf-8", errors="replace").read(),
                    s.path, "exec")
        except SyntaxError as exc:
            return {"ok": False, "error": "语法错误: %s" % exc, "path": s.path}
        return {"ok": True, "kind": s.kind, "path": s.path, "compiled": True}

    def stats(self) -> dict[str, Any]:
        return {"roots": self.roots, "scanned": self._loaded,
                "skills": len(self._skills), "lazy_loads": self.lazy_loads,
                "scan_ms": self.scan_ms, "conflicts": len(self.conflicts)}

    # -- 货架（marketplace 清单）-------------------------------------------
    def marketplace(self) -> dict[str, Any]:
        """把各 vendor 的 marketplace.json 读成"分组 → 技能"的货架视图。

        为什么需要这一层：扫盘只能回答"我这儿有哪些技能"，回答不了"这批技能
        是谁按什么主题打包搬进来的"。老板要关掉/换掉某个来源时，得能整包看清，
        而不是在一堆平铺的名字里猜。
        """
        if not self._loaded:
            self.refresh()
        shelves: list[dict[str, Any]] = []
        for root in self.roots:
            vendor_dir = os.path.dirname(root)          # marketplace/<vendor>/skills → ../
            manifest = os.path.join(vendor_dir, MANIFEST_REL)
            if not os.path.isfile(manifest):
                continue
            data = read_manifest(manifest)
            if data is None:
                shelves.append({"vendor": os.path.basename(vendor_dir),
                                "path": manifest, "error": "manifest 解析失败",
                                "groups": []})
                continue
            groups = []
            for plug in data.get("plugins") or []:
                names = [os.path.basename(str(s).rstrip("/\\"))
                         for s in (plug.get("skills") or [])]
                groups.append({
                    "name": plug.get("name") or "",
                    "description": (plug.get("description") or "").strip(),
                    "declared": len(names),
                    # 清单写了但盘上没有 = 搬家时漏件；盘上有但清单没写 = 野技能。
                    # 两种都要报，否则"市场里明明有 19 个"会一直对不上账。
                    "resolved": [n for n in names if n in self._skills],
                    "missing": [n for n in names if n not in self._skills],
                })
            shelves.append({"vendor": os.path.basename(vendor_dir),
                            "name": data.get("name") or "",
                            "version": str((data.get("metadata") or {}).get("version") or ""),
                            "path": manifest, "groups": groups})
        return {"shelves": shelves, "skills_total": len(self._skills)}


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
def _selftest() -> int:
    import shutil
    import tempfile

    rc = 0
    tmp = tempfile.mkdtemp(prefix="mkt_")
    try:
        def mk(name, md=None, py=None):
            d = os.path.join(tmp, name)
            os.makedirs(d)
            if md is not None:
                open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8").write(md)
            if py is not None:
                open(os.path.join(d, "run.py"), "w", encoding="utf-8").write(py)

        mk("demo-doc", md="---\nname: demo-doc\ndescription: 把散乱笔记整理成结构化文档并输出引用\n"
                          "version: 1.2.0\ntags: [文档, 整理]\nrequires: [json]\n---\n"
                          "## 用法\n\n给定一个目录，输出结构化文档。\n" + "细节说明。" * 10)
        mk("no-meta", md="---\ndescription: 短\n---\n正文很短。")
        mk("bare-mod", py='"""裸模块技能：只用 docstring 当描述。"""\n\n\ndef run():\n    return 1\n')
        mk("broken", py="def run(:\n    pass\n")
        mk("needs-x", md="---\nname: needs-x\ndescription: 需要一个不存在的模块用于验证依赖检查\n"
                         "version: 1.0.0\nrequires: [totally_not_installed_mod]\n---\n" + "说明。" * 20)

        m = SkillMarket(roots=[tmp])
        # 1) 懒加载契约
        if m.stats()["scanned"] is False and m.lazy_loads == 0:
            print("[OK ] 懒加载契约：构造后未扫盘（scanned=False, lazy_loads=0）")
        else:
            print("[FAIL] 构造即扫盘：%s" % m.stats())
            rc = 1

        # 2) 首次 list 才扫
        items = m.list()
        st = m.stats()
        if st["scanned"] and st["lazy_loads"] == 1 and len(items) >= 5:
            print("[OK ] 首次 list 触发一次扫描：%d 个技能 / %.1fms" % (len(items), st["scan_ms"]))
        else:
            print("[FAIL] 扫描异常：%s items=%d" % (st, len(items)))
            rc = 1

        # 3) frontmatter 解析正确
        info = m.info("demo-doc")
        ok = info and info["version"] == "1.2.0" and "文档" in info["tags"] \
            and info["missing_requires"] == []
        print(("[OK ] 元数据解析：version=%s tags=%s" if ok else "[FAIL] 元数据解析错误：%s")
              % ((info["version"], info["tags"]) if ok else info))
        rc |= 0 if ok else 1

        # 4) 裸模块兜底（无 SKILL.md 也要能被发现，描述取 docstring 首行）
        bi = m.info("bare-mod")
        ok = bi and "裸模块技能" in bi["description"] and bi["kind"] == "module"
        print(("[OK ] 裸模块兜底：%s" % bi["description"][:20]) if ok else "[FAIL] 裸模块未被发现")
        rc |= 0 if ok else 1

        # 5) 评分能区分好坏
        good, weak = m.info("demo-doc")["score"], m.info("no-meta")["score"]
        if good > weak:
            print("[OK ] 评分有区分度：demo-doc=%d > no-meta=%d" % (good, weak))
        else:
            print("[FAIL] 评分无区分度：%d vs %d" % (good, weak))
            rc = 1

        # 6) 依赖检查前置（不等 activate 才炸）
        nx = m.info("needs-x")
        ok = nx["missing_requires"] == ["totally_not_installed_mod"] and nx["checks"]["deps_ok"] is False
        print(("[OK ] 依赖前置检查：missing=%s" % nx["missing_requires"]) if ok
              else "[FAIL] 依赖检查失灵：%s" % nx)
        rc |= 0 if ok else 1

        # 7) 搜索：名字命中优先于正文命中
        res = m.search("demo")
        ok = bool(res) and res[0]["name"] == "demo-doc"
        print(("[OK ] 搜索排序：首位 %s" % res[0]["name"]) if ok else "[FAIL] 搜索排序错：%s" % res)
        rc |= 0 if ok else 1

        # 8) activate 的三种结局都要说清楚
        a1 = m.activate("demo-doc")
        a2 = m.activate("needs-x")
        a3 = m.activate("broken")
        a4 = m.activate("不存在")
        ok = a1["ok"] and not a2["ok"] and not a3["ok"] and not a4["ok"] \
            and "缺依赖" in a2["error"] and "语法" in a3["error"]
        print(("[OK ] activate 三态清晰：ok / 缺依赖 / 语法错 / 不存在" if ok
               else "[FAIL] activate 结局不清：%s %s %s %s" % (a1, a2, a3, a4)))
        rc |= 0 if ok else 1

        # 9) 扫描阶段不许 import 陌生代码（用 sys.modules 侧证）
        before = set(sys.modules)
        m.refresh()
        newly = {k for k in set(sys.modules) - before if k in
                 ("run", "bare_mod", "broken")}
        if not newly:
            print("[OK ] 扫描不执行技能代码（无新增模块入 sys.modules）")
        else:
            print("[FAIL] 扫描期竟然 import 了技能模块：%s" % newly)
            rc = 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 10) 真实根目录（项目 skills/）若存在，能扫就扫——不存在不算失败
    real = SkillMarket()
    r = real.refresh()
    print("[OK ] 真实根目录扫描：%d 个技能，冲突 %d（目录不存在则计 0，不算失败）"
          % (r["skills"], r["conflicts"]))

    # 11) 货架对账：manifest 声明的技能，盘上必须全在。
    #     这条是"搬进来"这个动作唯一可信的验收——不数文件、数清单与盘的对账结果。
    mk = real.marketplace()
    declared = missing = 0
    for sh in mk["shelves"]:
        for g in sh["groups"]:
            declared += g["declared"]
            missing += len(g["missing"])
    if mk["shelves"]:
        if missing == 0:
            print("[OK ] 货架对账：%d 个 vendor、清单声明 %d 个技能，盘上零缺件"
                  % (len(mk["shelves"]), declared))
        else:
            bad = [(sh["vendor"], g["name"], g["missing"]) for sh in mk["shelves"]
                   for g in sh["groups"] if g["missing"]]
            print("[FAIL] 清单有货、盘上没有：%s" % bad)
            rc = 1
    else:
        print("[OK ] 无外部货架（marketplace/ 未引入，不判失败）")

    # 12) 搬来的技能要"能被老板的说法查到"，不是"躺在硬盘里"——
    #     这里一律用中文/口语查询，因为那才是真实入口；用英文原名搜属于自欺欺人。
    for q, expect in (("帮我做个 PPT", "pptx"),
                      ("弄个表格", "xlsx"),
                      ("前端设计", "frontend-design")):
        if not any(os.path.isdir(p) for p in real.roots[1:]):
            break
        hits = [it["name"] for it in real.search(q)]
        # 验收口径：进前三即算"查得到"。要求必居首位就成了排序调参，
        # 而这里要保证的是"搬来的货不会被中文需求绕过"，不是名次。
        ok = expect in hits[:3]
        rank = (hits.index(expect) + 1) if expect in hits else None
        print(("[OK ] 口语可检索：「%s」→ %s 第 %s 位" % (q, hits[:3], rank)) if ok
              else "[FAIL] 「%s」前三查不到 %s，实际 %s" % (q, expect, hits[:5]))
        rc |= 0 if ok else 1

    # 13) 元数据合格率：搬进来的技能若大面积没 description，等于搬了一堆空壳
    if any(os.path.isdir(p) for p in real.roots[1:]):
        ext = [it for it in real.list() if it["path"].lower().endswith("skill.md")
               and "marketplace" in it["path"].replace("\\", "/").lower()]
        good = [it for it in ext if it["checks"]["has_desc"] and it["checks"]["has_body"]]
        ratio = (len(good) / len(ext)) if ext else 0.0
        ok = ext and ratio >= 0.9
        print(("[OK ] 外来技能元数据合格：%d/%d（%.0f%%）有描述且有正文"
               % (len(good), len(ext), ratio * 100)) if ok
              else "[FAIL] 外来技能空壳过多：%d/%d" % (len(good), len(ext)))
        rc |= 0 if ok else 1

    # 14) YAML 折叠块描述必须被完整读进来。
    #     旧实现把 ">-" 当描述（2 字符），claude-api 那种十几万字符的大技能
    #     在市场里显示为空壳，检索永远搜不到——"搬了一屋子货，标牌全是空白"。
    fm = parse_frontmatter("---\nname: x\ndescription: >-\n  第一行\n  第二行\n"
                           "tags:\n  - a\n  - b\n---\n正文\n")
    ok = ("第一行" in str(fm.get("description", ""))
          and "第二行" in str(fm.get("description", ""))
          and fm.get("tags") == ["a", "b"])
    print(("[OK ] 折叠块/块列表解析：desc=%r tags=%s"
           % (str(fm.get("description"))[:20], fm.get("tags"))) if ok
          else "[FAIL] 多行 frontmatter 解析失败：%s" % fm)
    rc |= 0 if ok else 1
    return rc


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return _selftest()

    def arg(n, d=""):
        return argv[argv.index(n) + 1] if n in argv else d

    m = SkillMarket()
    if "--list" in argv:
        for it in m.list(tag=arg("--tag")):
            print("%-16s %d/5  %-8s %s" % (it["name"], it["score"],
                                           it["version"] or "-", it["description"][:48]))
    if "--search" in argv:
        for it in m.search(arg("--search")):
            print("%-16s %d/5  %s" % (it["name"], it["score"], it["description"][:56]))
    if "--info" in argv:
        print(json.dumps(m.info(arg("--info")), ensure_ascii=False, indent=2))
    if "--marketplace" in argv:
        mk = m.marketplace()
        if "--json" in argv:
            print(json.dumps(mk, ensure_ascii=False, indent=2))
        else:
            for sh in mk["shelves"]:
                print("货架 %s（%s v%s）" % (sh.get("vendor"), sh.get("name", ""),
                                           sh.get("version", "-")))
                for g in sh["groups"]:
                    flag = "" if not g["missing"] else "  缺件:%s" % g["missing"]
                    print("  · %-16s %2d 个技能  %s%s"
                          % (g["name"], g["declared"], g["description"][:40], flag))
            if not mk["shelves"]:
                print("没有引入外部货架（marketplace/<vendor>/skills 不存在）")
    if "--json" in argv:
        print(json.dumps({"skills": m.list(), "conflicts": m.conflicts,
                          "stats": m.stats(), "marketplace": m.marketplace()},
                         ensure_ascii=False, indent=2))
    if not any(k in argv for k in ("--list", "--search", "--info", "--json",
                                   "--marketplace")):
        print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
