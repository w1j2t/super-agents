//! Personal Super Agents 后端库。
//!
//! 分层：`api`（HTTP）→ `services`（用例）→ `orchestration`（编排/执行治理）
//! → `domain`（契约与知识目录）→ `store`（快照存储）；横切能力在 `security`、`obs`。

// cli 是 `api` 的同级薄壳：只做参数解析与只读自检，不含任何业务规则。
pub mod api;
pub mod cli;
pub mod config;
pub mod domain;
pub mod error;
pub mod obs;
pub mod orchestration;
pub mod ports;
pub mod security;
pub mod services;
pub mod state;
pub mod store;
