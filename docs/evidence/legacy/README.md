# legacy/ —— 旧命名证据（仅存档，不再更新）

2026-11-11 起，证据文件改由 `scripts/regress_all.py` 在每次全量回归收尾时**同批重写**，
命名统一为 `<序号>-<脚本>-latest.txt`，与总表 `../regress-all.txt` 的时间戳必然一致。

以前的规矩是每个脚本自写 `xxx-latest.txt`，各自被单独运行时刷新。问题在于：
**文档里的数字来自最新一次全量回归，证据文件却可能停在三天前**，于是出现
"正文写 156 项、证据里是 154 项"的错位。这种错位比没有证据更糟——看的人会怀疑
所有数字都是编的。所以：旧命名的文件全部移到这里，只作历史，别再引用。

| 旧文件 | 对应的新文件 |
|---|---|
| `smoke-latest.txt` | `02-smoke-latest.txt` / `03-smoke-latest.txt`（json / memory 两个后端各一份） |
| `swap-test-latest.txt` | `09-matrix-latest.txt` |
| `prove-real-exec-latest.txt` | `08-verify_runner-latest.txt` |
| `check-openapi-latest.txt` | `06-gen-openapi-latest.txt` |
| `check-contract-latest.txt` | `07-check-contract-latest.txt` |
| `gen-ts-latest.txt` | `05-gen-ts-types-latest.txt` |
| `audit-probe-latest.txt` | `11-audit-probe-test-latest.txt` |
| `probe-latest.txt` | 无直接对应：`scripts/probe.py` 是早期换件探针，其职责已被 `matrix.py` 与 `swap_drill.py` 覆盖，脚本仍会写顶层同名文件 |
| `round3-summary.txt` | 历史小结，无对应 |
| `tmp/` | 早期测试留下的 0 字节残留（`robust-*`），未删除，仅收纳 |

需要复现任何一项，跑 `python scripts\regress_all.py`，再回到上一层读对应文件。
