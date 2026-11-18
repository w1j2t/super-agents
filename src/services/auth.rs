//! 认证服务：注册、登录（含渐进式锁定）、刷新令牌轮换、登出、会话管理。

use crate::config::Config;
use crate::domain::onboarding::OnboardingProgress;
use crate::domain::user::{Session, User, UserStatus, UserView};
use crate::error::{validation_error, ApiError};
use crate::obs::audit::AuditSink;
use crate::obs::metrics::MetricsExporter;
use crate::security::jwt::{self, JwtError};
use crate::security::login_guard;
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

/// FIX-010：账号级硬锁已废弃。原来 5 次失败就把 `locked_until` 写进用户记录，
/// 造成两个洞：触发锁定那一次返回 403「已锁定」＝直接告诉攻击者"这账号存在"；
/// 而且任何人发 5 个错密码就能把别人锁在门外。现在改为
/// `security::login_guard` 的「主体 + 来源 IP」退避，失败 5 次起 1s→2s→4s… 封顶 15 分钟。

/// FIX-002：库损坏被留档后写入的标记文件（相对 `<data_dir>`）。
/// 存在即表示"当前的空库是恢复出来的，不是首次安装"，因此不得按首装自举开放注册。
pub const RECOVERY_LOCK_FILE: &str = "registration.lock";

