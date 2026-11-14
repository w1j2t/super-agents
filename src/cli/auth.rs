//! `login` / `logout` 的交互层：只负责"把账号密码凑齐"和"把结果说人话"。
//!
//! 真正的网络与落盘在 `session.rs`；这里不重复任何协议细节。
//!
//! ## 密码为什么不建议写在命令行上
//!
//! `--password xxx` 会进 shell 历史、进 CI 日志、进屏幕录制。所以：
//! - 优先读 `SA_PASSWORD`（环境变量，进程级，不落历史）；
//! - 都没给才从 stdin 读一行，并且**明确告诉用户这一行会回显**——
//!   标准库没有无回显读密码的能力，与其假装安全，不如把限制说清楚。
//!
//! 令牌同理：`login` 默认**不打印** access_token，免得它出现在证据文件里；
//! 确实需要（比如喂给别的工具）再显式加 `--show-token`。

use std::io::Write;

use super::session::{self, Endpoint};
use super::{LoginArgs, LogoutArgs};

const RC_OK: i32 = 0;
const RC_LOCAL: i32 = 1;
const RC_SERVER: i32 = 2;

pub fn login(a: &LoginArgs) -> i32 {
    let ep = Endpoint::from_env(a.conn.addr.as_deref());

    let account = match a
        .account
        .clone()
        .or_else(|| std::env::var("SA_ACCOUNT").ok().filter(|s| !s.trim().is_empty()))
    {
        Some(v) => v.trim().to_string(),
        None => match prompt("用户名或邮箱：") {
            Some(v) if !v.trim().is_empty() => v.trim().to_string(),
            _ => {
                eprintln!("没有账号。用 --account，或先设 SA_ACCOUNT");
                return RC_LOCAL;
            }
        },
    };

    let password = match a
        .password
        .clone()
        .or_else(|| std::env::var("SA_PASSWORD").ok().filter(|s| !s.is_empty()))
    {
        Some(v) => v,
        None => {
            eprintln!("提示：下面的输入会显示在屏幕上（标准库无法关闭回显）。");
            eprintln!("      更稳妥的做法是设环境变量 SA_PASSWORD 后重试。");
            match prompt("密码：") {
                Some(v) if !v.is_empty() => v,
                _ => {
                    eprintln!("没有密码。用 SA_PASSWORD 环境变量，或 --password");
                    return RC_LOCAL;
                }
            }
        }
    };

    match session::login(&ep, &account, &password) {
        Ok(ok) => {
            if let Err(e) = session::save_token(&ep.token_file, &ok.token) {
                // 落盘失败不算登录失败：这一次的令牌照样能用，只是下次得重新登录。
                eprintln!("警告：令牌没能落盘（{e}）；本次可用，下次要重新登录");
            }
            println!(
                "登录成功：{}（{}）",
                ok.display,
                if ok.role.is_empty() { "?" } else { &ok.role }
            );
            if ok.expires_in > 0 {
                println!("  有效期  {} 秒（过期后会自动用 refresh_token 续一次）", ok.expires_in);
            }
            println!("  令牌存  {}", ep.token_file.display());
            if a.show_token {
                println!("  token   {}", ok.token.access);
            } else {
                println!("  （access_token 不打印；需要时加 --show-token）");
            }
            RC_OK
        }
        Err(msg) => {
            eprintln!("登录失败：{msg}");
            // 连不上 vs 被拒：错误文案里已经带 HTTP 状态码的算"服务端拒绝"，其余算本地。
            if msg.starts_with("HTTP ") {
                RC_SERVER
            } else {
                RC_LOCAL
            }
        }
    }
}

pub fn logout(a: &LogoutArgs) -> i32 {
    let ep = Endpoint::from_env(a.conn.addr.as_deref());
    if session::clear_token(&ep.token_file) {
        println!("已删除本地令牌：{}", ep.token_file.display());
        println!("（服务端会话未受影响；refresh_token 到期后自然失效）");
    } else {
        println!("本来就没登录（找不到 {}）", ep.token_file.display());
    }
    RC_OK
}

fn prompt(label: &str) -> Option<String> {
    print!("{label}");
    let _ = std::io::stdout().flush();
    let mut line = String::new();
    match std::io::stdin().read_line(&mut line) {
        Ok(0) => None, // EOF（比如被管道喂了空输入）
        Ok(_) => Some(line.trim_end_matches(['\r', '\n']).to_string()),
        Err(_) => None,
    }
}
