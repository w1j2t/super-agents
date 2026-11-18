//! 用户与会话模型。

use crate::security::rbac::Role;
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum UserStatus {
    Active,
    Disabled,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct User {
    pub id: String,
    pub username: String,
    pub username_lower: String,
    pub email: String,
    pub email_lower: String,
    pub display_name: String,
    pub password_hash: String,
    pub role: Role,
    pub status: UserStatus,
    pub locale: String,
    pub timezone: String,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
    pub last_login_at: Option<DateTime<Utc>>,
    /// 登录失败计数，用于渐进式锁定（连续失败 5 次锁 15 分钟）
    pub failed_login_count: u32,
    pub locked_until: Option<DateTime<Utc>>,
}

/// 对外视图：绝不包含 password_hash 等敏感字段。
#[derive(Debug, Clone, Serialize)]
pub struct UserView {
    pub id: String,
    pub username: String,
    pub email: String,
    pub display_name: String,
    pub role: Role,
    pub status: UserStatus,
    pub locale: String,
    pub timezone: String,
    pub created_at: DateTime<Utc>,
    pub last_login_at: Option<DateTime<Utc>>,
}

impl From<&User> for UserView {
    fn from(u: &User) -> Self {
        UserView {
            id: u.id.clone(),
            username: u.username.clone(),
            email: u.email.clone(),
            display_name: u.display_name.clone(),
            role: u.role,
            status: u.status,
            locale: u.locale.clone(),
            timezone: u.timezone.clone(),
            created_at: u.created_at,
            last_login_at: u.last_login_at,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Session {
    pub id: String,
    pub user_id: String,
    pub refresh_fingerprint: String,
    pub created_at: DateTime<Utc>,
    pub expires_at: DateTime<Utc>,
    pub revoked_at: Option<DateTime<Utc>>,
    /// 撤销原因。老数据没有这个字段，`serde(default)` 按 `None` 读入。
    ///
    /// 撤销有两种完全不同的含义，混在一起就会两头不讨好：
    /// - `logout` / `password`：这次登录结束了。手里那个还没过期的 access 必须立刻作废，
    ///   否则"已登出"只是错觉（SA-013）。
    /// - `rotated`：只是 refresh 令牌换了一代，人还是同一次登录。把 access 一起打断的话，
    ///   浏览器多标签、CLI 常驻进程这些"刷新与请求并发"的场景会互相踩，自己把自己踢下线。
    #[serde(default)]
    pub revoked_reason: Option<String>,
    /// refresh 轮换时指向"下一代"会话的 id。
    ///
    /// 轮换必然换出一条新会话记录（旧记录留着才能识别重放），而 access 令牌里的
    /// `sid` 是签发当时那一代的 id。若只看自己被撤销就放行/拒绝，会出现两个反向错误：
    /// - 拿到旧一代 access 的人，在轮换后永远读不到新一代的撤销（登出后 access 仍能用）；
    /// - 直接把 rotated 当失效，又会让并发刷新把自己的请求打断。
    /// 因此 `rotated` 不算终点，要顺着这条链走到最新一代再判死活。
    #[serde(default)]
    pub successor_id: Option<String>,
    pub user_agent: String,
    pub client_ip: String,
}

/// `revoked_reason` 取值：refresh 轮换导致的撤销。
pub const REVOKE_ROTATED: &str = "rotated";
/// `revoked_reason` 取值：用户主动登出。
pub const REVOKE_LOGOUT: &str = "logout";
/// `revoked_reason` 取值：改密导致的全量撤销。
pub const REVOKE_PASSWORD: &str = "password";

impl Session {
    pub fn is_active(&self, now: DateTime<Utc>) -> bool {
        self.revoked_at.is_none() && self.expires_at > now
    }

    /// 绑定在本会话上的 access 令牌还能不能用。
    ///
    /// 与 `is_active` 的差别只有一处：被**轮换**撤销过的会话仍放行（见 `revoked_reason`）。
    /// 过期判断照旧，所以最长也就撑到本会话的 refresh 有效期。
    pub fn access_still_valid(&self, now: DateTime<Utc>) -> bool {
        self.expires_at > now
            && (self.revoked_at.is_none()
                || self.revoked_reason.as_deref() == Some(REVOKE_ROTATED))
    }

    /// 顺着轮换链找到这次登录的**最新一代**会话 id。
    ///
    /// `sessions` 传空表也能用：调用方只关心"链尾是谁"。
    pub fn latest_in_lineage<'a, F>(&'a self, mut next: F) -> &'a Session
    where
        F: FnMut(&str) -> Option<&'a Session>,
    {
        let mut cur = self;
        // 理论上链是有向无环的，但数据被外部改坏也不能转圈：加个上限兜底。
        for _ in 0..64 {
            if cur.revoked_reason.as_deref() != Some(REVOKE_ROTATED) {
                return cur;
            }
            let Some(nid) = cur.successor_id.as_deref() else {
                return cur;
            };
            let Some(n) = next(nid) else {
                return cur;
            };
            cur = n;
        }
        cur
    }

    /// 链上是否还有效：走到最新一代再看它是否活着。
    pub fn lineage_alive<'a, F>(&'a self, now: DateTime<Utc>, next: F) -> bool
    where
        F: FnMut(&str) -> Option<&'a Session>,
    {
        self.latest_in_lineage(next).access_still_valid(now)
    }
}
