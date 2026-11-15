"""RAG 基础库：离线、零依赖、懒加载、引用可回链。

设计取舍（为什么不用向量库）
--------------------------
这个项目的场景是"本机几十万字的自有文档 + 一台没有 GPU 常驻的笔记本"。
上向量库要么拉一个 embedding 服务（多一个常驻进程、多一份模型权重），要么把
中文长尾实体（路径、seq=41、rc=3）化成稠密向量——而后者恰恰是稠密检索的弱项：
**数字和标识符在语义空间里几乎没有区分度**。所以这里的底座是"稀疏检索 + 实体精确命中
加权"的混合：

  score = BM25(字词二元组) + 3.0 × 实体命中项        （实体命中见 _entity_boost）

也就是说：问"seq=41 那条续传为什么丢帧"，答案不会因为语义相近被别的段落挤掉。

懒加载契约（可被探针验证）
------------------------
`RagIndex(...)` 构造时**不读盘、不建索引、不 import 任何重库**；一切推迟到首次
`build()` / `search()`。`stats()` 里的 `built` / `lazy_loads` 是这件事的可核对凭据。

引用可回链
----------
每个 chunk 都记 `span=(start, end)` 原文偏移与 `doc_id`，检索结果直接能指回原文那一句，
这也是"压缩不丢细节"的另一半：**摘要给你结论，引用让你回到原文**。

用法
----
```python
rag = RagIndex(cache_dir="data/rag")
rag.add_file(r"C:\\path\\to\\doc.md")
hits = rag.search("门禁 17 偶发红", k=5)
for h in hits:
    print(h.score, h.doc_id, h.span, h.text[:40])
```

命令行：
    python workers/_rag.py --selftest        # 内置语料自检，退出码 0/1
    python workers/_rag.py --index <目录>     # 建索引并落盘
    python workers/_rag.py --query "..." --dir <目录>
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

from _compact import extract_entities

CHUNK_SIZE = 400          # 每块目标字符数
CHUNK_OVERLAP = 80        # 块间重叠，避免结论被切断
ENTITY_WEIGHT = 3.0       # 实体精确命中权重
BM25_K1, BM25_B = 1.5, 0.75

_TEXT_EXT = {".md", ".txt", ".rs", ".py", ".json", ".toml", ".yaml", ".yml", ".log", ".csv"}


# ---------------------------------------------------------------------------
# 分词：中文按二元组（含单字），英文数字按词。不做词干化——宁可多召回。
# ---------------------------------------------------------------------------
_TOKEN = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*|\d+(?:\.\d+)?|[\u4e00-\u9fff]+")


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for m in _TOKEN.finditer(text or ""):
        tok = m.group(0)
        if "\u4e00" <= tok[0] <= "\u9fff":
            if len(tok) == 1:
                out.append(tok)
            else:
                out.extend(tok[i:i + 2] for i in range(len(tok) - 1))
        else:
            out.append(tok.lower())
    return out


@dataclass
class Hit:
    score: float
    doc_id: str
    chunk_id: int
    text: str
    span: tuple[int, int]
    meta: dict[str, Any] = field(default_factory=dict)

    def citation(self) -> str:
        """一行引用，可直接贴进结论里。"""
        return "%s#%d [%d:%d]" % (self.doc_id, self.chunk_id, self.span[0], self.span[1])


@dataclass
class _Doc:
    doc_id: str
    text: str
    meta: dict[str, Any] = field(default_factory=dict)


def _chunk(text: str) -> list[tuple[str, tuple[int, int]]]:
    """切块：优先在段落/句子边界断开，保 overlap。返回 (chunk_text, (start, end))。"""
    text = text or ""
    out: list[tuple[str, tuple[int, int]]] = []
    if not text.strip():
        return out
    start = 0
    n = len(text)
    while start < n:
        end = min(start + CHUNK_SIZE, n)
        if end < n:
            # 回退到最近的句末/换行，最多退 120 字符，避免把一句话劈成两半
            window = text[max(start + CHUNK_SIZE // 2, start):end]
            cut = max(window.rfind("。"), window.rfind("\n"), window.rfind("！"),
                      window.rfind("？"), window.rfind(". "))
            if cut > 0:
                end = max(start + CHUNK_SIZE // 2, start) + cut + 1
        out.append((text[start:end], (start, end)))
        if end >= n:
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return out


class RagIndex:
    """懒加载 RAG 索引。构造零开销，首次查询才建。"""

    def __init__(self, cache_dir: str | None = None, lazy: bool = True) -> None:
        self.cache_dir = cache_dir
        self.lazy = lazy
        self._docs: dict[str, _Doc] = {}
        self._chunks: list[dict[str, Any]] = []
        self._df: Counter[str] = Counter()
        self._built = False
        self.lazy_loads = 0          # 懒加载被触发的次数（探针看这个）
        self.build_ms = 0.0
        if cache_dir and not lazy:
            self.build()

    # -- 写入 ---------------------------------------------------------------
    def add_text(self, doc_id: str, text: str, meta: dict[str, Any] | None = None) -> None:
        self._docs[doc_id] = _Doc(doc_id=doc_id, text=text, meta=meta or {})
        self._built = False

    def add_file(self, path: str, doc_id: str | None = None) -> bool:
        ext = os.path.splitext(path)[1].lower()
        if ext not in _TEXT_EXT or not os.path.isfile(path):
            return False
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            return False
        key = doc_id or os.path.basename(path)
        self.add_text(key, text, {"path": os.path.abspath(path),
                                  "mtime": os.path.getmtime(path),
                                  "size": os.path.getsize(path)})
        return True

    def add_dir(self, root: str, limit: int = 5000) -> int:
        n = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in
                           ("target", "temp", ".git", "__pycache__", "node_modules", ".cargo")]
            for fn in filenames:
                if os.path.splitext(fn)[1].lower() in _TEXT_EXT:
                    rel = os.path.relpath(os.path.join(dirpath, fn), root)
                    if self.add_file(os.path.join(dirpath, fn), doc_id=rel):
                        n += 1
                    if n >= limit:
                        return n
        return n

    # -- 建索引 -------------------------------------------------------------
    def build(self, force: bool = False) -> dict[str, Any]:
        if self._built and not force:
            return self.stats()
        import time
        t0 = time.perf_counter()
        self._chunks = []
        self._df = Counter()
        for doc in self._docs.values():
            for i, (ctext, span) in enumerate(_chunk(doc.text)):
                toks = tokenize(ctext)
                tf = Counter(toks)
                for t in tf:
                    self._df[t] += 1
                ents = extract_entities(ctext)
                self._chunks.append({
                    "doc_id": doc.doc_id,
                    "chunk_id": i,
                    "text": ctext,
                    "span": span,
                    "len": max(len(toks), 1),
                    "tf": dict(tf),
                    "entities": {k: v for k, v in ents.items()},
                    "meta": doc.meta,
                })
        self._built = True
        self.build_ms = round((time.perf_counter() - t0) * 1000, 2)
        if self.cache_dir:
            self._persist()
        return self.stats()

    def _persist(self) -> None:
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            payload = {
                "version": 1,
                "docs": {k: {"text": v.text, "meta": v.meta} for k, v in self._docs.items()},
            }
            with open(os.path.join(self.cache_dir, "rag-index.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
        except OSError:
            pass   # 落盘失败不影响内存检索

    # -- 检索 ---------------------------------------------------------------
    def _ensure(self) -> None:
        if not self._built:
            self.lazy_loads += 1          # 懒加载在此刻发生
            self.build()

    def search(self, query: str, k: int = 5, min_score: float = 0.0) -> list[Hit]:
        self._ensure()
        q_tokens = tokenize(query)
        q_ents = set()
        for vals in extract_entities(query).values():
            q_ents.update(vals)
        if not self._chunks:
            return []
        n = len(self._chunks)
        avg_len = sum(c["len"] for c in self._chunks) / n
        scored: list[Hit] = []
        for c in self._chunks:
            score = 0.0
            for t in q_tokens:
                tf = c["tf"].get(t)
                if not tf:
                    continue
                df = self._df.get(t, 0) or 1
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                denom = tf + BM25_K1 * (1 - BM25_B + BM25_B * c["len"] / avg_len)
                score += idf * (tf * (BM25_K1 + 1)) / denom
            if q_ents:
                hit_ents = 0
                flat = [v for vals in c["entities"].values() for v in vals]
                for e in q_ents:
                    if any(e in f for f in flat):
                        hit_ents += 1
                if hit_ents:
                    score += ENTITY_WEIGHT * hit_ents
            if score > min_score:
                scored.append(Hit(score=round(score, 4), doc_id=c["doc_id"],
                                  chunk_id=c["chunk_id"], text=c["text"],
                                  span=c["span"], meta=c["meta"]))
        scored.sort(key=lambda h: (-h.score, h.doc_id, h.chunk_id))
        return scored[:k]

    def answer_context(self, query: str, k: int = 5, max_chars: int = 2000) -> dict[str, Any]:
        """给上层 agent 的组装件：片段 + 引用 + 预算内的拼接正文。"""
        hits = self.search(query, k=k)
        buf, used, cites = [], 0, []
        for h in hits:
            if used + len(h.text) > max_chars:
                continue
            buf.append(h.text)
            cites.append(h.citation())
            used += len(h.text)
        return {"query": query, "citations": cites, "context": "\n\n".join(buf),
                "hits": len(hits), "chars": used}

    def stats(self) -> dict[str, Any]:
        return {"docs": len(self._docs), "chunks": len(self._chunks),
                "terms": len(self._df), "built": self._built,
                "lazy_loads": self.lazy_loads, "build_ms": self.build_ms}


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
_CORPUS = {
    "gates.md": "门禁 17 是异步受理探针，命令为 probe_async_task.py --exe。"
                "A7 用例断言续传补帧，锚点取流中第一个非终态帧的 seq。"
                "曾出现偶发红：首连接上时任务已收尾，两帧正好是快照加终态。",
    "evidence.txt": "regress-all 全量结果：24 道门禁 24 通过，演练 5 项按预期，"
                    "耗时 397.3s，退出码 0，证据落盘 docs/evidence/regress-all.raw.txt。",
    "llm.md": "真模型链路走本机 Ollama，模型 minicpm5-2b:f16，"
              "单轮 129.9s，共 110 轮，内存 12.4 GB。缺模型时返回 rc=3 视为 SKIP。",
}


def _selftest() -> int:
    rc = 0

    # 1) 懒加载契约：构造后必须没建索引、没触发加载
    rag = RagIndex(cache_dir=None, lazy=True)
    for name, text in _CORPUS.items():
        rag.add_text(name, text)
    st0 = rag.stats()
    if st0["built"] or st0["lazy_loads"]:
        print("[FAIL] 懒加载违约：构造后已 built=%s lazy_loads=%d" % (st0["built"], st0["lazy_loads"]))
        rc = 1
    else:
        print("[OK ] 懒加载契约：构造/灌数据后仍 built=False lazy_loads=0")

    # 2) 首次查询触发加载，且只触发一次
    hits = rag.search("门禁 17 偶发红 锚点 seq", k=3)
    st1 = rag.stats()
    if st1["lazy_loads"] == 1 and st1["built"]:
        print("[OK ] 首次检索触发加载一次：chunks=%d terms=%d build_ms=%.2f"
              % (st1["chunks"], st1["terms"], st1["build_ms"]))
    else:
        print("[FAIL] 懒加载计数异常：lazy_loads=%d built=%s" % (st1["lazy_loads"], st1["built"]))
        rc = 1
    rag.search("退出码", k=1)
    if rag.stats()["lazy_loads"] == 1:
        print("[OK ] 二次检索不再重建")
    else:
        print("[FAIL] 二次检索又重建了一次索引")
        rc = 1

    # 3) 相关性：问门禁 17，首位必须是 gates.md
    if hits and hits[0].doc_id == "gates.md":
        print("[OK ] 相关性命中首位：%s score=%.3f cite=%s"
              % (hits[0].doc_id, hits[0].score, hits[0].citation()))
    else:
        print("[FAIL] 相关性命中错位：%s" % ([h.doc_id for h in hits],))
        rc = 1

    # 4) 引用可回链：span 必须能在原文里切回同样文本
    bad = 0
    for h in rag.search("门禁 17 偶发红", k=3):
        src = _CORPUS.get(h.doc_id, "")
        if src[h.span[0]:h.span[1]] != h.text:
            bad += 1
    if bad:
        print("[FAIL] 引用回链 %d 处对不上原文偏移" % bad)
        rc = 1
    else:
        print("[OK ] 引用回链：span 切回的文本与 chunk 完全一致")

    # 5) 实体精确命中加权：查 rc=3 必须落在 llm.md（语义上跟"退出码"很像，靠实体拉正）
    e_hits = rag.search("rc=3 缺模型", k=3)
    if e_hits and e_hits[0].doc_id == "llm.md":
        print("[OK ] 实体加权生效：rc=3 → %s（score=%.3f）" % (e_hits[0].doc_id, e_hits[0].score))
    else:
        print("[FAIL] 实体加权失效：%s" % ([h.doc_id for h in e_hits],))
        rc = 1

    # 6) 预算组装：answer_context 不超预算且引用数与片段数一致
    ctx = rag.answer_context("全量与演练结果", k=3, max_chars=120)
    if ctx["chars"] <= 120 and len(ctx["citations"]) >= 1:
        print("[OK ] 预算组装：chars=%d citations=%d" % (ctx["chars"], len(ctx["citations"])))
    else:
        print("[FAIL] 预算组装异常：%s" % ctx)
        rc = 1

    # 7) 空查询/空库不崩
    empty = RagIndex()
    if empty.search("任何东西") == [] and empty.answer_context("x")["hits"] == 0:
        print("[OK ] 空库检索安全返回空")
    else:
        print("[FAIL] 空库检索未安全返回")
        rc = 1
    return rc


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return _selftest()
    if "--index" in argv:
        root = argv[argv.index("--index") + 1]
        cache = argv[argv.index("--cache") + 1] if "--cache" in argv else None
        rag = RagIndex(cache_dir=cache)
        n = rag.add_dir(root)
        st = rag.build()
        print("已索引文件 %d 个 → %s" % (n, json.dumps(st, ensure_ascii=False)))
        return 0
    if "--query" in argv:
        q = argv[argv.index("--query") + 1]
        d = argv[argv.index("--dir") + 1] if "--dir" in argv else "."
        rag = RagIndex()
        rag.add_dir(d)
        for h in rag.search(q, k=int(argv[argv.index("--k") + 1]) if "--k" in argv else 5):
            print("%.3f  %s\n      %s" % (h.score, h.citation(), h.text[:120].replace("\n", " ")))
        return 0
    print("用法: --selftest | --index <目录> [--cache <目录>] | --query <q> [--dir <目录>] [--k N]")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
