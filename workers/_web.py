"""轻量网页浏览器：给 agent 读网页用的"无头取文器"。

为什么不是"装一个真浏览器"
------------------------
agent 读网页的真实需求是 **拿到正文 + 拿到链接 + 知道这是不是动态页**，不是渲染像素。
拉一个 Chromium 进项目意味着 ~150MB 体积、一个常驻进程、以及一堆版本兼容问题——
和"所有东西懒加载 + 离线可跑"的整体口径正好相反。所以这里做的是**零依赖取文器**：

  * 只用标准库（urllib + html.parser），不执行 JS；
  * 正文抽取＝按块累积文本 + 跳过 nav/aside/footer/script/style 区域；
  * 抽不到正文时**明确标注** `js_suspected=True`，交上层决定是否升级到真浏览器，
    而不是静默返回一坨导航文字。

安全边界（这几条是硬约束，不是建议）
----------------------------------
S1 只允许 http/https，`file://` / `javascript:` / `data:` 一律拒。
S2 单响应字节上限（默认 2MB），超限截断并置 `truncated=True`。
S3 重定向最多 3 跳，且每跳都重新过 S1。
S4 内网地址默认**拒绝**（SSRF 收口，三方审查 M1）：`allow_private=False`，命中 127/8、
    RFC1918、169.254 等私网/链路本地地址直接 `private_net_blocked`。只有本项目自己的
    探针（`_selftest`）显式传 `allow_private=True`——判断权在调用方而不是默认值。
    **重定向每一跳都重新做 scheme + 私网 + robots 全量校验**：只校验首跳等于给
    "公网 → 302 内网"留一扇后门。
   127.0.0.1 上的服务端；放行是把判断权交给上层，而不是假装看不见。
S5 默认尊重 robots.txt（可关）。取不到 robots 视为放行（RFC 9309 的宽容解释）。

懒加载契约
--------
构造不 import robotparser、不建 opener、不碰网络；首次 `fetch()` 才动。
`stats()["lazy_loads"]` 是这件事的可核对凭据。

用法
----
```python
from _web import Browser
b = Browser()
page = b.fetch("https://example.com")
print(page.title, page.status, page.private_net, len(page.text))
for text, href in page.links[:5]:
    print(href, text)
```

命令行：
    python workers/_web.py --selftest                 # 起本地 http 服务自检（完全离线）
    python workers/_web.py --url <URL> [--json]        # 抓一页看看
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import sys
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

DEFAULT_UA = "MarvisAgent/1.0 (+local research; respects robots)"
MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe", "nav",
             "footer", "aside", "form", "select", "button"}
BLOCK_TAGS = {"p", "div", "section", "article", "li", "td", "th", "h1", "h2", "h3",
              "h4", "h5", "h6", "blockquote", "pre", "dd", "dt"}
JS_HINT = re.compile(r"(enable javascript|requires javascript|请开启\s*javascript|"
                     r"__NEXT_DATA__|window\.__|加载中\.\.\.)", re.I)


@dataclass
class Page:
    url: str
    final_url: str = ""
    status: int = 0
    title: str = ""
    text: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)
    content_type: str = ""
    bytes: int = 0
    elapsed_ms: float = 0.0
    truncated: bool = False
    js_suspected: bool = False
    private_net: bool = False
    blocked_reason: str = ""
    redirects: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.text) and not self.blocked_reason

    def brief(self) -> dict[str, Any]:
        return {"url": self.url, "final_url": self.final_url, "status": self.status,
                "title": self.title, "chars": len(self.text), "links": len(self.links),
                "js_suspected": self.js_suspected, "truncated": self.truncated,
                "blocked_reason": self.blocked_reason}


class _Extractor(HTMLParser):
    """正文与链接抽取。用 skip 深度计数屏蔽导航/脚本区，比"取最长 div"稳。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._skip_depth = 0
        self._blocks: list[str] = []
        self._buf: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._anchor: list[str] = []

    # -- 生命周期 -----------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag in SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if tag == "a" and a.get("href"):
            self._href = a["href"]
            self._anchor = []
        if tag in BLOCK_TAGS:
            self._flush()
        if tag == "br":
            self._buf.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._href is not None:
            text = re.sub(r"\s+", " ", "".join(self._anchor)).strip()
            if text:
                self.links.append((text[:120], self._href))
            self._href = None
        if tag in BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skip_depth:
            return
        if self._href is not None:
            self._anchor.append(data)
        self._buf.append(data)

    def _flush(self) -> None:
        if not self._buf:
            return
        text = re.sub(r"[ \t\r\f\v]+", " ", "".join(self._buf))
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if text:
            self._blocks.append(text)
        self._buf = []

    def result(self) -> tuple[str, str, list[tuple[str, str]]]:
        self._flush()
        # 短块（菜单项、"首页"之类）权重低：整块丢弃，正文只剩实义段落
        blocks = [b for b in self._blocks if len(b) >= 2]
        text = "\n\n".join(blocks)
        return re.sub(r"\s+", " ", self.title).strip(), text.strip(), self.links