/// 账号不存在时同样执行一次哈希校验，避免通过响应时间探测账号是否注册。
/// 账号不存在时要拿来做“等价校验”的假哈希。
///
/// 旧实现是个常量，编码里写着 `$1$`（1 次迭代）：账号不存在时几十微秒就返回，
/// 账号存在时要跑满 `pbkdf2_iterations`（默认 12 万次，几十到几百毫秒）。
/// 两者差 4~5 个数量级，**网络侧测个响应时间就能批量枚举出哪些用户名真实存在**。
/// 这里改为“按当前配置迭代数生成一个随机盐 + 随机密码的假哈希”，首次生成后缓存：
/// 校验耗时与真实账号同量级，而任何输入都不可能命中它。
fn dummy_hash(iterations: u32) -> Result<String, ApiError> {
    static CACHE: std::sync::OnceLock<(u32, String)> = std::sync::OnceLock::new();
    if let Some((cached_iter, cached)) = CACHE.get() {
        if *cached_iter == iterations {
            return Ok(cached.clone());
        }
    }
    // 运行期换了迭代数（测试里常见）：现算一份，正确性优先于这点开销
    let fresh = password::hash_password(&uuid::Uuid::new_v4().to_string(), iterations)
        .map_err(|e| ApiError::Internal(format!("无法生成登录假哈希: {e}")))?;
    // 并发首次调用时谁先写谁生效，内容等价，后到者丢弃即可
    let _ = CACHE.set((iterations, fresh.clone()));
    Ok(fresh)
}

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
            // FIX-002：注册默认关闭，但首次自举必须可用，否则新装实例无法创建第一个账号。
            // 例外只给"库为空 + 无恢复标记"：库为空说明还没有任何账号，抢注者与用户
            // 面对同一个空库，先到先得本来就是自举语义；有恢复标记（registration.lock）
            // 说明库曾经存在但被判损坏、已留档并从空库启动，此时空库不等于首装，
            // 绝不能再开注册，否则"写坏 JSON 后重启再注册成 owner"就是接管路径。
            let recovery_locked = self.config.data_dir.join(RECOVERY_LOCK_FILE).exists();
            let bootstrapping = !recovery_locked && self.store.read(|db| db.users.is_empty());
            if !bootstrapping {
                return Err(ApiError::Forbidden("当前实例已关闭注册".into()));
            }
            tracing::warn!(
                client_ip,
                "注册未开启，但库为空且无恢复标记：按首次自举放行（该用户将成为 owner）"
            );
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
        let password_hash = password::hash_password(&input.password, self.config.pbkdf2_iterations)
            .map_err(|e| ApiError::Internal(format!("密码哈希失败: {e}")))?;
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

        let (tokens, _sid) = self.issue_pair(&created, client_ip, user_agent)?;
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

        let iters = self.config.pbkdf2_iterations;
        // FIX-010：退避的粒度是「主体 + 来源 IP」，不是账号本身。
        // 主体优先用 user id（同一账号的用户名/邮箱/大小写变体共用一份额度），
        // 账号不存在时用归一化后的输入 —— 两边都记账，退避才不会变成存在性信号。
        let guard_subject =
            login_guard::LoginGuard::subject_for(found.as_ref().map(|u| u.id.as_str()), &account);
        if let Some(wait) = login_guard::global().blocked_for(&guard_subject, client_ip) {
            // 仍在退避期：照样跑满一次哈希，照样返回统一的 401，只是文案多一句"稍后再试"
            // （对存在与不存在的账号完全一致，所以不泄漏任何东西）。
            let _ = password::verify_password(&input.password, &dummy_hash(iters)?);
            self.metrics.inc("sa_login_failures_total");
            self.audit.record(
                &guard_subject,
                "auth.login",
                &account,
                false,
                "登录退避中（同一来源连续失败过多）",
                "",
            );
            return Err(ApiError::UnauthorizedWithRetry {
                message: "账号或密码不正确，请稍后再试".into(),
                retry_after: wait.as_secs().max(1),
            });
        }

        let Some(user) = found else {
            // 恒定耗时：不存在的账号也跑一次真哈希，与真实账号同量级
            let _ = password::verify_password(&input.password, &dummy_hash(iters)?);
            self.metrics.inc("sa_login_failures_total");
            // 不存在的账号也照样记账：两边都退避，才没有"是否存在"的区分度
            login_guard::global().record_failure(&guard_subject, client_ip);
            self.audit
                .record(&account, "auth.login", &account, false, "账号不存在", "");
            return Err(ApiError::Unauthorized("账号或密码不正确".into()));
        };

        // 禁用 / 锁定分支：先照常跑一次哈希，再返回与“密码错误”完全一致的 401。
        // 原来的 403 + 差异化文案（“已被禁用”“已临时锁定”）等于白送一个
        // 比时间差更好用的存在性信号。真实原因只写审计，不对外暴露。
        if user.status != UserStatus::Active {
            let _ = password::verify_password(&input.password, &dummy_hash(iters)?);
            self.metrics.inc("sa_login_failures_total");
            login_guard::global().record_failure(&guard_subject, client_ip);
            self.audit
                .record(&account, "auth.login", &account, false, "账号已禁用", "");
            return Err(ApiError::Unauthorized("账号或密码不正确".into()));
        }
        // 兼容历史数据：老版本写进库里的 `locked_until` 不再产生差异响应，
        // 一律按普通失败处理并转入来源退避，等这份旧锁自然过期即可。
        if let Some(until) = user.locked_until {
            if until > now {
                let _ = password::verify_password(&input.password, &dummy_hash(iters)?);
                self.metrics.inc("sa_login_failures_total");
                login_guard::global().record_failure(&guard_subject, client_ip);
                self.audit
                    .record(&account, "auth.login", &account, false, "账号锁定中（历史遗留）", "");
                return Err(ApiError::Unauthorized("账号或密码不正确".into()));
            }
        }

        if !password::verify_password(&input.password, &user.password_hash) {
            // 库里的计数只用于审计与运维统计，不再决定"锁不锁"，也不再写 locked_until
            let count = self.store.write(|db| {
                let Some(u) = db.users.get_mut(&user.id) else {
                    return 0;
                };
                u.failed_login_count = u.failed_login_count.saturating_add(1);
                u.updated_at = now;
                u.failed_login_count
            });
            let backoff = login_guard::global().record_failure(&guard_subject, client_ip);
            self.metrics.inc("sa_login_failures_total");
            if !backoff.is_zero() {
                // 指标名沿用旧口径，含义变成"某来源进入了退避"，便于既有看板继续用
                self.metrics.inc("sa_account_locks_total");
            }
            self.audit.record(
                &user.id,
                "auth.login",
                &user.id,
                false,
                &format!(
                    "密码错误，累计 {count} 次{}",
                    if backoff.is_zero() {
                        String::new()
                    } else {
                        format!("，来源退避 {}s", backoff.as_secs())
                    }
                ),
                "",
            );
            // 关键改动：这一支原来返回 403「连续 5 次密码错误，账号已锁定 15 分钟」，
            // 等于直接对攻击者宣告"这个账号存在"。现在与其余失败分支完全同构：
            // 同一个状态码、同一句文案，没有任何区分度；刚进入退避时只多一个 Retry-After 头。
            return Err(if backoff.is_zero() {
                ApiError::Unauthorized("账号或密码不正确".into())
            } else {
                ApiError::UnauthorizedWithRetry {
                    message: "账号或密码不正确".into(),
                    retry_after: backoff.as_secs().max(1),
                }
            });
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
        // 登录成功：清掉这一份（主体+IP）的失败记账，别让之前几次打错留下尾巴
        login_guard::global().record_success(&guard_subject, client_ip);

        self.metrics.inc("sa_logins_total");
        self.audit.record(&user.id, "auth.login", &user.id, true, "", "");

        let (tokens, _sid) = self.issue_pair(&user, client_ip, user_agent)?;
        Ok(self.build_result(&user, tokens))
    }

    /// 刷新令牌轮换：旧会话立即失效，新令牌带新的指纹，防止重放。
    pub fn refresh(&self, refresh_token: &str) -> Result<AuthResult, ApiError> {
        let claims = jwt::verify(
            &self.config.jwt_secret,
            refresh_token,
            jwt::TOKEN_TYPE_REFRESH,
            Utc::now().timestamp(),
        )
        .map_err(map_jwt_error)?;

        // 限流必须放在**验签之后，并且按主体分桶**。
        //
        // 原来是一个 `refresh:global` 单桶，贯穿所有调用者：任何人拿一串乱码去刷
        // 就能把它打满，随后所有人的正常刷新一起 429——这是一个不需要任何凭据的
        // 全局拒绝服务。现在两处都改了：
        //   1. 垃圾令牌在验签这步直接 401，根本不碰桶；
        //   2. 有效令牌按 `sub` 分桶，刷爆的只有自己这份额度。
        self.limiter
            .check(&format!("refresh:{}", claims.sub), REFRESH_QUOTA)
            .map_err(|secs| ApiError::TooManyRequests {
                message: "刷新过于频繁".into(),
                retry_after: secs,
            })?;

        let fingerprint = jwt::fingerprint(refresh_token);
        let now = Utc::now();

        /// 轮换的判定结果，全部在**同一个写闭包**里得出。
        enum Claim {
            Fresh(Session),
            Reuse(Session),
            Gone,
        }

        // 查找 → 校验 active → 置 revoked，必须在同一次 store.write 内完成。
        // 旧实现是「read 一遍判断 active，write 一遍撤销」：两个并发请求都能读到
        // active、都能撤销成功、各自换出一对令牌——轮换的“一次性”承诺在并发下失效。
        let claim = self.store.write(|db| {
            let id = db
                .sessions
                .values()
                .find(|s| s.refresh_fingerprint == fingerprint)
                .map(|s| s.id.clone());
            let Some(id) = id else { return Claim::Gone };
            let Some(s) = db.sessions.get_mut(&id) else {
                return Claim::Gone;
            };
            // 已撤销 / 已过期还拿来换：这不是“轮换”，是重放或偷用
            if s.revoked_at.is_some() || !s.is_active(now) {
                return Claim::Reuse(s.clone());
            }
            s.revoked_at = Some(now);
            s.revoked_reason = Some(crate::domain::user::REVOKE_ROTATED.to_string());
            Claim::Fresh(s.clone())
        });

        let session = match claim {
            Claim::Gone => {
                self.metrics.inc("sa_refresh_reuse_detected_total");
                return Err(ApiError::Unauthorized(
                    "会话不存在或已被使用，请重新登录".into(),
                ));
            }
            Claim::Reuse(stale) => {
                // 复用处置：只拒绝本次请求并留痕，**不连锁撤销**其它会话。
                // 契约依据（门禁 S25 / S85 / C10）：
                //   - 轮换本身已让旧令牌作废，重放者拿不到新令牌（S25）；
                //   - 并发刷新、多标签多进程运维天然会把同一旧令牌再送一次，
                //     若在此 revoke_all，一次竞态就能把用户全部会话踢下线（S85/C10 变红）；
                //   - 重放者的收益已被“旧令牌不可用”封死，不需要牵连新会话。
                // 因此这里降级为观测事件；需要族级撤销时按 docs/06 的加固项单独开关。
                self.metrics.inc("sa_refresh_reuse_detected_total");
                self.audit.record(
                    &stale.user_id,
                    "auth.refresh_reuse",
                    &stale.id,
                    false,
                    "refresh 令牌被重复使用（已拒绝，未连锁撤销）",
                    "scope=session-only revoked_sessions=0",
                );
                return Err(ApiError::Unauthorized(
                    "会话已失效，请重新登录".into(),
                ));
            }
            Claim::Fresh(s) => s,
        };

        if session.user_id != claims.sub {
            return Err(ApiError::Unauthorized("会话已失效，请重新登录".into()));
        }

        let user = self
            .store
            .read(|db| db.users.get(&session.user_id).cloned())
            .ok_or_else(|| ApiError::Unauthorized("账号不存在".into()))?;
        if user.status != UserStatus::Active {
            return Err(ApiError::Forbidden("账号已被禁用".into()));
        }

        self.metrics.inc("sa_token_refresh_total");
        let (tokens, new_sid) =
            self.issue_pair(&user, &session.client_ip, &session.user_agent)?;
        // 补链：旧一代 → 新一代。只把旧一代记成 rotated 而不指向下一代的话，
        // 手里还拿着旧 access 的人永远读不到新一代的登出/改密撤销（SA-013）。
        let prev_id = session.id.clone();
        self.store.write(|db| {
            if let Some(prev) = db.sessions.get_mut(&prev_id) {
                prev.successor_id = Some(new_sid);
            }
        });
        Ok(self.build_result(&user, tokens))
    }

    pub fn logout(&self, refresh_token: &str) -> Result<usize, ApiError> {
        let fingerprint = jwt::fingerprint(refresh_token);
        let revoked = self.store.write(|db| {
            // 先定位这条 refresh 属于哪一代会话
            let Some(target) = db
                .sessions
                .values()
                .find(|s| s.refresh_fingerprint == fingerprint)
                .map(|s| s.id.clone())
            else {
                return 0;
            };
            // 登出结束的是"这一次登录"，不是"这一代令牌"：把整条轮换链一起撤掉。
            // 只撤当前一代的话，手里还捏着上一代 access 的人登出后照样能用到过期，
            // "已登出"就只存在于前端的想象里（SA-013）。
            let mut lineage = vec![target.clone()];
            let mut cur = target;
            for _ in 0..64 {
                let Some(prev) = db
                    .sessions
                    .values()
                    .find(|s| s.successor_id.as_deref() == Some(cur.as_str()))
                    .map(|s| s.id.clone())
                else {
                    break;
                };
                lineage.push(prev.clone());
                cur = prev;
            }
            let mut count = 0;
            for id in lineage {
                if let Some(s) = db.sessions.get_mut(&id) {
                    // 已被轮换撤过的那几代也要改写撤销原因：rotated 是"令牌换代"，
                    // 现在发生的是"登录结束"，两者语义不同，链尾判定靠的就是这个区别。
                    let retirable = s.revoked_at.is_none()
                        || s.revoked_reason.as_deref()
                            == Some(crate::domain::user::REVOKE_ROTATED);
                    if retirable {
                        s.revoked_at = Some(Utc::now());
                        s.revoked_reason = Some(crate::domain::user::REVOKE_LOGOUT.to_string());
                        count += 1;
                    }
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

        let hash = password::hash_password(new_password, self.config.pbkdf2_iterations)
            .map_err(|e| ApiError::Internal(format!("密码哈希失败: {e}")))?;
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
                // 连被轮换撤过的旧代一起改写原因：那些旧代上还挂着未过期的 access，
                // 只按 `revoked_at.is_none()` 筛会漏掉它们，改密后就踢不掉旧设备。
                let retirable = s.revoked_at.is_none()
                    || s.revoked_reason.as_deref() == Some(crate::domain::user::REVOKE_ROTATED);
                if s.user_id == user_id && retirable {
                    s.revoked_at = Some(now);
                    s.revoked_reason = Some(crate::domain::user::REVOKE_PASSWORD.to_string());
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

    /// 签发 access/refresh 令牌对。
    ///
    /// 返回 `Result` 而不是 `AuthTokens`：签发涉及序列化与 HMAC 初始化，
    /// 以前这两步内部 `expect`，等于让一次理论上的异常把请求线程打掉（三方审查 M2）。
    /// 现在失败一律转 500，服务继续可用。
    /// 返回 (令牌对, 本次新建会话的 id)。
    ///
    /// 会话 id 必须回传给调用方：refresh 轮换要把"旧一代 → 新一代"的链补上，
    /// 否则旧的 access 令牌永远看不到新一代的撤销（见 Session::successor_id）。
    fn issue_pair(
        &self,
        user: &User,
        client_ip: &str,
        user_agent: &str,
    ) -> Result<(AuthTokens, String), ApiError> {
        let now = Utc::now();
        let access_exp = now.timestamp() + self.config.access_ttl_secs;
        let refresh_exp = now.timestamp() + self.config.refresh_ttl_secs;

        // 会话 id 必须在签发之前定下来：access 令牌里要带 `sid`，
        // 否则"登出"撤不掉已经发出去的 access（见 Claims::sid 的注释）。
        // 下面 Session 用的就是同一个 id，两者必须同源，不能各生成一个 uuid。
        let session_id = uuid::Uuid::new_v4().to_string();

        let access = jwt::issue(
            &self.config.jwt_secret,
            &jwt::Claims {
                sub: user.id.clone(),
                role: user.role.as_str().to_string(),
                typ: jwt::TOKEN_TYPE_ACCESS.to_string(),
                iat: now.timestamp(),
                exp: access_exp,
                jti: uuid::Uuid::new_v4().to_string(),
                sid: session_id.clone(),
                // 实际值由 jwt::issue 单点覆写为 ISSUER/AUDIENCE，这里显式写出
                // 是为了让 Claims 的完整形状在签发点一眼可见（结构体字面量不允许省略）。
                iss: jwt::ISSUER.to_string(),
                aud: jwt::AUDIENCE.to_string(),
            },
        )
        .map_err(|e| ApiError::Internal(format!("令牌签发失败: {e}")))?;
        let refresh = jwt::issue(
            &self.config.jwt_secret,
            &jwt::Claims {
                sub: user.id.clone(),
                role: user.role.as_str().to_string(),
                typ: jwt::TOKEN_TYPE_REFRESH.to_string(),
                iat: now.timestamp(),
                exp: refresh_exp,
                jti: uuid::Uuid::new_v4().to_string(),
                sid: session_id.clone(),
                iss: jwt::ISSUER.to_string(),
                aud: jwt::AUDIENCE.to_string(),
            },
        )
        .map_err(|e| ApiError::Internal(format!("令牌签发失败: {e}")))?;

        let session = Session {
            id: session_id,
            user_id: user.id.clone(),
            refresh_fingerprint: jwt::fingerprint(&refresh),
            created_at: now,
            expires_at: now + Duration::seconds(self.config.refresh_ttl_secs),
            revoked_at: None,
            revoked_reason: None,
            successor_id: None,
            user_agent: user_agent.chars().take(200).collect(),
            client_ip: client_ip.to_string(),
        };
        let new_id = session.id.clone();
        self.store
            .write(|db| db.sessions.insert(session.id.clone(), session));

        Ok((
            AuthTokens {
                access_token: access,
                refresh_token: refresh,
                token_type: "Bearer",
                expires_in: self.config.access_ttl_secs,
            },
            new_id,
        ))
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

    /// FIX-010 方向一：第 5 次失败的返回必须与普通失败完全同构。
    /// 旧实现在这一次返回 `403 + "连续 5 次密码错误，账号已锁定 15 分钟"`，
    /// 等于用 5 个请求就能判定"这个用户名存在"——比任何时间差都好用。
    #[test]
    fn fifth_failure_is_indistinguishable_from_a_normal_failure() {
        let (config, store, metrics, audit, limiter) = fixture();
        register(&config, &store, &metrics, audit.as_ref(), &limiter, "alice", "abc1234567", "1.1.1.1").unwrap();
        let svc = AuthService {
            config: &config,
            store: &store,
            metrics: &metrics,
            audit: audit.as_ref(),
            limiter: &limiter,
        };
        let mut messages = Vec::new();
        for i in 0..5 {
            let r = svc.login(
                LoginInput {
                    account: Some("alice".into()),
                    username: None,
                    email: None,
                    password: "wrongpass123".into(),
                },
                "5.5.5.5",
                "ua",
            );
            match r {
                Err(ApiError::Unauthorized(m))
                | Err(ApiError::UnauthorizedWithRetry { message: m, .. }) => messages.push(m),
                other => panic!("第 {} 次失败必须是 401，实际 ok={}", i + 1, other.is_ok()),
            }
        }
        assert!(
            messages.iter().all(|m| m == "账号或密码不正确"),
            "5 次失败的文案必须一致，不能在第 5 次冒出锁定提示：{messages:?}"
        );
    }

    /// FIX-010 方向一（另一半）：存在的账号与不存在的账号，退避后的响应也要一致。
    #[test]
    fn lockout_gives_no_account_existence_signal() {
        let (config, store, metrics, audit, limiter) = fixture();
        register(&config, &store, &metrics, audit.as_ref(), &limiter, "alice", "abc1234567", "1.1.1.1").unwrap();
        let svc = AuthService {
            config: &config,
            store: &store,
            metrics: &metrics,
            audit: audit.as_ref(),
            limiter: &limiter,
        };
        let probe = |account: &str, ip: &str| {
            for _ in 0..5 {
                let _ = svc.login(
                    LoginInput {
                        account: Some(account.into()),
                        username: None,
                        email: None,
                        password: "wrongpass123".into(),
                    },
                    ip,
                    "ua",
                );
            }
            match svc.login(
                LoginInput {
                    account: Some(account.into()),
                    username: None,
                    email: None,
                    password: "wrongpass123".into(),
                },
                ip,
                "ua",
            ) {
                Err(ApiError::Unauthorized(m))
                | Err(ApiError::UnauthorizedWithRetry { message: m, .. }) => m,
                other => panic!("退避期内的账号 {account} 必须返回 401，实际 ok={}", other.is_ok()),
            }
        };
        let existing = probe("alice", "6.6.6.1");
        let ghost = probe("ghost_no_such_user", "6.6.6.2");
        assert_eq!(
            existing, ghost,
            "存在与不存在的账号，退避后的文案必须一字不差，否则就是枚举接口"
        );
    }

    /// FIX-010 方向二：退避按「账号 + 来源 IP」，不是按账号。
    /// 旧实现把 `locked_until` 写在用户记录上，任何人每 15 分钟发 5 个错密码
    /// 就能把管理员永久锁在门外；同理，别人从另一个 IP 正确登录也会被牵连。
    #[test]
    fn backoff_is_per_source_and_never_locks_the_account_itself() {
        let (config, store, metrics, audit, limiter) = fixture();
        register(&config, &store, &metrics, audit.as_ref(), &limiter, "alice", "abc1234567", "1.1.1.1").unwrap();
        let svc = AuthService {
            config: &config,
            store: &store,
            metrics: &metrics,
            audit: audit.as_ref(),
            limiter: &limiter,
        };
        let login = |password: &str, ip: &str| {
            svc.login(
                LoginInput {
                    account: Some("alice".into()),
                    username: None,
                    email: None,
                    password: password.into(),
                },
                ip,
                "ua",
            )
        };
        for _ in 0..6 {
            assert!(login("wrongpass123", "7.7.7.7").is_err());
        }
        // 攻击来源自己：即使密码正确也要等退避结束
        assert!(
            matches!(
                login("abc1234567", "7.7.7.7"),
                Err(ApiError::Unauthorized(_)) | Err(ApiError::UnauthorizedWithRetry { .. })
            ),
            "同一来源在退避期内必须被拒"
        );
        // 合法用户从另一个 IP 登录：必须放行，账号不能被别人锁死
        let ok = login("abc1234567", "8.8.8.8").expect("换来源不应被牵连，账号更不能被锁死");
        assert_eq!(ok.user.username, "alice");
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
        // 但重放**不连锁撤销**其它会话：合法会话仍能继续轮换。
        // 契约依据：门禁 S85（登出还能撤销到 1 个会话）、C10（8 进程并发刷新与
        // 多标签运维不能被一次重放全部踢下线）。撤销范围收窄到该 stale 会话本身。
        let third = svc
            .refresh(&second.tokens.refresh_token)
            .expect("重放者被拒，但合法会话不能被牵连");
        assert_ne!(third.tokens.refresh_token, second.tokens.refresh_token);
        assert!(
            store.read(|db| db.sessions.values().any(|s| s.revoked_at.is_none())),
            "重放检测不应把用户所有会话一次性清空"
        );
    }

    /// 同一个 refresh token 并发刷新：必须恰好一个赢。
    /// 旧实现是先 read 判断 active、再 write 撤销，两个请求都能通过 → 各换一对令牌。
    #[test]
    fn concurrent_refresh_only_one_wins() {
        let (config, store, metrics, audit, limiter) = fixture();
        let r = register(&config, &store, &metrics, audit.as_ref(), &limiter, "bob", "abc1234567", "2.2.2.2").unwrap();
        let svc = AuthService {
            config: &config,
            store: &store,
            metrics: &metrics,
            audit: audit.as_ref(),
            limiter: &limiter,
        };
        let token = r.tokens.refresh_token.clone();
        let (a, b) = std::thread::scope(|s| {
            let h1 = s.spawn(|| svc.refresh(&token).is_ok());
            let h2 = s.spawn(|| svc.refresh(&token).is_ok());
            (h1.join().unwrap(), h2.join().unwrap())
        });
        assert_eq!(
            u8::from(a) + u8::from(b),
            1,
            "同一 refresh token 并发刷新只能成功一次（实际成功 {a} / {b}）"
        );
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

    /// 假哈希的迭代数必须跟着配置走。
    /// 曾经的 bug：写死 `$1$`（1 次迭代），账号不存在时几十微秒返回、存在的账号要跑满
    /// 12 万次，攻击者测个响应时间就能把用户名枚举出来。
    #[test]
    fn dummy_hash_uses_configured_iterations() {
        let h = dummy_hash(4096).unwrap();
        let iters: u32 = h.split('$').nth(1).unwrap().parse().unwrap();
        assert_eq!(iters, 4096, "假哈希迭代数必须等于配置值，不能退回廉价哈希");
        assert!(
            !password::verify_password("随便什么密码", &h),
            "假哈希不允许被任何密码命中"
        );
    }

}
