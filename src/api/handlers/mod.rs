//! HTTP 处理器：只做参数映射与状态码，业务规则在 domain / services 层。

pub mod auth;
pub mod catalog;
pub mod cluster;
pub mod memory;
pub mod observability;
pub mod onboarding;
pub mod task;
pub mod task_events;