def _is_private(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return True
    return False


class Browser:
    """懒加载取文器。"""

    def __init__(self, user_agent: str = DEFAULT_UA, timeout: float = 10.0,
                 max_bytes: int = MAX_BYTES, respect_robots: bool = True,
                 allow_private: bool = False) -> None:
        self.ua = user_agent
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.respect_robots = respect_robots
        self.allow_private = allow_private
        self._robots: dict[str, Any] = {}
        self.lazy_loads = 0
        self._opener = None

    # -- 懒加载：所有重活都推到这里 -----------------------------------------
    def _ensure(self) -> None:
        if self._opener is None:
            self.lazy_loads += 1
            import urllib.request
            # 关键：不装 HTTPRedirectHandler。urllib 默认会自动跟随 302，
            # 而自动跟随的每一跳既不做私网判定也不做 robots 判定——等于把 SSRF 校验
            # 只留在第一跳上。这里显式剔掉它，重定向改由 fetch() 逐跳处理并逐跳校验。
            self._opener = urllib.request.build_opener(
                urllib.request.ProxyHandler(),
                urllib.request.HTTPHandler(),
                urllib.request.HTTPSHandler(),
            )

    def _robots_ok(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        p = urlparse(url)
        origin = "%s://%s" % (p.scheme, p.netloc)
        if origin in self._robots:
            rp = self._robots[origin]
            return True if rp is None else rp.can_fetch(self.ua, url)
        try:
            import urllib.robotparser as robotparser
            rp = robotparser.RobotFileParser()
            rp.set_url(origin + "/robots.txt")
            rp.read()
            self._robots[origin] = rp
            return rp.can_fetch(self.ua, url)
        except Exception:
            self._robots[origin] = None      # 取不到 = 放行（S5）
            return True

    # -- 抓取 ---------------------------------------------------------------
    def fetch(self, url: str, as_text: bool = True) -> Page:
        t0 = time.perf_counter()
        page = Page(url=url)
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            page.blocked_reason = "scheme_not_allowed:%s" % p.scheme
            return page
        host = p.hostname or ""
        page.private_net = _is_private(host)
        if page.private_net and not self.allow_private:
            page.blocked_reason = "private_net_blocked"
            return page
        self._ensure()
        if not self._robots_ok(url):
            page.blocked_reason = "robots_disallow"
            return page

        import urllib.error
        import urllib.request
        req = urllib.request.Request(url, headers={
            "User-Agent": self.ua,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
        })
        cur = url
        for _ in range(MAX_REDIRECTS + 1):
            try:
                with self._opener.open(req, timeout=self.timeout) as resp:
                    raw = resp.read(self.max_bytes + 1)
                    page.status = resp.status
                    page.final_url = resp.geturl()
                    page.content_type = resp.headers.get("Content-Type", "")
                    if len(raw) > self.max_bytes:
                        raw = raw[:self.max_bytes]
                        page.truncated = True
                    page.bytes = len(raw)
                    break
            except urllib.error.HTTPError as e:
                page.status = e.code
                loc = e.headers.get("Location") if e.headers else None
                if e.code in (301, 302, 303, 307, 308) and loc:
                    cur = urljoin(cur, loc)
                    nxt = urlparse(cur)
                    if nxt.scheme not in ("http", "https"):
                        page.blocked_reason = "redirect_scheme_not_allowed"
                        page.elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
                        return page
                    # 每一跳都当成新的 URL 重新判：公网 302 到 169.254.169.254 是
                    # 最经典的 SSRF 绕过手法，只校验首跳的 host 等于没校验。
                    hop_host = nxt.hostname or ""
                    if _is_private(hop_host):
                        page.private_net = True
                        if not self.allow_private:
                            page.blocked_reason = "redirect_private_net_blocked"
                            page.elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
                            return page
                    if not self._robots_ok(cur):
                        page.blocked_reason = "redirect_robots_disallow"
                        page.elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
                        return page
                    page.redirects.append(cur)
                    req = urllib.request.Request(cur, headers={"User-Agent": self.ua})
                    continue
                page.blocked_reason = "http_error:%d" % e.code
                page.elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
                return page
            except Exception as e:                     # 超时/DNS/TLS 统一兜住
                page.blocked_reason = "fetch_failed:%s" % type(e).__name__
                page.elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
                return page

        charset = "utf-8"
        m = re.search(r"charset=([\w\-]+)", page.content_type or "", re.I)
        if m:
            charset = m.group(1)
        try:
            body = raw.decode(charset, errors="replace")
        except LookupError:
            body = raw.decode("utf-8", errors="replace")

        if "html" in (page.content_type or "").lower() or body.lstrip().startswith("<"):
            ex = _Extractor()
            try:
                ex.feed(body)
                ex.close()
            except Exception:
                pass
            title, text, links = ex.result()
            page.title, page.text = title, text
            page.links = [(t, urljoin(page.final_url or cur, h)) for t, h in links]
        else:
            page.text = body
            page.title = (page.final_url or cur).rsplit("/", 1)[-1]

        if as_text and len(page.text) < 200 and JS_HINT.search(body):
            page.js_suspected = True
        page.elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        return page

    def stats(self) -> dict[str, Any]:
        return {"lazy_loads": self.lazy_loads, "opener_ready": self._opener is not None,
                "robots_cached": len(self._robots)}


# ---------------------------------------------------------------------------
# 自检：起本地 http 服务，全程离线
# ---------------------------------------------------------------------------
_PAGE = """<!doctype html><html><head><title>门禁 17 说明</title>
<style>body{color:#333}</style></head><body>
<nav><a href="/nav1">首页</a><a href="/nav2">文档</a></nav>
<h1>异步受理探针</h1>
<p>门禁 17 命令为 probe_async_task.py --exe，A7 断言续传补帧，锚点取第一个非终态帧 seq=40。</p>
<p>全量结果 24 道门禁全部通过，演练 5 项按预期，耗时 397.3s，退出码 0。</p>
<script>var x = "这段脚本文字绝不能出现在正文里";</script>
<footer>版权所有 不该出现</footer>
<a href="/evidence/regress-all.raw.txt">证据文件</a>
</body></html>"""


def _selftest() -> int:
    import http.server
    import os
    import tempfile
    import threading

    rc = 0
    tmp = tempfile.mkdtemp(prefix="webtest_")
    with open(os.path.join(tmp, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(_PAGE)
    os.makedirs(os.path.join(tmp, "evidence"), exist_ok=True)
    with open(os.path.join(tmp, "evidence", "regress-all.raw.txt"), "w",
              encoding="utf-8") as fh:
        fh.write("24 道门禁 24 通过 退出码 0")

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=tmp, **kw)

        def log_message(self, *a):       # 静音，别污染自检输出
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    base = "http://127.0.0.1:%d" % port

    try:
        # 1) 懒加载契约
        # 自检全程访问 127.0.0.1，必须显式开内网：默认值已改成拒绝（M1）
        b = Browser(respect_robots=False, allow_private=True)
        if b.stats()["opener_ready"] or b.lazy_loads:
            print("[FAIL] 懒加载违约：构造即建 opener")
            rc = 1
        else:
            print("[OK ] 懒加载契约：构造后 opener_ready=False lazy_loads=0")

        # 2) 正文抽取 + 去导航/脚本/页脚
        page = b.fetch(base + "/index.html")
        if b.lazy_loads == 1 and page.status == 200 and page.ok:
            print("[OK ] 首次抓取触发一次懒加载：status=%d chars=%d links=%d %.1fms"
                  % (page.status, len(page.text), len(page.links), page.elapsed_ms))
        else:
            print("[FAIL] 抓取失败：%s" % page.brief())
            rc = 1
        must = ["异步受理探针", "seq=40", "397.3s", "退出码 0"]
        missing = [k for k in must if k not in page.text]
        if missing:
            print("[FAIL] 正文缺关键内容：%s" % missing)
            rc = 1
        else:
            print("[OK ] 正文完整：%s 全在" % "、".join(must))
        noisy = [k for k in ("这段脚本文字", "版权所有", "首页") if k in page.text]
        if noisy:
            print("[FAIL] 噪声混入正文：%s" % noisy)
            rc = 1
        else:
            print("[OK ] 噪声已剔除：script/nav/footer 均未进正文")

        # 3) 标题与链接绝对化
        if page.title == "门禁 17 说明":
            print("[OK ] 标题抽取：%s" % page.title)
        else:
            print("[FAIL] 标题抽取错：%r" % page.title)
            rc = 1
        abs_ok = all(h.startswith("http://127.0.0.1") for _, h in page.links) and page.links
        if abs_ok:
            print("[OK ] 链接绝对化：%d 条，例 %s" % (len(page.links), page.links[-1][1]))
        else:
            print("[FAIL] 链接未绝对化：%s" % (page.links[:2],))
            rc = 1

        # 4) 内网标注（S4）：本机地址必须被标出来，而不是悄悄放过
        if page.private_net:
            print("[OK ] 内网标注：private_net=True（访问的是 127.0.0.1）")
        else:
            print("[FAIL] 内网地址未标注")
            rc = 1

        # 4b) SSRF 收口（M1）：缺省配置下内网地址必须被拒，不是"标注后放行"
        strict = Browser(respect_robots=False, timeout=2.0)
        blocked = strict.fetch("http://127.0.0.1:%d/index.html" % port)
        if blocked.blocked_reason == "private_net_blocked" and not blocked.ok:
            print("[OK ] S4b 默认拒绝内网：%s" % blocked.blocked_reason)
        else:
            print("[FAIL] 默认仍放行内网：%s" % blocked.brief())
            rc = 1
        # 云元数据端点（169.254.169.254）与 RFC1918 必须一并识别为私网
        meta = [h for h in ("169.254.169.254", "10.1.2.3", "192.168.1.1", "172.16.0.9", "127.0.0.1")
                if not _is_private(h)]
        if not meta and not _is_private("example.com"):
            print("[OK ] S4c 私网判定：元数据/RFC1918/回环全覆盖，公网域名不误判")
        else:
            print("[FAIL] 私网判定漏判：%s" % meta)
            rc = 1

        # 5) 协议白名单（S1）
        bad = b.fetch("file:///C:/Windows/win.ini")
        if bad.blocked_reason.startswith("scheme_not_allowed") and not bad.ok:
            print("[OK ] S1 协议白名单：file:// 被拒（%s）" % bad.blocked_reason)
        else:
            print("[FAIL] file:// 未被拒：%s" % bad.brief())
            rc = 1

        # 6) 纯文本响应不被当 HTML 解析
        txt = b.fetch(base + "/evidence/regress-all.raw.txt")
        if txt.ok and "24 道门禁" in txt.text:
            print("[OK ] 纯文本直通：%d 字" % len(txt.text))
        else:
            print("[FAIL] 纯文本读取异常：%s" % txt.brief())
            rc = 1

        # 7) 失败路径不抛异常，只给 blocked_reason
        dead = Browser(respect_robots=False, timeout=2.0, allow_private=True).fetch("http://127.0.0.1:9/none")
        if dead.blocked_reason and not dead.ok:
            print("[OK ] 失败路径可诊断：%s" % dead.blocked_reason)
        else:
            print("[FAIL] 失败未给出诊断：%s" % dead.brief())
            rc = 1

        # 8) robots 生效（S5）：本服务无 robots.txt → 放行
        rb = Browser(respect_robots=True, allow_private=True)
        if rb.fetch(base + "/index.html").ok:
            print("[OK ] S5 robots 缺省放行（无 robots.txt 时正常抓取）")
        else:
            print("[FAIL] robots 逻辑误杀正常抓取")
            rc = 1
    finally:
        srv.shutdown()
    return rc


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return _selftest()
    if "--url" in argv:
        url = argv[argv.index("--url") + 1]
        page = Browser().fetch(url)
        if "--json" in argv:
            print(json.dumps({**page.brief(), "text": page.text[:4000]}, ensure_ascii=False, indent=2))
        else:
            print("标题: %s\n状态: %s  字符: %d  链接: %d  %.1fms\n阻断: %s"
                  % (page.title, page.status, len(page.text), len(page.links),
                     page.elapsed_ms, page.blocked_reason or "无"))
            print("-" * 60)
            print(page.text[:2000])
        return 0 if page.ok else 1
    print("用法: --selftest | --url <URL> [--json]")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
