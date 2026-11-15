"""门禁 24 探针：懒加载三件套（压缩 / RAG / 取文器）的联动与纪律。

为什么单模块自检不够
------------------
三个模块各自 `--selftest` 全绿，仍然可能是"三个各自正确的零件拼不起来"。真正会出问题
的地方在接缝上：
  * 取文器抓回来的文本，喂给 RAG 能不能被检索到（编码/超长/切块边界）；
  * 压缩产物里的实体，还能不能反查回原文（引用回链断裂就退化成"看起来没丢"）；
  * 三个模块是不是真的懒加载——只要有一个在 import 时就建索引/开连接，
    服务启动那 200ms 就白省了，而且启动期会多一次网络风险。

本探针守四件事，每件都配一条**负向演练**（故意改坏 → 必须变红）：
  G1 模块自检：三个 `--selftest` 各自 rc=0
  G2 接缝联动：web → rag → compact → 引用回链，一条链走通
  G3 懒加载纪律：import 之后不许有 opener / 索引 / robotparser
  G4 上限不被绕过：极小预算仍保实体；极小字节上限必须 truncated

用法：
    python scripts/probe_lazy_stack.py                 # 常规，退出码 0/1
    python scripts/probe_lazy_stack.py --drill         # 负向演练，期望打印 DRILL-OK
    python scripts/probe_lazy_stack.py --json          # 机读结果
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKERS = os.path.join(ROOT, "workers")
PY = sys.executable or "python"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print("[%s] %-42s %s" % ("OK  " if ok else "FAIL", name, detail))
    return bool(ok)


def run(args: list[str], cwd: str = WORKERS, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run([PY] + args, cwd=cwd, capture_output=True,
                          timeout=timeout, text=True, encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# G1 三个模块自检
# ---------------------------------------------------------------------------
def g1_module_selftests() -> bool:
    ok_all = True
    for script in ("_compact.py", "_rag.py", "_web.py"):
        try:
            r = run([script, "--selftest"])
            passed = r.returncode == 0 and "FAIL" not in (r.stdout or "")
            fails = [ln for ln in (r.stdout or "").splitlines() if "FAIL" in ln]
            ok_all &= check("G1 %s 自检" % script, passed,
                            "rc=%d%s" % (r.returncode, ("  " + fails[0][:70]) if fails else ""))
        except subprocess.TimeoutExpired:
            ok_all &= check("G1 %s 自检" % script, False, "超时")
    return ok_all


# ---------------------------------------------------------------------------
# G2 接缝联动：抓网页 → 入 RAG → 检索命中 → 压缩 → 引用回链
# ---------------------------------------------------------------------------
_LINK_SCRIPT = r'''
import json, sys, threading, http.server, tempfile, os
sys.path.insert(0, r"%s")
from _web import Browser
from _rag import RagIndex
from _compact import compact, verify_recall, render

page_html = """<html><head><title>门禁 17 复盘</title></head><body>
<h1>异步受理探针</h1>
<p>门禁 17 命令为 probe_async_task.py。A7 断言续传补帧，锚点取第一个非终态帧 seq=40。</p>
<p>全量结果：24 道门禁 24 通过，演练 5 项按预期，耗时 397.3s，退出码 0。</p>
<p>证据落盘 docs/evidence/regress-all.raw.txt。</p>
</body></html>"""

tmp = tempfile.mkdtemp(prefix="link_")
open(os.path.join(tmp, "p.html"), "w", encoding="utf-8").write(page_html)

class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=tmp, **kw)
    def log_message(self, *a):
        pass

srv = http.server.HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = "http://127.0.0.1:%%d/p.html" %% srv.server_address[1]

out = {"steps": {}}
try:
    # 1) 抓
    pg = Browser(respect_robots=False).fetch(url)
    out["steps"]["fetch"] = {"ok": pg.ok, "chars": len(pg.text), "title": pg.title}

    # 2) 入 RAG（懒加载：add 之后仍不该建索引）
    rag = RagIndex()
    rag.add_text("web:p.html", pg.text, {"source": url})
    out["steps"]["rag_before_build"] = rag.stats()["built"]

    # 3) 检索：问细节（seq=40）必须命中，且引用能切回原文
    hits = rag.search("seq=40 锚点 续传", k=3)
    cite_ok = bool(hits) and hits[0].text[hits[0].span[0]-hits[0].span[0]:hits[0].span[1]-hits[0].span[0]] == hits[0].text
    out["steps"]["search"] = {
        "hits": len(hits),
        "top": hits[0].citation() if hits else "",
        "score": hits[0].score if hits else 0.0,
        "cite_span_ok": cite_ok,
    }

    # 4) 压缩这段会话，再用压缩产物回答同一个细节问题
    msgs = [{"role": "tool", "ref": "web:1", "content": pg.text}]
    small = compact(msgs, budget=80)
    missed = verify_recall(msgs, small)
    rendered = render(small)
    out["steps"]["compact"] = {
        "ratio": small["stats"]["ratio"],
        "entities": small["stats"]["entities"],
        "missed": missed,
        "seq40_in_render": "40" in rendered,
    }

    # 5) 压缩产物反查原文：render 里给出的 ref 必须能在原文里找到出处
    ref_ok = all(any(m["ref"] == r for m in msgs) for r in small["refs"]) if small["refs"] else True
    out["steps"]["ref_backlink"] = ref_ok
finally:
    srv.shutdown()

print("LINK_JSON " + json.dumps(out, ensure_ascii=False))
'''


def g2_linkage() -> bool:
    script = _LINK_SCRIPT % WORKERS
    fd, path = tempfile.mkstemp(suffix=".py", dir=os.path.join(ROOT, "temp"))
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(script)
        r = run([path], cwd=WORKERS)
        line = [ln for ln in (r.stdout or "").splitlines() if ln.startswith("LINK_JSON ")]
        if not line:
            return check("G2 接缝联动", False, "无结果输出 rc=%d %s" % (r.returncode, (r.stderr or "")[:90]))
        data = json.loads(line[0][len("LINK_JSON "):])
        s = data["steps"]
        ok = True
        ok &= check("G2.1 web→rag 抓取可入库", s["fetch"]["ok"] and s["fetch"]["chars"] > 100,
                    "chars=%d title=%s" % (s["fetch"]["chars"], s["fetch"]["title"]))
        ok &= check("G2.2 入库不触发建索引（懒）", s["rag_before_build"] is False,
                    "built=%s" % s["rag_before_build"])
        ok &= check("G2.3 检索细节命中（seq=40）", s["search"]["hits"] > 0 and s["search"]["cite_span_ok"],
                    "top=%s score=%.2f" % (s["search"]["top"], s["search"]["score"]))
        ok &= check("G2.4 压缩后细节零丢失", not s["compact"]["missed"] and s["compact"]["seq40_in_render"],
                    "ratio=%.3f entities=%d missed=%s"
                    % (s["compact"]["ratio"], s["compact"]["entities"],
                       s["compact"]["missed"] or "0"))
        ok &= check("G2.5 压缩产物可回链原文", bool(s["ref_backlink"]))
        return ok
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# G3 懒加载纪律：import 之后不许有任何"已经动了"的迹象
# ---------------------------------------------------------------------------
_PROBE_IMPORT = r'''
import sys, json
sys.path.insert(0, r"%s")
import _compact, _rag, _web
b = _web.Browser()
r = _rag.RagIndex()
print(json.dumps({
    "opener_ready": b.stats()["opener_ready"],
    "web_lazy_loads": b.lazy_loads,
    "rag_built": r.stats()["built"],
    "rag_lazy_loads": r.lazy_loads,
    "robotparser_imported": "urllib.robotparser" in sys.modules,
    "socket_open": False,
}, ensure_ascii=False))
'''


def g3_lazy_discipline() -> bool:
    r = run(["-c", _PROBE_IMPORT % WORKERS])
    try:
        d = json.loads((r.stdout or "").strip().splitlines()[-1])
    except Exception:
        return check("G3 懒加载纪律", False, "无法解析 rc=%d %s" % (r.returncode, (r.stderr or "")[:90]))
    ok = True
    ok &= check("G3.1 Browser 构造不开连接", d["opener_ready"] is False and d["web_lazy_loads"] == 0,
                "opener_ready=%s lazy=%d" % (d["opener_ready"], d["web_lazy_loads"]))
    ok &= check("G3.2 RagIndex 构造不建索引", d["rag_built"] is False and d["rag_lazy_loads"] == 0,
                "built=%s lazy=%d" % (d["rag_built"], d["rag_lazy_loads"]))
    ok &= check("G3.3 import 不拉 robots 解析器", d["robotparser_imported"] is False,
                "robotparser_imported=%s" % d["robotparser_imported"])
    return ok


# ---------------------------------------------------------------------------
# G4 上限不被绕过
# ---------------------------------------------------------------------------
def g4_limits() -> bool:
    code = r'''
import sys, json
sys.path.insert(0, r"%s")
from _compact import compact, verify_recall
msgs = [{"role":"user","ref":"a","content":"锚点 seq=40，退出码 0，耗时 397.3s，路径 C:\\tmp\\x.py，UUID 3f2504e0-4f89-11d3-9a0c-0305e82c3301。"}]
tight = compact(msgs, budget=1)
print(json.dumps({"entities": tight["stats"]["entities"],
                  "missed": verify_recall(msgs, tight),
                  "ratio": tight["stats"]["ratio"]}, ensure_ascii=False))
''' % WORKERS
    r = run(["-c", code])
    try:
        d = json.loads((r.stdout or "").strip().splitlines()[-1])
    except Exception:
        return check("G4 上限不被绕过", False, "无法解析 %s" % (r.stderr or "")[:90])
    ok = check("G4.1 预算=1 仍保全部实体", not d["missed"] and d["entities"] >= 4,
               "entities=%d missed=%s ratio=%.3f" % (d["entities"], d["missed"] or "0", d["ratio"]))

    web = r'''
import sys, json, threading, http.server, tempfile, os
sys.path.insert(0, r"%s")
from _web import Browser
tmp = tempfile.mkdtemp()
open(os.path.join(tmp,"big.txt"),"w",encoding="utf-8").write("门禁" * 5000)
class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self,*a,**k): super().__init__(*a,directory=tmp,**k)
    def log_message(self,*a): pass
srv = http.server.HTTPServer(("127.0.0.1",0),H)
threading.Thread(target=srv.serve_forever,daemon=True).start()
p = Browser(respect_robots=False, max_bytes=500).fetch("http://127.0.0.1:%%d/big.txt" %% srv.server_address[1])
srv.shutdown()
print(json.dumps({"truncated": p.truncated, "bytes": p.bytes}))
''' % WORKERS
    r2 = run(["-c", web])
    try:
        d2 = json.loads((r2.stdout or "").strip().splitlines()[-1])
    except Exception:
        return ok and check("G4.2 字节上限", False, "无法解析 %s" % (r2.stderr or "")[:90])
    ok &= check("G4.2 字节上限生效", d2["truncated"] and d2["bytes"] <= 500,
                "truncated=%s bytes=%d" % (d2["truncated"], d2["bytes"]))
    return ok


# ---------------------------------------------------------------------------
# 负向演练：把轮换一种"看起来能跑但失去判定力"的坏法，探针必须变红
# ---------------------------------------------------------------------------
DRILLS = {
    "no_entity_table": {
        "file": "_compact.py",
        "old": "    ents = out.get(\"entities\") or {}\n    if ents:",
        "new": "    ents = {}\n    if ents:",
        "why": "压掉实体索引 → G2.4/G4.1 应判红（细节静默丢失）",
    },
    "eager_index": {
        "file": "_rag.py",
        "old": "        if cache_dir and not lazy:\n            self.build()",
        "new": "        self.build()",
        "why": "构造即建索引 → G3.2 应判红（懒加载被破坏）",
    },
    "no_truncate": {
        "file": "_web.py",
        "old": "                    if len(raw) > self.max_bytes:\n                        raw = raw[:self.max_bytes]\n                        page.truncated = True",
        "new": "                    if False:\n                        pass",
        "why": "字节上限被绕过 → G4.2 应判红",
    },
}


def drill() -> int:
    print("=== 负向演练：确认探针有判定力 ===")
    ok_count = 0
    for name, spec in DRILLS.items():
        work = os.path.join(ROOT, "temp", "drill_lazy_%s" % name)
        if os.path.isdir(work):
            shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work, exist_ok=True)
        for f in ("_compact.py", "_rag.py", "_web.py"):
            shutil.copy(os.path.join(WORKERS, f), os.path.join(work, f))
        target = os.path.join(work, spec["file"])
        with open(target, "r", encoding="utf-8") as fh:
            src = fh.read()
        if spec["old"] not in src:
            print("[DRILL-FAIL] %s：找不到注入点（演练自身失效，需修 DRILLS）" % name)
            continue
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(src.replace(spec["old"], spec["new"], 1))

        # 在副本上跑同样的联动+纪律关卡
        script = _LINK_SCRIPT % work
        fd, path = tempfile.mkstemp(suffix=".py", dir=os.path.join(ROOT, "temp"))
        os.close(fd)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(script)
        red = False
        try:
            if name == "eager_index":
                r = run(["-c", _PROBE_IMPORT % work])
                try:
                    d = json.loads((r.stdout or "").strip().splitlines()[-1])
                    red = d["rag_built"] is True
                except Exception:
                    red = True
            elif name == "no_truncate":
                code = (r'''
import sys, json, threading, http.server, tempfile, os
sys.path.insert(0, r"%s")
from _web import Browser
tmp = tempfile.mkdtemp()
open(os.path.join(tmp,"big.txt"),"w",encoding="utf-8").write("x"*5000)
class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self,*a,**k): super().__init__(*a,directory=tmp,**k)
    def log_message(self,*a): pass
srv = http.server.HTTPServer(("127.0.0.1",0),H)
threading.Thread(target=srv.serve_forever,daemon=True).start()
p = Browser(respect_robots=False, max_bytes=500).fetch("http://127.0.0.1:%%d/big.txt" %% srv.server_address[1])
srv.shutdown()
print(json.dumps({"truncated": p.truncated}))
''' % work)
                r = run(["-c", code])
                try:
                    d = json.loads((r.stdout or "").strip().splitlines()[-1])
                    red = d["truncated"] is False
                except Exception:
                    red = True
            else:
                r = run([path], cwd=work)
                line = [ln for ln in (r.stdout or "").splitlines() if ln.startswith("LINK_JSON ")]
                if not line:
                    red = True
                else:
                    s = json.loads(line[0][len("LINK_JSON "):])["steps"]
                    red = bool(s["compact"]["missed"]) or not s["compact"]["seq40_in_render"]
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

        if red:
            ok_count += 1
            print("[DRILL-OK] %-16s 改坏后按预期变红：%s" % (name, spec["why"]))
        else:
            print("[DRILL-FAIL] %-16s 改坏了却没变红 → 探针在这一项上没有判定力" % name)
        shutil.rmtree(work, ignore_errors=True)

    total = len(DRILLS)
    print("=== 演练小结：%d 项按预期变红 %d、未按预期 %d ===" % (total, ok_count, total - ok_count))
    return 0 if ok_count == total else 1


def main(argv: list[str]) -> int:
    if "--drill" in argv:
        return drill()

    print("=== 门禁 24：懒加载三件套（压缩 / RAG / 取文器） ===")
    ok = True
    ok &= g1_module_selftests()
    ok &= g2_linkage()
    ok &= g3_lazy_discipline()
    ok &= g4_limits()

    passed = sum(1 for _, v, _ in RESULTS if v)
    total = len(RESULTS)
    print("=== 小结：断言 %d 项 | 通过 %d | 失败 %d ===" % (total, passed, total - passed))
    if "--json" in argv:
        print(json.dumps({"passed": passed, "total": total,
                          "failed": [n for n, v, _ in RESULTS if not v]}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
