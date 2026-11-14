//! 认证服务：注册、登录（含渐进式锁定）、刷新令牌轮换、登出、会话管理。

use crate::config::Config;
use crate::domain::onboarding::OnboardingProgress;
use crate::domain::user::{Session, User, UserStatus, UserView};
use crate::error::{validation_error, ApiError};
use crate::obs::audit::AuditSink;
use crate::obs::metrics::MetricsExporter;
use crate::security::jwt::{self, JwtError};
use crate::security::password;
use crate::security::ratelimit::{Limiter, Quota};
use crate::security::rbac::Role;
use crate::store::Store;
use chrono::{Duration, Utc};
use serde::{Deserialize, Serialize};

/// 注册：每 IP 每小时 10 次
const REGISTER_QUOTA: Quota = Quota::new(10.0, 10.0 / 3600.0);
/// 登录：每 IP+账号 每分钟 10 次
const LOGIN_QUOTA: Quota = Quota::new(10.0, 10.0 / 60.0);
/// 刷新：每分钟 30 次（正常前端每次过期才刷一次）
const REFRESH_QUOTA: Quota = Quota::new(30.0, 30.0 / 60.0);

const MAX_FAILED_LOGINS: u32 = 5;
const LOCK_MINUTES: i64 = 15;
/// 账号不存在时同样执行一次哈希校验，避免通过响应时间探测账号是否注册。
const DUMMY_HASH: &str = "pbkdf2_sha256$1$c2FsdHNhbHRzYWx0c2FsdA==$aGFzaGhhc2hoYXNoaGFzaGhhc2hoYXNoaGFzaGhhc2g=";

#[derive(Debug, Deserialize)]
pub struct RegisterInput {
    pub username: String,
    pub email: String,
    pub password: String,
    #[serde(default)]
    pub display_name: Option<String>,
    #[serde(default)]
    pub locale: Option<String>,
    #[serde(default)]
    pub timezone: Option<String>,
}

/// 登录入参。
///
/// 三个"登录名"字段是**同一件事的三种叫法**，不是三个不同凭据：
/// - `account`：契约主字段，用户名或邮箱都行；
/// - `username` / `email`：兼容别名。前端常常把注册表单的对象直接复用到登录，
///   或者手上只有邮箱，硬要它改键名只会换来一次 401 和一段无意义的排查。
///
/// 服务端按 `account → username → email` 取第一个非空值；三个都空才 422。
/// 三个字段都不进 required（否则前端必须三选一填三次），真正的必填关系
/// 用 schema 的 `anyOf` 表达：`account|username|email` 任给其一 + `password`。
#[derive(Debug, Deserialize)]
pub struct LoginInput {
    /// 登录名：用户名或邮箱（主字段）
    #[serde(default)]
    pub account: Option<String>,
    /// `account` 的兼容别名
    #[serde(default)]
    pub username: Option<String>,
    /// `account` 的兼容别名
    #[serde(default)]
    pub email: Option<String>,
    pub password: String,
}

