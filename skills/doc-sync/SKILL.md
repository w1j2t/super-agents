---
name: doc-sync
description: 改完代码同步文档的规定动作，避免"文档比源码超前"或"源码改了文档没跟上"。
version: 1.1.0
tags: [文档, 规范]
requires: []
---

# 文档同步（doc-sync）

## 三条铁律
1. **生成物不许手改**：`docs/02`、`docs/03`、`docs/07` 由 `gen-spec-docs.py` 产出，改了会在 `--check` 里报漂移；
2. **判据先落纸面**：新功能先在 `docs/06-acceptance-criteria.md` 写清"怎样算过"，再写代码；
3. **进展倒序**：`docs/84-feature-gap-audit.md` §9 按时间倒序追加，最新在最上面，写清踩坑与复盘。

## 编号约定
| 文件 | 职责 |
|---|---|
| `docs/06` | 验收标准正文 |
| `docs/84` | 功能落差与进展流水 |
| `docs/86` | 垂直 agent 准入判据 |
| `docs/87` | CLI 客户端口径 |
| `docs/evidence/` | 每次门禁的证据原文 |

## 写完自查
- 新门禁是否已登记进 `regress_all.py` 的编号注释、`TOTAL_GATES`、分层 `TIERS` 三处？漏一处会让 `--tier full` 直接 `rc=2`；
- 文档里引用的路径是否真实存在（死链检查在 `cargo test` 里有守护）。
