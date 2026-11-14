#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""workers/_llm.py —— 可选的外部模型通道（OpenAI 兼容协议）。

## 为什么是这样写的（每条都来自实测，不是猜的）

1. **纯标准库**。worker 的立场是"拷到任何一台机器上就能跑"，装依赖才能跑等于没做，
   所以不引入 requests / httpx，直接用 urllib。

2. **默认关闭**。不配 `SA_LLM_BASE` + `SA_LLM_MODEL` 就一个字节都不往外发，
   规则实现照常工作。于是冒烟、矩阵、单测都不依赖"本机装没装模型服务"，
   也不会因为某台机器没拉模型就红一片。

3. **推理模型不是普通模型**。实测 `minicpm5-2b:f16`（面壁 MiniCPM5-2B，f16）：
   176 字正文消耗了 794 个 token 的思维链，思维链单独放在响应的 `reasoning` 字段。
   如果按"max_tokens 就是正文预算"来配（比如 400），结果是 content 为空、
   finish_reason=length——看起来像模型坏了，其实是被自己的思考吃光了预算。
   本模块因此把 reasoning / content 分开上报，并在"预算被思考吃光"时自动加预算重试一次。

4. **失败不抛给主流程**。返回结构化结果，由 worker 决定回退还是硬失败；
   抛异常会让 `_common.main` 把整步判成可重试失败，而"模型输出不合规"重试十次也一样。

## 环境变量（都由 ProcessRunner 按 `SA_LLM_` 前缀透传）

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `SA_LLM_BASE` | 是 | — | OpenAI 兼容根地址，如 `http://127.0.0.1:11434/v1` |
| `SA_LLM_MODEL` | 是 | — | 模型名，如 `minicpm5-2b:f16` |
| `SA_LLM_API_KEY` | 否 | — | 本地 Ollama 不需要；云服务填 |
| `SA_LLM_TIMEOUT` | 否 | 180 | 单次请求秒数。**必须低于 runner 的单步硬超时** |
| `SA_LLM_MAX_TOKENS` | 否 | 1600 | 含思考在内的总预算，不是正文长度 |
| `SA_LLM_BUDGET_CEIL` | 否 | 4000 | 预算翻倍重试的上限 |
| `SA_LLM_JSON_MODE` | 否 | 0 | 1 = 启用 response_format=json_object（提升严格 JSON 合规率） |
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from typing import Any


# ---------------------------------------------------------------- 配置读取

def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int, low: int, high: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return max(low, min(high, int(raw)))
    except ValueError:
        # 配错就回落，不让一个笔误把步骤打死
        return default


def enabled() -> bool:
    """是否接入外部模型。两个必填项缺一即视为"未接入"。"""
    return bool(_env("SA_LLM_BASE") and _env("SA_LLM_MODEL"))


def describe() -> dict[str, Any]:
    """对外可披露的配置摘要——**绝不含 API Key 明文**，只说明带没带。"""
    base = _env("SA_LLM_BASE")
    return {
        "enabled": enabled(),
        "model": _env("SA_LLM_MODEL"),
        "base": base,
        "has_api_key": bool(_env("SA_LLM_API_KEY")),
        "timeout_secs": _env_int("SA_LLM_TIMEOUT", 180, 5, 590),
        "max_tokens": _env_int("SA_LLM_MAX_TOKENS", 1600, 64, 8000),
        "json_mode": _env("SA_LLM_JSON_MODE") == "1",
    }


# ---------------------------------------------------------------- 调用

def _url() -> str:
    base = _env("SA_LLM_BASE").rstrip("/")
    return base + "/chat/completions"


def _schema_unsupported(detail: str) -> bool:
    """从 400 的响应正文里判断"是不是因为不支持结构化输出"。"""
    d = (detail or "").lower()
    return any(k in d for k in ("response_format", "json_schema", "unsupported", "not support"))