impl LoginInput {
    /// 取出实际用于查库的登录名（已 trim + 小写）。
    ///
    /// 只做取值与规范化，不做"是否存在"的判断——那属于服务层，
    /// 并且必须走恒定时间路径（见 `AuthService::login`）。
    pub fn login_name(&self) -> Option<String> {
        [&self.account, &self.username, &self.email]
            .into_iter()
            .filter_map(|v| v.as_deref())
            .map(str::trim)
            .find(|s| !s.is_empty())
            .map(str::to_lowercase)
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct AuthTokens {
    pub access_token: String,
    pub refresh_token: String,
    pub token_type: &'static str,
    pub expires_in: i64,
}

#[derive(Debug, Clone, Serialize)]
pub struct AuthResult {
    pub user: UserView,
    pub tokens: AuthTokens,
    pub onboarding_status: &'static str,
    pub onboarding_next_step: Option<&'static str>,
}

pub struct AuthService<'a> {
    pub config: &'a Config,
    pub store: &'a Store,
    pub metrics: &'a dyn MetricsExporter,
    pub audit: &'a dyn AuditSink,
    pub limiter: &'a dyn Limiter,
}

impl<'a> AuthService<'a> {
    pub fn register(
        &self,
        input: RegisterInput,
        client_ip: &str,
        user_agent: &str,
    ) -> Result<AuthResult, ApiError> {
        if !self.config.allow_registration {
            return Err(ApiError::Forbidden("当前实例已关闭注册".into()));
        }
        self.limiter
            .check(&format!("register:{client_ip}"), REGISTER_QUOTA)
            .map_err(|secs| ApiError::TooManyRequests {
                message: "注册过于频繁，请稍后再试".into(),
                retry_after: secs,
            })?;

        let username = input.username.trim().to_string();
        let email = input.email.trim().to_string();
        let mut errs: Vec<(&'static str, String)> = Vec::new();
        if !valid_username(&username) {
            errs.push((
                "username",
                "长度 2-32，仅允许字母、数字、下划线与汉字，且不能以数字开头".into(),
            ));
        }
        if !valid_email(&email) {
            errs.push(("email", "邮箱格式不正确".into()));
        }
        if let Err(msg) = password::validate_password_strength(&input.password, &username) {
            errs.push(("password", msg));
        }
        if !errs.is_empty() {
            return Err(validation_error(errs));
        }

        let now = Utc::now();
        let username_lower = username.to_lowercase();
        let email_lower = email.to_lowercase();
        let password_hash = password::hash_password(&input.password, self.config.pbkdf2_iterations);
        let user_id = uuid::Uuid::new_v4().to_string();

        let created = self.store.write(|db| {
            if db.user_by_username(&username_lower).is_some() {
                return Err(ApiError::Conflict("用户名已被占用".into()));
            }
            if db.user_by_email(&email_lower).is_some() {
                return Err(ApiError::Conflict("邮箱已被注册".into()));
            }
            // 首个注册用户为 owner，便于个人部署自举
            let role = if db.users.is_empty() {
                Role::Owner
            } else {
                Role::Member
            };
            let user = User {
                id: user_id.clone(),
                username: username.clone(),
                username_lower: username_lower.clone(),
                email: email.clone(),
                email_lower: email_lower.clone(),
                display_name: input
                    .display_name
                    .clone()
                    .unwrap_or_else(|| username.clone()),
                password_hash: password_hash.clone(),
                role,
                status: UserStatus::Active,
                locale: input.locale.clone().unwrap_or_else(|| "zh-CN".into()),
                timezone: input.timezone.clone().unwrap_or_else(|| "Asia/Shanghai".into()),
                created_at: now,
                updated_at: now,
                last_login_at: None,
                failed_login_count: 0,
                locked_until: None,
            };
            db.users.insert(user_id.clone(), user.clone());
            db.username_index.insert(username_lower.clone(), user_id.clone());
            db.email_index.insert(email_lower.clone(), user_id.clone());
            db.progress
                .insert(user_id.clone(), OnboardingProgress::new(&user_id, now));
            Ok(user)
        })?;

        self.metrics.inc("sa_registrations_total");
        self.audit.record(
            &created.id,
            "auth.register",
            &created.id,
            true,
            &format!("role={}", created.role.as_str()),
            "",
        );

        let tokens = self.issue_pair(&created, client_ip, user_agent);
        Ok(self.build_result(&created, tokens))
    }

    pub fn login(
        &self,
        input: LoginInput,
        client_ip: &str,
        user_agent: &str,
    ) -> Result<AuthResult, ApiError> {
        // 先取登录名再限流：空登录名不该消耗任何人的配额，也不该进限流键
        let Some(account) = input.login_name() else {
            return Err(validation_error(vec![(
                "account",
                "请提供 account（用户名或邮箱；也接受 username / email 别名）".into(),
            )]));
        };
        self.limiter
            .check(&format!("login:{client_ip}:{account}"), LOGIN_QUOTA)
            .map_err(|secs| ApiError::TooManyRequests {
                message: "登录尝试过于频繁，请稍后再试".into(),
                retry_after: secs,
            })?;

        let now = Utc::now();
        let found = self
            .store
            .read(|db| db.user_by_username(&account).or_else(|| db.user_by_email(&account)).cloned());

        let Some(user) = found else {
            // 恒定时间：不存在的账号也走一次哈希，响应时间与真实校验一致
            let _ = password::verify_password(&input.password, DUMMY_HASH);
            self.metrics.inc("sa_login_failures_total");
            self.audit
                .record(&account, "auth.login", &account, false, "账号不存在", "");
            return Err(ApiError::Unauthorized("账号或密码不正确".into()));
        };

        if user.status != UserStatus::Active {
            return Err(ApiError::Forbidden("账号已被禁用，请联系管理员".into()));
        }
        if let Some(until) = user.locked_until {
            if until > now {
                let wait = (until - now).num_minutes() + 1;
                return Err(ApiError::Forbidden(format!(
                    "账号因多次登录失败已临时锁定，请 {wait} 分钟后重试"
                )));
            }
        }

        if !password::verify_password(&input.password, &user.password_hash) {
            let (locked_now, count) = self.store.write(|db| {
                let Some(u) = db.users.get_mut(&user.id) else {
                    return (false, 0);
                };
                u.failed_login_count += 1;
                u.updated_at = now;
                if u.failed_login_count >= MAX_FAILED_LOGINS {
                    u.locked_until = Some(now + Duration::minutes(LOCK_MINUTES));
                    let c = u.failed_login_count;
                    u.failed_login_count = 0;
                    (true, c)
                } else {
                    (false, u.failed_login_count)
                }
            });
            self.metrics.inc("sa_login_failures_total");
            self.audit.record(
                &user.id,
                "auth.login",
                &user.id,
                false,
                &format!("密码错误，累计 {count} 次{}", if locked_now { "，已锁定" } else { "" }),
                "",
            );
            if locked_now {
                self.metrics.inc("sa_account_locks_total");
                return Err(ApiError::Forbidden(format!(
                    "连续 {MAX_FAILED_LOGINS} 次密码错误，账号已锁定 {LOCK_MINUTES} 分钟"
                )));
            }
            return Err(ApiError::Unauthorized("账号或密码不正确".into()));
        }

        let refreshed = self.store.write(|db| {
            let Some(u) = db.users.get_mut(&user.id) else {
                return None;
            };
            u.failed_login_count = 0;
            u.locked_until = None;
            u.last_login_at = Some(now);
            u.updated_at = now;
            Some(u.clone())
        });
        let user = refreshed.ok_or_else(|| ApiError::Internal("用户状态更新失败".into()))?;

        self.metrics.inc("sa_logins_total");
        self.audit.record(&user.id, "auth.login", &user.id, true, "", "");

        let tokens = self.issue_pair(&user, client_ip, user_agent);
        Ok(self.build_result(&user, tokens))
    }

    /// 刷新令牌轮换：旧会话立即失效，新令牌带新的指纹，防止重放。
    pub fn refresh(&self, refresh_token: &str) -> Result<AuthResult, ApiError> {
        self.limiter
            .check("refresh:global", REFRESH_QUOTA)
            .map_err(|secs| ApiError::TooManyRequests {
                message: "刷新过于频繁".into(),
                retry_after: secs,
            })?;

        let claims = jwt::verify(
            &self.config.jwt_secret,
            refresh_token,
            jwt::TOKEN_TYPE_REFRESH,
            Utc::now().timestamp(),
        )
        .map_err(map_jwt_error)?;

        let fingerprint = jwt::fingerprint(refresh_token);
        let session = self.store.read(|db| {
            db.sessions
                .values()
                .find(|s| s.refresh_fingerprint == fingerprint)
                .cloned()
        });
        let Some(session) = session else {
            self.metrics.inc("sa_refresh_reuse_detected_total");
            return Err(ApiError::Unauthorized(
                "会话不存在或已被使用，请重新登录".into(),
            ));
        };
        if !session.is_active(Utc::now()) || session.user_id != claims.sub {
            return Err(ApiError::Unauthorized("会话已失效，请重新登录".into()));
        }

        let user = self
            .store
            .read(|db| db.users.get(&session.user_id).cloned())
            .ok_or_else(|| ApiError::Unauthorized("账号不存在".into()))?;
        if user.status != UserStatus::Active {
            return Err(ApiError::Forbidden("账号已被禁用".into()));
        }

        // 撤销旧会话（一次性使用）
        self.store.write(|db| {
            if let Some(s) = db.sessions.get_mut(&session.id) {
                s.revoked_at = Some(Utc::now());
            }
        });

        self.metrics.inc("sa_token_refresh_total");
        let tokens = self.issue_pair(&user, &session.client_ip, &session.user_agent);
        Ok(self.build_result(&user, tokens))
    }

    pub fn logout(&self, refresh_token: &str) -> Result<usize, ApiError> {
        let fingerprint = jwt::fingerprint(refresh_token);
        let revoked = self.store.write(|db| {
            let mut count = 0;
            for s in db.sessions.values_mut() {
                if s.refresh_fingerprint == fingerprint && s.revoked_at.is_none() {
                    s.revoked_at = Some(Utc::now());
                    count += 1;
                }
            }
            count
        });
        self.note_revoked(revoked);
        Ok(revoked)
    }

    /// 改密：校验旧密码 → 更新哈希 → 撤销全部会话（强制重新登录）。
    pub fn change_password(
        &self,
        user_id: &str,
        current_password: &str,
        new_password: &str,
    ) -> Result<usize, ApiError> {
        let user = self
            .store
            .read(|db| db.users.get(user_id).cloned())
            .ok_or_else(|| ApiError::Unauthorized("账号不存在".into()))?;
        if !password::verify_password(current_password, &user.password_hash) {
            self.metrics.inc("sa_password_change_failures_total");
            self.audit.record(
                &user.id,
                "auth.change_password",
                &user.id,
                false,
                "当前密码不正确",
                "",
            );
            return Err(ApiError::Unauthorized("当前密码不正确".into()));
        }
        if password::verify_password(new_password, &user.password_hash) {
            return Err(validation_error(vec![(
                "new_password",
                "新密码不能与当前密码相同".into(),
            )]));
        }
        if let Err(msg) = password::validate_password_strength(new_password, &user.username) {
            return Err(validation_error(vec![("new_password", msg)]));
        }

        let hash = password::hash_password(new_password, self.config.pbkdf2_iterations);
        let now = Utc::now();
        self.store.write(|db| {
            if let Some(u) = db.users.get_mut(user_id) {
                u.password_hash = hash.clone();
                u.updated_at = now;
                u.failed_login_count = 0;
                u.locked_until = None;
            }
        });
        let revoked = self.revoke_all(user_id);
        self.metrics.inc("sa_password_changes_total");
        self.audit.record(
            &user.id,
            "auth.change_password",
            &user.id,
            true,
            &format!("revoked_sessions={revoked}"),
            "",
        );
        Ok(revoked)
    }

    /// 撤销该用户的全部会话（改密/管理员踢下线场景）。
    pub fn revoke_all(&self, user_id: &str) -> usize {
        let now = Utc::now();
        let revoked = self.store.write(|db| {
            let mut count = 0;
            for s in db.sessions.values_mut() {
                if s.user_id == user_id && s.revoked_at.is_none() {
                    s.revoked_at = Some(now);
                    count += 1;
                }
            }
            count
        });
        self.note_revoked(revoked);
        revoked
    }

    /// 会话撤销唯一打点出口。
    ///
    /// `sa_sessions_revoked_total` 此前零落点（能力规格 `session_management` 引用了它）。
    /// 主动退出、改密踢全设备都收敛到这里，是为了让"撤销数突然抬头"在排障时
    /// 能配合审计日志的 `action` 分清是哪一类动作——否则只看到一个计数器陡增，
    /// 既可能是用户批量退出，也可能是有人在撞库成功后被强制下线，含义完全不同。
    fn note_revoked(&self, revoked: usize) {
        if revoked > 0 {
            self.metrics
                .inc_by("sa_sessions_revoked_total", "", revoked as u64);
        }
    }

    pub fn me(&self, user_id: &str) -> Result<UserView, ApiError> {
        self.store
            .read(|db| db.users.get(user_id).map(UserView::from))
            .ok_or_else(|| ApiError::Unauthorized("账号不存在或已被删除".into()))
    }

    fn build_result(&self, user: &User, tokens: AuthTokens) -> AuthResult {
        let (status, next) = self.store.read(|db| {
            db.progress
                .get(&user.id)
                .map(|p| (p.status(), p.next_step().map(|s| s.key)))
                .unwrap_or(("not_started", Some("welcome")))
        });
        AuthResult {
            user: UserView::from(user),
            tokens,
            onboarding_status: status,
            onboarding_next_step: next,
        }
    }

    fn issue_pair(&self, user: &User, client_ip: &str, user_agent: &str) -> AuthTokens {
        let now = Utc::now();
        let access_exp = now.timestamp() + self.config.access_ttl_secs;
        let refresh_exp = now.timestamp() + self.config.refresh_ttl_secs;

        let access = jwt::issue(
            &self.config.jwt_secret,
            &jwt::Claims {
                sub: user.id.clone(),
                role: user.role.as_str().to_string(),
                typ: jwt::TOKEN_TYPE_ACCESS.to_string(),
                iat: now.timestamp(),
                exp: access_exp,
                jti: uuid::Uuid::new_v4().to_string(),
            },
        );
        let refresh = jwt::issue(
            &self.config.jwt_secret,
            &jwt::Claims {
                sub: user.id.clone(),
                role: user.role.as_str().to_string(),
                typ: jwt::TOKEN_TYPE_REFRESH.to_string(),
                iat: now.timestamp(),
                exp: refresh_exp,
                jti: uuid::Uuid::new_v4().to_string(),
            },
        );

        let session = Session {
            id: uuid::Uuid::new_v4().to_string(),
            user_id: user.id.clone(),
            refresh_fingerprint: jwt::fingerprint(&refresh),
            created_at: now,
            expires_at: now + Duration::seconds(self.config.refresh_ttl_secs),
            revoked_at: None,
            user_agent: user_agent.chars().take(200).collect(),
            client_ip: client_ip.to_string(),
        };
        self.store
            .write(|db| db.sessions.insert(session.id.clone(), session));

        AuthTokens {
            access_token: access,
            refresh_token: refresh,
            token_type: "Bearer",
            expires_in: self.config.access_ttl_secs,
        }
    }
}

fn map_jwt_error(e: JwtError) -> ApiError {
    match e {
        JwtError::Expired => ApiError::Unauthorized("令牌已过期，请重新登录".into()),
        JwtError::WrongType => ApiError::Unauthorized("令牌类型不正确".into()),
        _ => ApiError::Unauthorized("令牌无效".into()),
    }
}

pub fn valid_username(username: &str) -> bool {
    let len = username.chars().count();
    if !(2..=32).contains(&len) {
        return false;
    }
    let mut chars = username.chars();
    match chars.next() {
        Some(c) if c.is_ascii_digit() => return false,
        Some(c) if c.is_alphanumeric() || c == '_' => {}
        _ => return false,
    }
    username
        .chars()
        .all(|c| c.is_alphanumeric() || c == '_')
}

pub fn valid_email(email: &str) -> bool {
    let len = email.chars().count();
    if !(5..=254).contains(&len) || email.contains(' ') {
        return false;
    }
    let mut parts = email.split('@');
    let (Some(local), Some(domain), None) = (parts.next(), parts.next(), parts.next()) else {
        return false;
    };
    !local.is_empty()
        && domain.contains('.')
        && !domain.starts_with('.')
        && !domain.ends_with('.')
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::obs::metrics::Metrics;
    use crate::security::ratelimit::RateLimiter;
    use std::path::PathBuf;

    /// 走审计端口工厂构造，测试不绑死具体实现：换 SA_AUDIT 后测试自动跟随。
    fn fixture() -> (Config, Store, Metrics, std::sync::Arc<dyn AuditSink>, RateLimiter) {
        let dir = PathBuf::from(std::env::temp_dir()).join(format!("sa_auth_{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let config = Config::for_test(dir);
        let audit = config
            .audit
            .open(config.audit_capacity, config.audit_file.clone())
            .expect("审计端口构造失败");
        (config, Store::in_memory(), Metrics::new(), audit, RateLimiter::new())
    }

    fn register(
        config: &Config,
        store: &Store,
        metrics: &Metrics,
        audit: &dyn AuditSink,
        limiter: &RateLimiter,
        username: &str,
        password: &str,
        ip: &str,
    ) -> Result<AuthResult, ApiError> {
        let svc = AuthService {
            config,
            store,
            metrics,
            audit,
            limiter,
        };
        svc.register(
            RegisterInput {
                username: username.into(),
                email: format!("{username}@example.com"),
                password: password.into(),
                display_name: None,
                locale: None,
                timezone: None,
            },
            ip,
            "unit-test-agent",
        )
    }

    #[test]
    fn first_user_becomes_owner() {
        let (config, store, metrics, audit, limiter) = fixture();
        let r = register(&config, &store, &metrics, audit.as_ref(), &limiter, "alice", "abc1234567", "1.1.1.1").unwrap();
        assert_eq!(r.user.role, Role::Owner);
        assert_eq!(r.onboarding_status, "not_started");
        assert_eq!(r.onboarding_next_step, Some("welcome"));
        assert!(!r.tokens.access_token.is_empty());

        let r2 = register(&config, &store, &metrics, audit.as_ref(), &limiter, "bob", "abc1234567", "1.1.1.2").unwrap();
        assert_eq!(r2.user.role, Role::Member, "非首个用户应为 member");
    }

    #[test]
    fn duplicate_and_weak_inputs_are_rejected() {
        let (config, store, metrics, audit, limiter) = fixture();
        register(&config, &store, &metrics, audit.as_ref(), &limiter, "alice", "abc1234567", "1.1.1.1").unwrap();

        let dup = register(&config, &store, &metrics, audit.as_ref(), &limiter, "alice", "abc1234567", "1.1.1.3");
        assert!(matches!(dup, Err(ApiError::Conflict(_))));

        let weak = register(&config, &store, &metrics, audit.as_ref(), &limiter, "carol", "123456", "1.1.1.4");
        assert!(matches!(weak, Err(ApiError::Validation(_))));

        let bad_name = register(&config, &store, &metrics, audit.as_ref(), &limiter, "1abc", "abc1234567", "1.1.1.5");
        assert!(matches!(bad_name, Err(ApiError::Validation(_))));
    }

    #[test]
    fn login_success_and_failure() {
        let (config, store, metrics, audit, limiter) = fixture();
        register(&config, &store, &metrics, audit.as_ref(), &limiter, "alice", "abc1234567", "1.1.1.1").unwrap();
        let svc = AuthService {
            config: &config,
            store: &store,
            metrics: &metrics,
            audit: audit.as_ref(),
            limiter: &limiter,
        };

        let ok = svc
            .login(
                LoginInput {
                    account: Some("alice".into()),
                    username: None,
                    email: None,
                    password: "abc1234567".into(),
                },
                "2.2.2.2",
                "ua",
            )
            .unwrap();
        assert_eq!(ok.user.username, "alice");

        let bad = svc.login(
            LoginInput {
                account: Some("alice@example.com".into()),
                username: None,
                email: None,
                password: "wrongpass123".into(),
            },
            "2.2.2.3",
            "ua",
        );
        assert!(matches!(bad, Err(ApiError::Unauthorized(_))));
        assert_eq!(metrics.snapshot().get("sa_login_failures_total"), 1);
    }

    #[test]
    fn locks_after_five_failures() {
        let (config, store, metrics, audit, limiter) = fixture();
        register(&config, &store, &metrics, audit.as_ref(), &limiter, "alice", "abc1234567", "1.1.1.1").unwrap();
        let svc = AuthService {
            config: &config,
            store: &store,
            metrics: &metrics,
            audit: audit.as_ref(),
            limiter: &limiter,
        };
        for i in 0..5 {
            let r = svc.login(
                LoginInput {
                    account: Some("alice".into()),
                    username: None,
                    email: None,
                    password: "wrongpass123".into(),
                },
                &format!("3.3.3.{i}"),
                "ua",
            );
            assert!(r.is_err());
        }
        let locked = svc.login(
            LoginInput {
                account: Some("alice".into()),
                username: None,
                email: None,
                password: "abc1234567".into(),
            },
            "4.4.4.4",
            "ua",
        );
        match locked {
            Err(ApiError::Forbidden(msg)) => assert!(msg.contains("锁定")),
            other => panic!("应返回锁定：{:?}", other.is_ok()),
        }
    }

    #[test]
    fn refresh_rotates_and_blocks_replay() {
        let (config, store, metrics, audit, limiter) = fixture();
        let r = register(&config, &store, &metrics, audit.as_ref(), &limiter, "alice", "abc1234567", "1.1.1.1").unwrap();
        let svc = AuthService {
            config: &config,
            store: &store,
            metrics: &metrics,
            audit: audit.as_ref(),
            limiter: &limiter,
        };
        let second = svc.refresh(&r.tokens.refresh_token).unwrap();
        assert_ne!(second.tokens.refresh_token, r.tokens.refresh_token);

        // 旧 token 已撤销，重放必须失败
        assert!(svc.refresh(&r.tokens.refresh_token).is_err());
        // 新 token 仍可用
        assert!(svc.refresh(&second.tokens.refresh_token).is_ok());
    }

    #[test]
    fn logout_revokes_session() {
        let (config, store, metrics, audit, limiter) = fixture();
        let r = register(&config, &store, &metrics, audit.as_ref(), &limiter, "alice", "abc1234567", "1.1.1.1").unwrap();
        let svc = AuthService {
            config: &config,
            store: &store,
            metrics: &metrics,
            audit: audit.as_ref(),
            limiter: &limiter,
        };
        assert_eq!(svc.logout(&r.tokens.refresh_token).unwrap(), 1);
        assert!(svc.refresh(&r.tokens.refresh_token).is_err());
    }

    #[test]
    fn email_and_username_validators() {
        assert!(valid_username("alice"));
        assert!(valid_username("张_三"));
        assert!(!valid_username("1alice"));
        assert!(!valid_username("a"));
        assert!(valid_email("a.b@example.com"));
        assert!(!valid_email("a@b"));
        assert!(!valid_email("a b@example.com"));
    }
}
