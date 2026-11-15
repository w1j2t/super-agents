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
SKILL_ROOTS = [os.path.join(ROOT, "skills")]

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
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        kv = KV_RE.match(line)
        if not kv:
            continue
        key, val = kv.group(1).strip(), kv.group(2).strip()
        if val.startswith("[") and val.endswith("]"):
            out[key] = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
        else:
            out[key] = val.strip("'\"")
    return out


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
        hits = []
        for s in self._skills.values():
            head = " ".join([s.name, s.description, " ".join(map(str, s.tags))]).lower()
            hay = head + " " + s.body[:2000].lower()
            if q not in hay:
                continue
            # 三级排序：名字命中 > 描述/标签命中 > 仅正文命中。
            #   只分两级的后果实测过——搜"门禁"时三个技能全落在正文级，顺序由目录名决定，
            #   真正讲门禁的那个反而不在首位。
            if q in s.name.lower():
                rank = 0
            elif q in head:
                rank = 1
            else:
                rank = 2
            hits.append((rank, -s.score()["score"], s.name, s.info()))
        hits.sort(key=lambda x: (x[0], x[1], x[2]))
        return [h[3] for h in hits]

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
    if "--json" in argv:
        print(json.dumps({"skills": m.list(), "conflicts": m.conflicts,
                          "stats": m.stats()}, ensure_ascii=False, indent=2))
    if not any(k in argv for k in ("--list", "--search", "--info", "--json")):
        print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
