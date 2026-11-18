# super-agents

自托管的个人智能体编排后端。核心主张很简单：**你的数据不出你的机器，你的智能体由你调度。**

一句话定位：把「一个能读文件、能跑命令、能写文档的 AI 助手」做成一套跑在自己电脑上的服务，账户、会话、任务、密钥全部落在本地 `data/` 目录，作者不运营任何服务器。

---

## 这个项目是什么

一套 Rust 后端（axum），负责把用户的一句话委托拆成子任务，路由给合适的垂直智能体，执行、评审、汇总，并把全过程留痕。

- **14 个智能体**：1 个总控 + 6 个通用职能（代码、文档、研究、规划、构思、叙事）+ 4 个垂直域（游戏设计、财务核算、法务合同、数据分析）+ 市场、质量、守卫。
- **路由而非闲聊**：每个智能体有明确的 `task_types` / `keywords` / 知识库 / 沙箱画像。没有可评审产物的域不配单独立册（判据见 `docs/86-vertical-agents.md`）。
- **本地优先**：默认连本机模型（Ollama）。想用云端模型也可以，改配置即可——但那时数据会离开你的机器，请先读 `PRIVACY.md`。

## 安全模型（先看这段再部署）

| 你的担心 | 这个项目怎么处理 |
| --- | --- |
| 别人能调我的接口吗 | JWT 鉴权；**注册默认关闭**，只有空库首装才放行一次自举 |
| 口令人家不会爆破吗 | 按「主体 + 来源 IP」退避，1s 起翻倍封顶 900s，退避期带 `Retry-After` 头 |
| 会不会被拿去枚举账户 | 账户不存在、口令错、第 5 次失败，响应完全一致 |
| 智能体会乱删我文件吗 | 每个智能体有沙箱画像（只读 / 仓库读写 / 文档读写 / 禁执行）+ 路径守卫 |
| 密钥被同机其他用户读走 | 密钥落盘后收紧权限，Windows 用 `icacls` 只留当前用户，类 Unix `0600` |
| 磁盘被拿走呢 | **防不住**。请开全盘加密。详见 `SECURITY.md` 的威胁模型 |

诚实说明：这个项目不假装能防住已经拿到系统管理员权限的人。想清楚这一点再决定拿它存什么。

## 快速开始

```bash
# 1. 编译
cargo build --release

# 2. 首装自举：只在数据库为空、且没有 registration.lock 时生效
SA_ALLOW_REGISTRATION=1 cargo run --release

# 3. 注册完第一个账户后，去掉上面那个环境变量再重启
#    此后注册接口关闭，别人拿不到新账户
cargo run --release
```

默认监听回环地址。**不要把端口裸奔到公网**：若必须对外，请放在反向代理后面并配 TLS。

## 目录结构

```
src/             Rust 后端（domain / orchestration / ports / adapters）
workers/         各智能体的 Python 执行体与公共件（_llm / _rag / _web / _skills）
scripts/         门禁与运维脚本（regress_all.py / pathguard.py / sanitize_privacy.py ...）
docs/            设计与验收文档，evidence/ 是门禁原始留痕
skills/          技能包（SKILL.md 形态）
marketplace/     技能与插件市场目录
data/            运行时数据（已 gitignore，绝不要提交）
```

## 门禁

改动前后都该跑：

```bash
python scripts/regress_all.py --tier quick    # 提交前
python scripts/regress_all.py --tier full     # 宣称「全绿」前
```

`full` 通过才叫全绿。单跑 `cargo test --lib` 通过不构成任何结论。

## 隐私与合规

- **数据不上传**：没有遥测、没有崩溃上报、没有使用统计。断网可用（除调用云端模型的功能）。
- **证据脱敏**：`docs/evidence/` 里的本机路径与用户名已由 `scripts/sanitize_privacy.py` 归一化为占位符。你在自己机器上重跑门禁后，提交前请先跑一次：

  ```bash
  python scripts/sanitize_privacy.py --apply
  python scripts/sanitize_privacy.py --check   # 有残留则退出码 1
  ```

- 完整说明见 [`PRIVACY.md`](PRIVACY.md)（数据流向）与 [`SECURITY.md`](SECURITY.md)（威胁模型、漏洞报告）。

## 许可与协议

- 代码：[MIT](LICENSE) —— 可商用、可改、可再分发，保留版权声明即可。
- 使用行为：[用户协议](TERMS.md)。
- 数据与隐私：[隐私说明](PRIVACY.md)。
- 安全问题：[安全策略](SECURITY.md)。

## 状态

活跃开发中，仅对 main 分支提供安全修复。这是一个个人项目：作者自己每天在用，接口与内部实现仍可能变动，但**对外 API 路径、状态码、字段不做破坏性修改**——这是硬约束。
