//! 业务服务层：HTTP 层只做参数映射，业务规则全部收敛在这里。

pub mod auth;
pub mod cluster_registry;
pub mod cluster_view;
