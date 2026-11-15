"""本地个人助理：把"压缩 / 检索 / 取网页"三件套拼成一个能用的助手。

它解决的真实问题
--------------
老板电脑里散着一堆 md、py、txt，问一句"上次那个门禁 17 的锚点怎么定的"，
现在的做法是打开搜索框、翻文件、肉眼比对——慢且容易翻错版本。这个助理只做三件事：

  1. **就地索引**：`--index <目录>` 把本地资料切片建索引（落盘缓存，第二次直接加载）；
  2. **带引用回答**：`--ask "问题"` 检索出证据片段，每条都带 `文件#序号[起:止]`，
     拿不准就明说"没找到"，绝不自己编；
  3. **长会话不失细节**：`--session` 读一段会话，先按预算压缩（叙述可裁、实体必留），
     再拼进上下文——这样"问过什么、定了什么"不会因为超预算被整段丢掉。

隐私口径（默认值就是承诺）
------------------------
默认**完全离线**：不联网、不调模型，只做本地检索与拼装。
只有显式加 `--llm` 且本机 Ollama 可用时才生成自然语言答案；没有模型就退回"给证据片段"，
并在输出里写明 `mode="evidence-only"`——降级要说出来，不能让老板以为答案被生成过。

懒加载口径
---------
不 `--index` 就不碰磁盘；构造 RagIndex 不建索引；压缩只在 `--session` 时发生。
`--stats` 能看见这三件事的真实计数。

用法
----
    python workers/personal-assistant.py --index "C:\\path\\to\\docs"
    python workers/personal-assistant.py --ask "门禁 17 的锚点怎么定"
    python workers/personal-assistant.py --ask "..." --k 8 --json
    python workers/personal-assistant.py --session data/conv.jsonl --ask "刚才定了什么"
    python workers/personal-assistant.py --selftest        # 全离线自检
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _compact import compact, render, verify_recall  # noqa: E402
from _rag import RagIndex                             # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_INDEX = os.path.join(ROOT, "data", "assistant-index")
INDEXABLE = (".md", ".py", ".txt", ".json", ".yaml", ".yml", ".toml", ".rs", ".ts")


class Assistant:
    """一个能在本地答话的助手。构造期零副作用（不读盘、不建索引、不联网）。"""

    def __init__(self, index_dir: str = DEFAULT_INDEX, budget: int = 6000) -> None:
        self.index_dir = index_dir
        self.budget = budget
        self.rag = RagIndex(cache_dir=index_dir, lazy=True)
        self.events: list[dict] = []

    # -- 索引 ---------------------------------------------------------------
    def build_index(self, root: str, limit: int = 5000) -> dict:
        t0 = time.perf_counter()
        n = self.rag.add_dir(root, limit=limit)
        info = self.rag.build()
        self.events.append({"ev": "index", "root": root, "files": n,
                            "ms": round((time.perf_counter() - t0) * 1000, 1)})
        return {"files": n, **info, "ms": self.events[-1]["ms"]}

    # -- 问答 ---------------------------------------------------------------
    def ask(self, question: str, k: int = 5, max_chars: int = 2400,
            session: str = "", use_llm: bool = False) -> dict:
        ctx = self.rag.answer_context(question, k=k, max_chars=max_chars)
        # answer_context 的口径：hits 是**命中条数**（int），引用在 citations，正文在 context
        n_hits = int(ctx.get("hits") or 0)
        cites = list(ctx.get("citations") or [])
        out = {
            "question": question,
            "mode": "evidence-only",
            "answer": "",
            "citations": cites,
            "context_chars": int(ctx.get("chars") or 0),
            "hit_count": n_hits,
            "session_note": "",
            "grounded": n_hits > 0,
        }
        session_ctx, session_note = ("", "")
        if session:
            # 会话压缩要独立于"这次检索有没有命中"：即便本地资料里查不到，
            # 也该把"刚才聊过什么、定了什么"带上——这正是助理该记住的部分。
            session_ctx, session_note = self._compress_session(session)
            out["session_note"] = session_note

        if not n_hits:
            # 证据不足时**不生成**：宁可说"没找到"，也不能让模型拿常识补一篇像样的答案
            out["answer"] = "本地资料里没有找到相关证据（索引目录：%s）。" % self.index_dir
            return out

        if use_llm:
            text = self._generate(question, ctx.get("context", ""), session_ctx)
            if text:
                out.update(mode="llm", answer=text)
            else:
                out["answer"] = "本机模型不可用，已降级为证据片段（mode=evidence-only）。"
        else:
            out["answer"] = "证据片段如下（未调用模型）：\n" + \
                            (ctx.get("context", "")[:max_chars])
        return out

    def _compress_session(self, path: str) -> tuple[str, str]:
        """把会话压到预算内：叙述可裁，实体（路径/数字/UUID/判定语）必须留。"""
        if not os.path.isfile(path):
            return "", "会话文件不存在，已跳过"
        msgs = []
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msgs.append({"role": obj.get("role", "user"),
                             "ref": obj.get("ref", "s%d" % len(msgs)),
                             "content": obj.get("content", "")})
        if not msgs:
            return "", "会话为空"
        small = compact(msgs, budget=self.budget)
        missed = verify_recall(msgs, small)
        ratio = small["stats"]["ratio"]
        note = "压缩比 %.2f，实体 %d 个，丢失 %d 个" % (
            ratio, small["stats"]["entities"], len(missed))
        if ratio >= 0.95:
            # 比接近 1 不一定是压缩失败：这段会话每句都带新实体（新路径/新编号）时，
            # 无损压缩本来就无从下手。要把这个判断说出来，不能让老板以为"压了 95%"。
            note += "（每句都含新实体，本段无可压冗余——不做假压缩）"
        if "repeated_merged" in small["stats"] and small["stats"]["repeated_merged"]:
            note += "；另归并同义重复 %d 条" % small["stats"]["repeated_merged"]
        if missed:
            # 丢细节就是缺陷，不当成小事：原样多带一份原文，并把这个事实写进输出
            note += "；⚠ 检出丢失：%s（已附原文兜底）" % ",".join(missed[:5])
            return "\n".join(m["content"] for m in msgs)[:self.budget], note
        return render(small), note

    def _generate(self, question: str, context: str, session_ctx: str) -> str:
        """可选：走本机 Ollama。拿不到就返回空串，由上层降级——不抛异常。"""
        try:
            from _llm import chat                      # 懒加载：不用模型就不 import
        except Exception:
            return ""
        prompt = ("只依据下面资料回答，资料里没有就说不知道，不要补充资料外内容。\n\n"
                  "【资料】\n%s\n\n【会话要点】\n%s\n\n【问题】%s\n"
                  % (context[:4000], session_ctx[:1500], question))
        try:
            return chat(prompt, temperature=0.2) or ""
        except Exception:
            return ""

    def stats(self) -> dict:
        return {"rag": self.rag.stats(), "events": self.events,
                "index_dir": self.index_dir, "budget": self.budget}


# ---------------------------------------------------------------------------
# 自检：全程离线，造一个小资料库
# ---------------------------------------------------------------------------
_DOC_A = """# 门禁 17 复盘

