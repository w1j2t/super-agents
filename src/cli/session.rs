//! 会话层：把"我是谁"从一次 `login` 取出来，后续命令复用。
//!
//! ## 为什么 CLI 要自己存令牌
//!
//! 所有 `/api/v1/tasks` 接口都要 `Authorization: Bearer <access_token>`（默认 1800 秒）。
//! 如果每次 `ls` 都要求用户抄一遍 JWT，这个 CLI 就没人会用；
//! 所以 `login` 成功后落一份令牌到本地，`submit` / `ls` / `show` 自动读取。
//!
//! ## 令牌文件放哪
//!
//! `SA_TOKEN_FILE` 优先；否则 `SA_DATA_DIR/cli-token`（`SA_DATA_DIR` 缺省是 `data`）。
//! 不另造目录、不写进 `~/.config`：服务的自检、清理、备份都围着 `SA_DATA_DIR` 转，
//! 令牌混进另一个位置，换机时就会"服务搬过去了、CLI 却没登录"。
//!
//! **文件权限**：这里只做 `0600` 之外的尽力而为——Windows 上 chmod 语义不同，
//! 靠的是用户目录 ACL。令牌本身是短期（1800s）且可撤的，风险与"浏览器里存个 cookie"同级。
//!
//! ## 为什么不把 token 打到屏幕上
//!
//! 一旦 `login` 会把 token 打印出来，它就会出现在 shell 历史、CI 日志、门禁证据文件里。
//! 所以 `login` 只报"登录成功 + 有效期"，需要原值请自己去看文件（`--show-token` 才打印）。

use std::io::Write;
use std::path::{Path, PathBuf};

use serde_json::Value;

use super::http;

/// 客户端需要知道的全部配置只有两项——地址和令牌文件。
///
/// 刻意**不**复用 `Config::from_env()`：那是服务端的完整校验（JWT 密钥、槽位枚举……）。
/// 如果 CLI 也走它，用户就得为了执行 `ls` 而在客户端环境里配好 `SA_JWT_SECRET`，
/// 这显然是荒谬的。缺省值与 `Config` 保持一致（`SA_BIND` → `127.0.0.1:8088`），
/// 由 `doctor` 在服务端那一侧负责校验。
pub struct Endpoint {
    pub addr: String,
    pub token_file: PathBuf,
}

impl Endpoint {
    /// `addr_override` 来自 `--addr`，优先级最高。
    pub fn from_env(addr_override: Option<&str>) -> Self {
        let addr = addr_override
            .map(|s| s.to_string())
            .or_else(|| std::env::var("SA_BIND").ok())
            .unwrap_or_else(|| "127.0.0.1:8088".to_string())
            .trim()
            .to_string();
        // 允许直接写 `http://127.0.0.1:8088`：人复制粘贴 URL 时不管有没有 scheme。
        let addr = addr
            .trim_start_matches("http://")
            .trim_start_matches("https://")
            .trim_end_matches('/')
            .to_string();
        let token_file = std::env::var("SA_TOKEN_FILE")
            .map(PathBuf::from)
            .unwrap_or_else(|_| {
                PathBuf::from(std::env::var("SA_DATA_DIR").unwrap_or_else(|_| "data".into()))
                    .join("cli-token")
            });
        Self { addr, token_file }
    }
}

pub struct Token {
    pub access: String,
    pub refresh: Option<String>,
}

const K_ACCESS: &str = "access=";
const K_REFRESH: &str = "refresh=";

pub fn load_token(path: &Path) -> Option<Token> {
    let text = std::fs::read_to_string(path).ok()?;
    let mut access = None;
    let mut refresh = None;
    for line in text.lines() {
        if let Some(v) = line.strip_prefix(K_ACCESS) {
            access = Some(v.trim().to_string());
        } else if let Some(v) = line.strip_prefix(K_REFRESH) {
            refresh = Some(v.trim().to_string());
        }
    }
    access.map(|a| Token {
        access: a,
        refresh,
    })
}

pub fn save_token(path: &Path, t: &Token) -> Result<(), String> {
    if let Some(dir) = path.parent() {
        if !dir.as_os_str().is_empty() {
            std::fs::create_dir_all(dir)
                .map_err(|e| format!("建令牌目录失败 {}：{e}", dir.display()))?;
        }
    }
    let mut body = format!("{K_ACCESS}{}\n", t.access);
    if let Some(r) = &t.refresh {
        body.push_str(&format!("{K_REFRESH}{r}\n"));
    }
    // 先写临时文件再改名：中途崩掉也不会留下"半行令牌"这种最难查的东西。
    let tmp = path.with_extension("tmp");
    {
        let mut f = std::fs::File::create(&tmp)
            .map_err(|e| format!("写令牌失败 {}：{e}", tmp.display()))?;
        f.write_all(body.as_bytes())
            .map_err(|e| format!("写令牌失败 {}：{e}", tmp.display()))?;
        f.flush().ok();
    }
    std::fs::rename(&tmp, path).map_err(|e| format!("落盘令牌失败 {}：{e}", path.display()))?;
    restrict_permissions(path);
    Ok(())
}

#[cfg(unix)]
fn restrict_permissions(path: &Path) {
    use std::os::unix::fs::PermissionsExt;
    let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
}

