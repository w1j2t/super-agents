---
name: gate-keeper
description: 跑门禁、读证据、判定"这次改动到底绿没绿"的标准流程，专治假绿与假红。
version: 1.0.0
tags: [验收, 门禁, 证据]
requires: []
---

# 门禁看守（gate-keeper）

## 什么时候用
改完 Rust / Python 代码，准备说"做完了"之前。**没有证据就不算做完。**

## 标准流程
1. 形式审计：`python scripts/audit_agents.py`，期望 `rc=0`；
2. 文档漂移：`python scripts/gen-spec-docs.py --check`，期望 `rc=0`；
3. 单元守护：`cargo +stable-x86_64-pc-windows-gnu test`；
4. 端到端：`python scripts/regress_all.py --with-drills`。

第 4 步耗时约 7 分钟，前台跑容易被工具超时打断，正确姿势是后台重定向写日志再轮询：

    Start-Process -FilePath python -ArgumentList "scripts\regress_all.py","--with-drills" `
      -RedirectStandardOutput "temp\regress.log" -RedirectStandardError "temp\regress.err" -NoNewWindow

## 怎么读结论
- 只看 `EXIT=` 与末尾的"合计 N 道门禁 | 通过 N | 失败 0 | 跳过 0"；
- `S K I P` 绝不等于通过，退出码 `3` 是"环境不具备"，要在汇报里单独说明；
- 演练段要看"按预期变红 / 未按预期"，出现"未按预期"= 门禁失去判定力，比红还严重。

## 常见假绿
| 现象 | 真相 |
|---|---|
| 全绿但测的是旧 exe | 新鲜度判据被 mtime 骗了，要内容指纹 |
| 演练行显示 FAIL | 摘要口径复用了门禁的 tail，抽不到演练结论行 |
| ratio=1.00 看着很美 | 压缩其实没发生，只是没丢东西 |