异步受理探针的 A7 断言要验证续传补帧。锚点取流中第一个非终态帧的 seq，实测为 seq=40。
尝试次数从 3 次提到 6 次，max_events 从 2 提到 8。
全量结果 24 道门禁全部通过，退出码 0。
"""

_DOC_B = """# 演练口径

负向演练不计入门禁道数。判断标准是"改坏一处之后门禁必须变红"。
演练 5 项按预期，未按预期 0。证据落盘 docs/evidence/regress-all.raw.txt。
"""

_DOC_C = """# 无关文档

今天天气不错，适合出门走走。
"""


def _selftest() -> int:
    import shutil
    import tempfile

    rc = 0
    tmp = tempfile.mkdtemp(prefix="assist_")
    docs = os.path.join(tmp, "docs")
    os.makedirs(docs)
    for name, body in (("gates.md", _DOC_A), ("drill.md", _DOC_B), ("misc.md", _DOC_C)):
        with open(os.path.join(docs, name), "w", encoding="utf-8") as fh:
            fh.write(body)

    idx_dir = os.path.join(tmp, "idx")
    try:
        # 1) 懒加载契约
        a = Assistant(index_dir=idx_dir)
        st = a.stats()["rag"]
        if st["built"] or a.rag.lazy_loads:
            print("[FAIL] 懒加载违约：构造即建索引 %s" % st)
            rc = 1
        else:
            print("[OK ] 懒加载契约：构造后 built=False lazy_loads=0，且未读盘")

        # 2) 建索引
        info = a.build_index(docs)
        if info.get("files") == 3 and a.rag.stats()["built"]:
            print("[OK ] 索引完成：%d 个文件 / %d 块 / %.1fms"
                  % (info["files"], info.get("chunks", 0), info.get("ms", 0)))
        else:
            print("[FAIL] 索引异常：%s" % info)
            rc = 1

        # 3) 带引用回答：问的是细节（seq=40），必须命中正确文件
        r = a.ask("A7 的锚点 seq 定成多少", k=3)
        cite = r["citations"][0] if r["citations"] else ""
        if r["grounded"] and "gates.md" in cite:
            print("[OK ] 检索命中正确来源：%s" % cite)
        else:
            print("[FAIL] 检索未命中：grounded=%s cite=%s" % (r["grounded"], cite))
            rc = 1

        # 4) 引用能切回原文（抽查 span 是否指向真实片段）
        hits = a.rag.search("A7 的锚点 seq", k=1)
        ok_span = bool(hits) and hits[0].text.strip() and len(hits[0].text) > 20
        print(("[OK ] 引用片段可回读：%d 字" if ok_span else "[FAIL] 引用片段为空：%d 字")
              % (len(hits[0].text) if hits else 0))
        rc |= 0 if ok_span else 1

        # 5) 无证据不编造
        r2 = a.ask("火星殖民计划预算", k=3)
        if not r2["grounded"] and "没有找到" in r2["answer"]:
            print("[OK ] 无证据时不编造：%s" % r2["answer"][:32])
        else:
            print("[FAIL] 无证据却给了答案（幻觉风险）：%s" % r2["answer"][:60])
            rc = 1

        # 6) 长会话压缩：30 轮里塞满实体，压缩后实体必须零丢失
        sess = os.path.join(tmp, "conv.jsonl")
        with open(sess, "w", encoding="utf-8") as fh:
            for i in range(30):
                fh.write(json.dumps({"role": "user", "ref": "r%d" % i,
                                     "content": "第 %d 轮：重试次数定为 6，超时 1800s，"
                                                "证据写 docs/evidence/run-%d.raw.txt，"
                                                "退出码 0。" % (i, i)},
                                    ensure_ascii=False) + "\n")
        r3 = a.ask("刚才定了什么", k=3, session=sess)
        note = r3["session_note"]
        if "丢失 0" in note:
            print("[OK ] 长会话压缩零丢失：%s" % note)
        else:
            print("[FAIL] 长会话压缩丢了细节：%s" % note)
            rc = 1

        # 6b) 不可压会话必须**明说**不可压，而不是悄悄报个漂亮的 1.00 糊过去
        if "无可压冗余" in note:
            print("[OK ] 不可压会话如实标注：含新实体的会话不假装压缩")
        else:
            print("[FAIL] ratio≈1 却没说清原因，易被误读为压缩成功：%s" % note)
            rc = 1

        # 6c) 可压会话必须真压下来：同一句话重复三十轮，实体全被首句覆盖
        sess2 = os.path.join(tmp, "conv2.jsonl")
        with open(sess2, "w", encoding="utf-8") as fh:
            for i in range(30):
                fh.write(json.dumps({"role": "user", "ref": "d%d" % i,
                                     "content": "第 %d 轮复核：重试 6 次，超时 1800s，"
                                                "退出码 0，结论不变。" % i},
                                    ensure_ascii=False) + "\n")
        r4 = a.ask("刚才定了什么", k=3, session=sess2)
        n4 = r4["session_note"]
        ratio = float(n4.split("压缩比 ")[1].split("，")[0]) if "压缩比 " in n4 else 9.9
        if ratio < 0.6 and "丢失 0" in n4:
            print("[OK ] 同义会话真压缩：%s" % n4)
        else:
            print("[FAIL] 同义重复压不下来（ratio=%.2f，应为 <0.6）：%s" % (ratio, n4))
            rc = 1

        # 7) 默认不调模型（离线优先）
        if r3["mode"] == "evidence-only":
            print("[OK ] 默认离线：mode=evidence-only（未调用任何模型）")
        else:
            print("[FAIL] 默认居然调用了模型：%s" % r3["mode"])
            rc = 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return rc


def main(argv: list[str]) -> int:
    def arg(name, default=None):
        return argv[argv.index(name) + 1] if name in argv else default

    if "--selftest" in argv:
        return _selftest()

    a = Assistant(index_dir=arg("--index-dir", DEFAULT_INDEX), budget=int(arg("--budget", 6000)))
    if "--index" in argv:
        info = a.build_index(arg("--index"))
        print(json.dumps({"indexed": info}, ensure_ascii=False, indent=2))
    if "--ask" in argv:
        res = a.ask(arg("--ask"), k=int(arg("--k", 5)),
                    session=arg("--session", ""), use_llm="--llm" in argv)
        if "--json" in argv:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print("问题：%s" % res["question"])
            print("模式：%s  证据：%d 条  上下文：%d 字"
                  % (res["mode"], res["hit_count"], res["context_chars"]))
            if res["session_note"]:
                print("会话：%s" % res["session_note"])
            print("-" * 60)
            print(res["answer"])
            if res["citations"]:
                print("-" * 60)
                for c in res["citations"]:
                    print("· %s" % c)
    if "--stats" in argv:
        print(json.dumps(a.stats(), ensure_ascii=False, indent=2))
    if len(argv) == 0:
        print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