#[cfg(not(unix))]
fn restrict_permissions(_path: &Path) {
    // Windows：文件继承用户目录 ACL，生产环境里通常只有本人与 Administrators 可读。
    // 不做 `icacls` 调用——它需要额外进程、会拖慢 `login`，收益却在边界情况才有。
}

pub fn clear_token(path: &Path) -> bool {
    match std::fs::remove_file(path) {
        Ok(()) => true,
        Err(_) => false,
    }
}

pub struct LoginOk {
    pub token: Token,
    pub display: String,
    pub role: String,
    pub expires_in: i64,
}

pub fn login(ep: &Endpoint, account: &str, password: &str) -> Result<LoginOk, String> {
    let body = serde_json::json!({ "account": account, "password": password }).to_string();
    let r = http::request(&ep.addr, "POST", "/api/v1/auth/login", None, Some(&body))?;
    if !r.is_ok() {
        return Err(server_error(&r));
    }
    let v: Value = serde_json::from_str(&r.body)
        .map_err(|e| format!("登录响应不是 JSON：{e}\n原文：{}", brief(&r.body)))?;
    let tokens = v.get("tokens").cloned().unwrap_or(Value::Null);
    let access = tokens
        .get("access_token")
        .and_then(|x| x.as_str())
        .ok_or_else(|| format!("登录响应里没有 tokens.access_token：{}", brief(&r.body)))?
        .to_string();
    let refresh = tokens
        .get("refresh_token")
        .and_then(|x| x.as_str())
        .map(|s| s.to_string());
    let expires_in = tokens.get("expires_in").and_then(|x| x.as_i64()).unwrap_or(0);
    let user = v.get("user").cloned().unwrap_or(Value::Null);
    let display = user
        .get("display_name")
        .and_then(|x| x.as_str())
        .or_else(|| user.get("username").and_then(|x| x.as_str()))
        .unwrap_or(account)
        .to_string();
    let role = user
        .get("role")
        .map(|x| match x.as_str() {
            Some(s) => s.to_string(),
            None => x.to_string(),
        })
        .unwrap_or_else(|| "?".into());
    Ok(LoginOk {
        token: Token { access, refresh },
        display,
        role,
        expires_in,
    })
}

/// 用 refresh_token 换一对新令牌。失败不致命——调用方会退化成"提示重新登录"。
pub fn refresh(ep: &Endpoint, old: &Token) -> Result<Token, String> {
    let rt = old
        .refresh
        .as_ref()
        .ok_or_else(|| "本地没有 refresh_token".to_string())?;
    let body = serde_json::json!({ "refresh_token": rt }).to_string();
    let r = http::request(&ep.addr, "POST", "/api/v1/auth/refresh", None, Some(&body))?;
    if !r.is_ok() {
        return Err(server_error(&r));
    }
    let v: Value =
        serde_json::from_str(&r.body).map_err(|e| format!("刷新响应不是 JSON：{e}"))?;
    // 服务端统一返回 { user, tokens: { access_token, refresh_token, … } }（见 AuthResult）。
    // 兼容历史写法 new_tok / new_rtok / 顶层 access_token。两条链必须同样宽——
    // 曾漏掉 tokens.refresh_token 一层，导致刷新拿到的新 refresh 被丢掉、文件里
    // 留着已被服务端作废的旧 refresh，下一条命令必然再 401 并误报"请重新登录"。
    let pick = |k: &str| {
        v.get(k)
            .or_else(|| v.get("tokens").and_then(|t| t.get(k)))
            .and_then(|x| x.as_str())
            .map(|s| s.to_string())
    };
    let access = pick("new_tok")
        .or_else(|| pick("access_token"))
        .ok_or_else(|| format!("刷新响应里没有新令牌：{}", brief(&r.body)))?;
    let refresh = pick("new_rtok")
        .or_else(|| pick("refresh_token"))
        .or_else(|| old.refresh.clone());
    Ok(Token { access, refresh })
}

/// 服务端拒绝时，把 opendapi 的 `Problem` 形状尽量读成人话：
/// 优先 `detail` / `title`，带 `status` 与 `code`，都读不到就截断原文。
pub fn server_error(r: &http::Response) -> String {
    let v: Option<Value> = serde_json::from_str(&r.body).ok();
    let pick = |k: &str| {
        v.as_ref()
            .and_then(|x| x.get(k))
            .and_then(|x| x.as_str())
            .map(|s| s.to_string())
    };
    let mut msg = format!("HTTP {}", r.status);
    if let Some(d) = pick("detail").or_else(|| pick("title")) {
        msg.push_str(" · ");
        msg.push_str(&d);
    }
    if let Some(c) = pick("code") {
        msg.push(' ');
        msg.push('[');
        msg.push_str(&c);
        msg.push(']');
    }
    if msg == format!("HTTP {}", r.status) {
        msg.push_str(" · ");
        msg.push_str(&brief(&r.body));
    }
    msg
}

/// 错误里引用响应原文时只给一小截：完整 HTML 错误页会淹掉真正的原因。
pub fn brief(s: &str) -> String {
    let t = s.trim();
    let cut: String = t.chars().take(200).collect();
    if t.chars().count() > 200 {
        format!("{cut}…")
    } else {
        cut
    }
}
