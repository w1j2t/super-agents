# -*- coding: utf-8 -*-
"""门禁：文档里的"具体数字"必须与代码/契约的实时值一致。

为什么需要这道门禁：
`docs/30-ts-frontend-plan.md` 里写着"24 路径 / 28 操作 / 50 schema"和
"9 个 agent"，而 2026-11-16 实测已是 29 / 33 / 51 和 14 个 agent。
29 道门禁里只有这一道会发现这件事 —— 它们验代码对不对，不验文档说的数对不对。
数字过时不会让任何东西报错，只会让接手前端的人怀疑自己。

用法：
    python scripts/check_docs_drift.py            # 校验，有不一致则 rc=1
    python scripts/check_docs_drift.py --drill    # 负向演练：塞一个假真值，必须报警
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = ROOT / 'docs'

# 只查这几篇：它们会被前端/交接者当作"权威数字"引用
WATCH = [
    '30-ts-frontend-plan.md',
    '40-api-contract-ts.md',
    '07-api-reference.md',
    '88-frontend-handoff.md',
    '90-metric-credibility.md',
    '91-split-strategy.md',
]


def _block(text: str, marker: str) -> str:
    """截取 `marker` 所在常量**赋值表达式**的方括号段。

    必须从 `=` 之后开始数方括号：`pub const AGENTS: &[AgentSpec] = &[` 里
    第一个 `[AgentSpec]` 是类型标注，从那里数会立刻配平、截出空块
    （第一版就踩了这个坑，真值全成 0）。
    """
    i = text.find(marker)
    if i < 0:
        return ''
    eq = text.find('=', i)
    if eq < 0:
        return ''
    depth = 0
    started = False
    for j in range(eq, len(text)):
        ch = text[j]
        if ch == '[':
            depth += 1
            started = True
        elif ch == ']':
            depth -= 1
            if started and depth == 0:
                return text[eq:j]
    return text[eq:]


# 这些行是在叙述"以前是多少、后来改成多少"，不该拿当前值去校它
HISTORY_HINTS = ('修订', '原为', '旧值', '→', '->', '当时', '曾经', '改为')

# 局部计数（只算 /api/v1 前缀、只算根路径运维端点）不能拿全局真值去校。
# 文档里凡写局部数，必须带这些限定词，脚本据此把它摘出去；写成全局口径的才校验。
LOCAL_HINTS = ('前缀下', '根路径', '另有', '其中', '不含')


def _strip_local(line: str) -> str:
    """摘掉行内的局部计数片段，只留下"全局口径"的句子。

    实测教训：`C 产出 29 条路径 / 33 个操作 / 51 个 schema（其中 /api/v1 前缀下
    26 条路径 / 30 个操作…）` 这一句里同时有全局数和局部数，不摘局部就会把
    26/30 判成"文档写错"。先摘带括号的局部说明，再摘无括号的局部子句。
    """
    out = re.sub(r'（[^（）]*前缀下[^（）]*）', '', line)
    out = re.sub(r'\([^()]*前缀下[^()]*\)', '', out)
    parts = re.split(r'([，,；;。])', out)
    kept = []
    for seg in parts:
        if seg in ('，', ',', '；', ';', '。'):
            kept.append(seg)
            continue
        if any(h in seg for h in LOCAL_HINTS):
            kept.append(' ')          # 局部子句整段丢弃
        else:
            kept.append(seg)
    return ''.join(kept)


def truth() -> dict:
    t = {}

    spec = json.load(open(ROOT / 'docs' / 'openapi.json', encoding='utf-8'))
    paths = spec['paths']
    t['paths'] = len(paths)
    t['ops'] = sum(1 for v in paths.values() for m in v
                   if m in ('get', 'post', 'put', 'patch', 'delete'))
    t['schemas'] = len(spec['components']['schemas'])

    ts = (ROOT / 'generated' / 'types.generated.ts').read_text(encoding='utf-8')
    t['interface'] = len(re.findall(r'^export interface ', ts, re.M))
    t['union'] = len(re.findall(r'^export type \w+\s*=\s*[^;]*\|', ts, re.M))

    agent_src = (ROOT / 'src' / 'domain' / 'agent.rs').read_text(encoding='utf-8')
    t['agents'] = len(re.findall(r'AgentSpec \{', _block(agent_src, 'pub const AGENTS')))

    cap_src = (ROOT / 'src' / 'domain' / 'capability.rs').read_text(encoding='utf-8')
    t['capabilities'] = len(re.findall(r'CapabilitySpec \{',
                                       _block(cap_src, 'pub const CAPABILITIES')))

    # onboarding：从源码数步数与必填数；解析不出来就置 None，宁可跳过也不误报
    try:
        ob = (ROOT / 'src' / 'domain' / 'onboarding.rs').read_text(encoding='utf-8')
        t['onboarding_steps'] = len(re.findall(r'OnboardingStep \{', _block(ob, 'pub const STEPS')))
        t['onboarding_required'] = len(re.findall(
            r'required:\s*true', _block(ob, 'pub const STEPS'))) or None
        if not t['onboarding_steps']:
            t['onboarding_steps'] = None
    except OSError:
        t['onboarding_steps'] = None
        t['onboarding_required'] = None
    return t


# (正则, 真值键, 人话名) —— 数字后面必须跟着明确的名词，减少误报
PATTERNS = [
    (r'(\d+)\s*条路径', 'paths', '路径数'),
    (r'(\d+)\s*个操作', 'ops', '操作数'),
    (r'(\d+)\s*个\s*schema', 'schemas', 'schema 数'),
    (r'(\d+)\s*个\s*interface', 'interface', 'interface 数'),
    (r'(\d+)\s*个\s*联合类型', 'union', '联合类型数'),
    (r'(\d+)\s*个\s*agent', 'agents', 'agent 数'),
    (r'(\d+)\s*个卡片', 'agents', 'agent 卡片数'),
    (r'(\d+)\s*项能力', 'capabilities', '能力项数'),
    (r'(\d+)\s*项机制', 'capabilities', '能力机制数'),
]


def main() -> int:
    drill = '--drill' in sys.argv
    t = truth()
    if drill:
        # 负向演练：把真值改错，本脚本必须报出不一致，否则说明它根本没在看
        t = dict(t, paths=999, agents=999)

    print('实时真值: ' + ' | '.join('%s=%s' % (k, v) for k, v in sorted(t.items())
                                if v is not None))
    bad = []
    checked = 0
    for name in WATCH:
        p = DOCS / name
        if not p.exists():
            continue
        for lineno, line in enumerate(p.read_text(encoding='utf-8').splitlines(), 1):
            if any(h in line for h in HISTORY_HINTS):
                continue
            line = _strip_local(line)
            for pat, key, label in PATTERNS:
                real = t.get(key)
                if real is None:
                    continue
                for m in re.finditer(pat, line):
                    claimed = int(m.group(1))
                    checked += 1
                    if claimed != real:
                        bad.append((name, lineno, label, claimed, real, m.group(0)))

    print('扫描文档 %d 篇，命中数字断言 %d 处' % (sum(1 for n in WATCH if (DOCS / n).exists()), checked))

    # 演练档的退出码语义与正常档**相反**，这是总控 regress_all.py 定死的口径：
    # drill=True 时 status = DRILL-OK 当且仅当 rc == 0。
    # 而演练成功的含义是"该红的红了"，不是"一切正常"——所以抓到错必须报 0。
    # 这里曾经写成"抓到就 return 1"，导致演练被记成 DRILL-FAIL、
    # 整轮结论判"演练全绿不可信"，而脚本其实抓得好好的：**判据没问题，接口对不上**。
    # 这类 bug 不会让任何断言变红，只会让"门禁有没有判定力"这件事永远说不清。
    if drill:
        if bad:
            print('DRILL-OK 负向演练按预期变红（这是好事）：真值强行改成 999 后，'
                  '%d 处数字断言当场报不一致，说明它在真看数字' % len(bad))
            for name, lineno, label, claimed, real, frag in bad[:6]:
                print('  %s:%d  %s 写成 %d，实际 %d' % (name, lineno, label, claimed, real))
            return 0
        print('DRILL-FAIL 真值被改成 999，文档里的数字却全都对得上——'
              '说明这道门禁根本没在比数字，已失去判定力')
        return 2

    if bad:
        print('FAIL 文档数字与代码不一致 %d 处：' % len(bad))
        for name, lineno, label, claimed, real, frag in bad:
            print('  %s:%d  %s 写成 %d，实际 %d（原文 “%s”）'
                  % (name, lineno, label, claimed, real, frag))
        return 1
    print('PASS 文档数字与代码/契约一致')
    return 0


if __name__ == '__main__':
    sys.exit(main())
