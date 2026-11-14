//! 角色与权限模型：所有"能做/不能做"的判定都收敛到这里，便于审计与演进。

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    Owner,
    Admin,
    Member,
}

impl Role {
    pub fn as_str(&self) -> &'static str {
        match self {
            Role::Owner => "owner",
            Role::Admin => "admin",
            Role::Member => "member",
        }
    }

    pub fn parse(s: &str) -> Option<Role> {
        match s.to_ascii_lowercase().as_str() {
            "owner" => Some(Role::Owner),
            "admin" => Some(Role::Admin),
            "member" => Some(Role::Member),
            _ => None,
        }
    }

    /// 系统里首个注册用户为 owner，其余为 member；owner 可后续提升他人。
    pub fn rank(&self) -> u8 {
        match self {
            Role::Owner => 3,
            Role::Admin => 2,
            Role::Member => 1,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Permission {
    /// 读取自己的资源
    Read,
    /// 写入自己的资源（任务、私有记忆、教程进度）
    Write,
    /// 触发沙箱执行（代码/浏览器/工具）
    SandboxExec,
    /// 写 shared 作用域共享记忆
    MemoryWriteShared,
    /// 写 system 作用域记忆、管理全局注册表
    MemoryWriteSystem,
    /// 跨 agent 编排调用
    CrossAgent,
    /// 管理用户、查看全局审计
    Admin,
}

impl Permission {
    pub fn as_str(&self) -> &'static str {
        match self {
            Permission::Read => "read",
            Permission::Write => "write",
            Permission::SandboxExec => "sandbox_exec",
            Permission::MemoryWriteShared => "memory_write_shared",
            Permission::MemoryWriteSystem => "memory_write_system",
            Permission::CrossAgent => "cross_agent",
            Permission::Admin => "admin",
        }
    }
}

/// 权力分配矩阵：唯一真源，文档与代码必须一致。
pub fn allows(role: Role, perm: Permission) -> bool {
    match (role, perm) {
        (Role::Owner, _) => true,
        (Role::Admin, Permission::Admin) => true,
        (Role::Admin, Permission::MemoryWriteSystem) => true,
        (Role::Admin, _) => true,
        (Role::Member, Permission::Read) => true,
        (Role::Member, Permission::Write) => true,
        (Role::Member, Permission::SandboxExec) => true,
        (Role::Member, Permission::MemoryWriteShared) => true,
        (Role::Member, Permission::CrossAgent) => true,
        (Role::Member, Permission::MemoryWriteSystem) => false,
        (Role::Member, Permission::Admin) => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn member_cannot_admin() {
        assert!(!allows(Role::Member, Permission::Admin));
        assert!(!allows(Role::Member, Permission::MemoryWriteSystem));
        assert!(allows(Role::Member, Permission::Write));
    }

    #[test]
    fn owner_can_all() {
        for p in [
            Permission::Read,
            Permission::Write,
            Permission::SandboxExec,
            Permission::MemoryWriteShared,
            Permission::MemoryWriteSystem,
            Permission::CrossAgent,
            Permission::Admin,
        ] {
            assert!(allows(Role::Owner, p));
        }
    }
}
