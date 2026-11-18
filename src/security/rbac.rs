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
        // 三方审查低危 1：Admin 与 Owner 行为等价只体现在这一条通配，
        // 原先并列写死的 (Admin, Admin)/(Admin, MemoryWriteSystem) 是不可达分支，
        // 会让人误以为"Admin 能管系统记忆，但别的权限另有所限"。删除冗余，
        // 只留一条通配，语义即"Admin 除 Owner 专属动作外全开"。
        // 若将来要限制 Admin（例如禁止 MemoryWriteSystem），必须把该分支放到
        // 通配**之前**，否则永远不会生效——这是本矩阵唯一的修改纪律。
        (Role::Admin, _) => true,
        (Role::Member, Permission::Read) => true,
        (Role::Member, Permission::Write) => true,
        // 沙箱执行是特权能力：注册即得的 Member 不能直接持有（三方审查 H1）。
        // 需要跑脚本的成员由 Owner 显式提权，而不是默认开通。
        (Role::Member, Permission::SandboxExec) => false,
        (Role::Member, Permission::MemoryWriteShared) => true,
        // 跨 agent 调度是平台编排动作，不作为用户侧特权授予；
        // 编排链路在 executor 里单独豁免（见 executor.rs 第 2 步权力校验）。
        (Role::Member, Permission::CrossAgent) => false,
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
        // H1：注册即得的 Member 不持有沙箱执行与跨 agent 特权
        assert!(!allows(Role::Member, Permission::SandboxExec));
        assert!(!allows(Role::Member, Permission::CrossAgent));
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