def chat(
    prompt: str,
    system: str | None = None,
    max_tokens: int | None = None,
    temperature: float = 0.2,
    allow_retry: bool = True,
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """调用一次模型，返回结构化结果（不抛异常）。

    参数：
        schema  可选 JSON Schema。给了就走 response_format=json_schema 强约束
                （服务端按 grammar 解码，从根上杜绝重复键/缺字段/尾随废话）。
                这一层比"事后解析补救"可靠得多：实测同一个 2B 模型，
                不加约束时 12/12 轮结构不合法，加了以后 1 次就合法。

    返回字段：
        ok               bool    是否拿到非空 content
        content          str     正文（不含思维链）
        reasoning_chars  int     思维链字符数（用于诊断"预算被思考吃光"）
        finish           str     stop / length
        completion_tokens int
        elapsed_ms       int
        attempts         int     实际发起的请求数
        error            str|None  失败分类：未配置/unreachable/timeout/http_xxx/empty_content
    """
    if not enabled():
        return _fail("not_configured")

    cfg = describe()
    budget = max_tokens or cfg["max_tokens"]
    ceil = _env_int("SA_LLM_BUDGET_CEIL", 4000, 128, 16000)
    cert = _env("SA_LLM_TIMEOUT")
    timeout = _env_int("SA_LLM_TIMEOUT", 180, 5, 590) if cert else cfg["timeout_secs"]

    attempts = 0
    started = time.time()
    last: dict[str, Any] = _fail("no_attempt")

    while True:
        attempts += 1
        last = _once(prompt, system, budget, temperature, timeout, schema)
        last["attempts"] = attempts
        last["elapsed_ms"] = int((time.time() - started) * 1000)

        if last["ok"]:
            return last

        # 服务端不认 json_schema（老版本 OpenAI 兼容层会直接 400）时降级重来一次，
        # 而不是让整条模型路径报废。只在错误信息确实指向 response_format / schema
        # 时才降级——否则"模型名写错"这类真错误也会被当成"不支持"吞掉。
        if schema and last["error"] == "http_400" and _schema_unsupported(last.get("detail") or ""):
            schema = None
            attempts = 0
            started = time.time()
            continue

        # 硬失败（连不上 / HTTP 错）重试没有意义，只会把超时耗光
        if last["error"] in ("unreachable", "timeout") or str(last["error"] or "").startswith("http_"):
            return last

        # 预算被思考吃光：这是推理模型的专属失败形态，加预算再要一次才有意义
        ate_budget = last["finish"] == "length" and (last["reasoning_chars"] > 0)
        if not (allow_retry and ate_budget):
            return last
        nxt = min(ceil, budget * 2)
        if nxt <= budget:
            return last  # 已到上限，重试是空转
        budget = nxt


def _once(
    prompt: str,
    system: str | None,
    max_tokens: int,
    temperature: float,
    timeout: int,
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    body: dict[str, Any] = {
        "model": _env("SA_LLM_MODEL"),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    # 三级强度：schema 强约束 > json_object（只保证是合法 JSON，不管字段） > 不加约束
    # 为什么分成三档而不是一律上 schema：schema 需要服务端支持 grammar 解码
    # （Ollama 0.14+ / vLLM 均支持），老版本兼容层会直接 400。给运维留降级路径。
    if schema:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "sa_structured", "strict": True, "schema": schema},
        }
    elif _env("SA_LLM_JSON_MODE") == "1":
        body["response_format"] = {"type": "json_object"}

    headers = {"Content-Type": "application/json"}
    key = _env("SA_LLM_API_KEY")
    if key:
        headers["Authorization"] = "Bearer " + key

    req = urllib.request.Request(
        _url(), data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        return _fail(f"http_{exc.code}", extra={"detail": detail, "finish": ""})
    except (socket.timeout, TimeoutError):
        return _fail("timeout", extra={"finish": ""})
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return _fail("timeout", extra={"finish": ""})
        return _fail("unreachable", extra={"detail": str(reason)[:200], "finish": ""})
    except (ValueError, KeyError) as exc:
        return _fail("bad_response", extra={"detail": repr(exc)[:200], "finish": ""})

    try:
        choice = payload["choices"][0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        reasoning = message.get("reasoning") or message.get("reasoning_content") or ""
        finish = choice.get("finish_reason") or ""
        usage = payload.get("usage") or {}
    except (KeyError, IndexError, TypeError) as exc:
        return _fail("bad_response", extra={"detail": repr(exc)[:200], "finish": ""})

    ok = bool(content.strip())
    return {
        "ok": ok,
        "content": content.strip(),
        "reasoning_chars": len(reasoning),
        "finish": finish,
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "attempts": 1,
        "elapsed_ms": 0,
        "error": None if ok else "empty_content",
    }


def _fail(error: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": False,
        "content": "",
        "reasoning_chars": 0,
        "finish": "",
        "completion_tokens": 0,
        "prompt_tokens": 0,
        "attempts": 0,
        "elapsed_ms": 0,
        "error": error,
    }
    if extra:
        out.update(extra)
    return out


# ---------------------------------------------------------------- 结构化解析
#
# 纪律：**去噪声可以，"聪明修复"不可以**。
#   - 允许：剥掉模型加的包裹层（``` 围栏、`"""` 三引号）、从"JSON + 后接废话"里
#     按花括号配平取出第一个完整对象。这些操作不改变对象里任何一个字节。
#   - 禁止：补引号、补花括号、猜键名、容忍重复键。那会把残缺结构伪装成完整结构，
#     悄悄丢掉一半内容比直接报错更坏。
#
# 这条分界线是实测划出来的：2B 模型 7/7 轮把 JSON 包在 `"""` 里、后面还接一段
# 自说自话（"用户现在……"），旧解析器只认 ``` 围栏，于是在位置 2 报 Extra data，
# 全域回退到规则骨架——模型白跑，接口看起来还是 succeeded。

_WRAP_TOKENS = ("```", '"""', "'''")


def _strip_wrappers(raw: str) -> str:
    """剥掉模型爱加的包裹层。只脱外壳，不碰内容。"""
    text = raw.strip()
    changed = True
    while changed:
        changed = False
        for tok in _WRAP_TOKENS:
            if text.startswith(tok):
                text = text[len(tok) :].lstrip()
                changed = True
            if text.endswith(tok):
                text = text[: -len(tok)].rstrip()
                changed = True
        # 围栏常带语言标注：```json
        if text[:5].lower() == "json\n":
            text = text[5:].lstrip()
            changed = True
    return text


def _scan_first_object(text: str) -> tuple[str | None, str]:
    """按花括号配平取出第一个完整对象（字符串感知，处理转义引号）。

    未闭合一律判失败——多半是输出被 max_tokens 截断，这种情况必须让上层看见，
    不能猜一个右花括号把结构凑出来。
    """
    start = text.find("{")
    if start < 0:
        return None, "输出里没有 JSON 对象"
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1], ""
    return None, "花括号未闭合（输出可能被截断）"


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """重复键直接判错：json 默认"后者覆盖前者"，等于静默丢内容。"""
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"重复键「{key}」")
        seen[key] = value
    return seen


def _loads_strict(text: str) -> tuple[dict[str, Any] | None, str]:
    try:
        obj = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except ValueError as exc:  # 重复键
        return None, f"JSON 不可信：{exc}"
    except json.JSONDecodeError as exc:
        return None, f"JSON 不合法：{exc.msg}（位置 {exc.pos}）"
    if not isinstance(obj, dict):
        return None, f"顶层不是对象，而是 {type(obj).__name__}"
    return obj, ""


def parse_json_object(text: str) -> tuple[dict[str, Any] | None, str]:
    """从模型输出里取出一个 JSON 对象，返回 (对象, 失败原因)。

    两跳：先按"干净输出"直接解析；不成就剥壳 + 配平提取再解析。
    两跳都失败时，报第二跳的原因（它更接近真相：是被截断还是压根没对象）。
    """
    if not text or not text.strip():
        return None, "空输出"

    raw = _strip_wrappers(text)
    if not raw:
        return None, "空输出"

    obj, why = _loads_strict(raw)
    if obj is not None:
        return obj, ""

    obj_text, scan_why = _scan_first_object(raw)
    if obj_text and obj_text != raw:
        obj2, why2 = _loads_strict(obj_text)
        if obj2 is not None:
            return obj2, ""
        return None, why2
    if scan_why and "没有 JSON 对象" not in scan_why:
        return None, scan_why
    return None, why
