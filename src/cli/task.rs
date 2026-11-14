//! 任务相关子命令：`submit` / `ls` / `show`。
//!
//! ## 这一层刻意很薄
//!
//! 所有字段、枚举、状态机都在服务端，CLI 只做三件事：拼 URL、带令牌、把 JSON 读成人能看的几行。
//! 一旦这里开始"自己判断任务成功没成功""自己算要不要重试"，就出现了第二套业务规则，
//! 而两套规则迟早不一致——那种故障最难查，因为 CLI 和服务端各自都能自圆其说。
//!
//! 所以：**响应里没有的字段，这里绝不推测**；读不到就显示 `-`，而不是 0 或"未知错误"。
//!
//! ## 退出码（与 `doctor` 的 0/1/2 语义对齐，只多一个 3）
//!
//! - `0` 成功
//! - `1` 本地问题：没登录、连不上、参数不成立
//! - `2` 服务端拒绝：4xx / 5xx（原因已打印）
//! - `3` 需要重新登录：401 且刷新也没成功——脚本靠这个码决定"要不要先 login"

use serde_json::Value;

use super::http;
use super::session::{self, Endpoint, Token};
use super::{ConnArgs, LsArgs, ShowArgs, SubmitArgs};

pub const RC_OK: i32 = 0;
pub const RC_LOCAL: i32 = 1;
pub const RC_SERVER: i32 = 2;
pub const RC_AUTH: i32 = 3;

pub const TASKS: &str = "/api/v1/tasks";

/// 带上令牌发请求；401 就刷新一次再试。
///
/// 只重试一次是有意的：refresh 也被拒说明令牌体系已经不可用（改过 `SA_JWT_SECRET`、
/// 或用户被删了），再重试只是把同一个错误打三遍。
fn call(
    ep: &Endpoint,
    tok: &mut Token,
    method: &str,
    path: &str,
    body: Option<&str>,
) -> Result<http::Response, i32> {
    let r = match http::request(&ep.addr, method, path, Some(&tok.access), body) {
        Ok(r) => r,
        Err(e) => {
            eprintln!("{e}");
            return Err(RC_LOCAL);
        }
    };
    if r.status != 401 {
        return Ok(r);
    }
    match session::refresh(ep, tok) {
        Ok(new) => {
            *tok = new;
            if let Err(e) = session::save_token(&ep.token_file, tok) {
                eprintln!("警告：新令牌没能落盘（{e}），下次还得重新登录");
            }
            match http::request(&ep.addr, method, path, Some(&tok.access), body) {
                Ok(r2) => Ok(r2),
                Err(e) => {
                    eprintln!("{e}");
                    Err(RC_LOCAL)
                }
            }
        }
        Err(e) => {
            match retry_after_peer_refresh(ep, tok, method, path, body) {
                Retry::Rescued(r2) => return Ok(r2),
                Retry::WaitedButStillOld => {
                    // 等过了仍然没看到别人换出来的新令牌：多半是同一个令牌在两个终端
                    // 里同时过期了。别急着说"令牌已失效"——重跑一次就会成功一个。
                    eprintln!("令牌刷新被拒：{e}");
                    eprintln!("若刚才另一个终端也在用，请再执行一次；一直这样的话执行：super-agents login");
                    return Err(RC_AUTH);
                }
                Retry::Hopeless => {}
            }
            eprintln!("令牌已失效，且刷新失败：{e}");
            eprintln!("请重新登录：super-agents login");
            Err(RC_AUTH)
        }
    }
}

enum Retry {
    /// 抢救成功：对端已换出新令牌，且拿它把请求做成了。
    Rescued(http::Response),
    /// 等了一段仍然只看到自己手上这对旧令牌。
    WaitedButStillOld,
    /// 看到了新令牌却还是 401，或者本地根本没令牌文件——真的没救了。
    Hopeless,
}

/// 服务端 refresh 是「轮换 + 重放检测」：同一个 refresh 只认第一次，后到的直接 401。
/// 两个终端同时跑命令时，必然有一个刷不上。此时另一个人已经把新令牌写回文件了，
/// 读出来用即可，不必让用户重新登录。
///
/// 这里有竞态：失败方重读文件的瞬间，成功方可能还没写完。所以不是读一次就罢休，
/// 而是退避重读（8 次 × 60ms ≈ 半秒）——这个窗口足够覆盖一次本地文件写入。
fn retry_after_peer_refresh(
    ep: &Endpoint,
    tok: &mut Token,
    method: &str,
    path: &str,
    body: Option<&str>,
) -> Retry {
    for attempt in 0..8 {
        if attempt > 0 {
            std::thread::sleep(std::time::Duration::from_millis(60));
        }
        let Some(fresh) = session::load_token(&ep.token_file) else {
            // 没有令牌文件，等也等不出东西来（比如用了 --token 直传）
            return if attempt == 0 { Retry::Hopeless } else { Retry::WaitedButStillOld };
        };
        if fresh.refresh.is_none() || fresh.refresh == tok.refresh {
            continue;
        }
        *tok = fresh;
        return match http::request(&ep.addr, method, path, Some(&tok.access), body) {
            Ok(r) if r.status != 401 => Retry::Rescued(r),
            Ok(_) => Retry::Hopeless,
            Err(_) => Retry::WaitedButStillOld,
        };
    }
    Retry::WaitedButStillOld
}

/// 令牌来源优先级：`--token` > `SA_TOKEN` > 本地令牌文件。
fn need_token(conn: &ConnArgs, ep: &Endpoint) -> Result<Token, i32> {
    if let Some(t) = &conn.token {
        return Ok(Token {
            access: t.clone(),
            refresh: None,
        });
    }
    if let Ok(t) = std::env::var("SA_TOKEN") {
        if !t.trim().is_empty() {
            return Ok(Token {
                access: t.trim().to_string(),
                refresh: None,
            });
        }
    }
    match session::load_token(&ep.token_file) {
        Some(t) => Ok(t),
        None => {
            eprintln!(
                "还没登录（找不到 {}）。先跑：super-agents login --account <用户名或邮箱>",
                ep.token_file.display()
            );
            Err(RC_AUTH)
        }
    }
}

pub fn submit(conn: &ConnArgs, a: &SubmitArgs) -> i32 {
    let ep = Endpoint::from_env(conn.addr.as_deref());
    let mut tok = match need_token(conn, &ep) {
        Ok(t) => t,
        Err(rc) => return rc,
    };

    // 只提交用户显式给的字段。缺省值归服务端管——
    // 门禁 17 守的就是"POST /tasks 的 mode 缺省"，CLI 在这里补一个默认值，
    // 那条契约就永远测不出来了。
    let mut body = serde_json::Map::new();
    body.insert("goal".into(), Value::String(a.goal.clone()));
    if let Some(v) = &a.task_type {
        body.insert("task_type".into(), Value::String(v.clone()));
    }
    if let Some(v) = &a.mode {
        body.insert("mode".into(), Value::String(v.clone()));
    }
    if let Some(v) = &a.model_profile {
        body.insert("model_profile".into(), Value::String(v.clone()));
    }
    if let Some(v) = a.budget_tokens {
        body.insert("budget_tokens".into(), Value::Number(v.into()));
    }
    let payload = Value::Object(body).to_string();

    let r = match call(&ep, &mut tok, "POST", TASKS, Some(&payload)) {
        Ok(r) => r,
        Err(rc) => return rc,
    };
    if !r.is_ok() {
        eprintln!("提交失败：{}", session::server_error(&r));
        return RC_SERVER;
    }
    let v = match serde_json::from_str::<Value>(&r.body) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("响应不是 JSON（{e}）：{}", session::brief(&r.body));
            return RC_SERVER;
        }
    };

    if a.json {
        println!("{v}");
        return RC_OK;
    }
    // 201 = 同步跑完返回 TaskDetail；202 = 已受理。两者都用同一套渲染，
    // 差别只在末尾提示，因为 202 的任务还需要再看一次。
    println!("已提交 HTTP {}", r.status);
    render_detail(&v, "  ");
    if r.status == 202 {
        if let Some(id) = v.get("id").and_then(|x| x.as_str()) {
            println!("  提示  异步执行中：super-agents show {id}");
        }
    }
    RC_OK
}

pub fn ls(conn: &ConnArgs, a: &LsArgs) -> i32 {
    let ep = Endpoint::from_env(conn.addr.as_deref());
    let mut tok = match need_token(conn, &ep) {
        Ok(t) => t,
        Err(rc) => return rc,
    };

    let mut q: Vec<String> = Vec::new();
    if let Some(v) = a.limit {
        q.push(format!("limit={v}"));
    }
    if let Some(v) = a.offset {
        q.push(format!("offset={v}"));
    }
    if let Some(v) = &a.status {
        q.push(format!("status={}", urlencode(v)));
    }
    if let Some(v) = &a.task_type {
        q.push(format!("task_type={}", urlencode(v)));
    }
    if let Some(v) = &a.q {
        q.push(format!("q={}", urlencode(v)));
    }
    let path = if q.is_empty() {
        TASKS.to_string()
    } else {
        format!("{TASKS}?{}", q.join("&"))
    };

    let r = match call(&ep, &mut tok, "GET", &path, None) {
        Ok(r) => r,
        Err(rc) => return rc,
    };
    if !r.is_ok() {
        eprintln!("查询失败：{}", session::server_error(&r));
        return RC_SERVER;
    }
    let v: Value = match serde_json::from_str(&r.body) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("响应不是 JSON（{e}）：{}", session::brief(&r.body));
            return RC_SERVER;
        }
    };
    if a.json {
        println!("{v}");
        return RC_OK;
    }

    let items = v
        .get("items")
        .and_then(|x| x.as_array())
        .cloned()
        .unwrap_or_default();
    let total = num_field(&v, "total");
    if items.is_empty() {
        println!("没有任务（共 {total} 条）");
        return RC_OK;
    }

    // 列宽按"最长真实取值"定，不做二次截断：
    // id 是 UUID 36 位，截成 `204750aa-5cd7-4dda-…` 就没法复制去 `show` 了；
    // `partially_succeeded` 19 位、`collaboration` 13 位，都曾把列撑破。
    // 表会宽到 140 列左右——这是运维 CLI，可复制 > 好看。
    const W_ID: usize = 36;
    const W_ST: usize = 19;
    const W_TY: usize = 13;
    const W_TOK: usize = 13;
    const W_TIME: usize = 19;
    println!(
        "{} {} {} {} {} {}",
        pad("id", W_ID),
        pad("状态", W_ST),
        pad("类型", W_TY),
        pad("tokens", W_TOK),
        pad("创建时间(UTC)", W_TIME),
        "目标"
    );
    for it in &items {
        let goal = it.get("goal").and_then(|x| x.as_str()).unwrap_or("-");
        println!(
            "{} {} {} {} {} {}",
            pad(&trunc(&str_field(it, "id"), W_ID), W_ID),
            pad(&trunc(&str_field(it, "status"), W_ST), W_ST),
            pad(&trunc(&str_field(it, "task_type"), W_TY), W_TY),
            pad(&trunc(&tokens_cell(it), W_TOK), W_TOK),
            pad(&ts(&str_field(it, "created_at")), W_TIME),
            trunc(goal, 40),
        );
    }

    let limit = num_field(&v, "limit");
    let offset = num_field(&v, "offset");
    let has_more = v
        .get("has_more")
        .and_then(|x| x.as_bool())
        .unwrap_or(false);
    let mut foot = format!(
        "共 {total} 条，本页 {} 条（limit={limit} offset={offset}；时间为 UTC）",
        items.len()
    );
    if has_more {
        let next = offset + items.len() as u64;
        foot.push_str(&format!("，还有更多：--offset {next}"));
    }
    println!("{foot}");
    RC_OK
}

pub fn show(conn: &ConnArgs, a: &ShowArgs) -> i32 {
    let ep = Endpoint::from_env(conn.addr.as_deref());
    let mut tok = match need_token(conn, &ep) {
        Ok(t) => t,
        Err(rc) => return rc,
    };
    let path = format!("{TASKS}/{}", urlencode_path(&a.id));
    let r = match call(&ep, &mut tok, "GET", &path, None) {
        Ok(r) => r,
        Err(rc) => return rc,
    };
    if r.status == 404 {
        eprintln!("没有这个任务：{}", a.id);
        return RC_SERVER;
    }
    if !r.is_ok() {
        eprintln!("查询失败：{}", session::server_error(&r));
        return RC_SERVER;
    }
    let v: Value = match serde_json::from_str(&r.body) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("响应不是 JSON（{e}）：{}", session::brief(&r.body));
            return RC_SERVER;
        }
    };
    if a.json {
        println!("{v}");
        return RC_OK;
    }
    render_detail(&v, "");
    RC_OK
}

/// detail 渲染：只打印响应里真的有的键。少了就少一行，不补 0、不写"未知"。
///
/// 时间一律标 `(UTC)`：服务端给的是 `...Z`，而 Rust 标准库拿不到本地时区偏移
/// （换算要靠平台 API 或第三方库）。与其猜一个 `+8` 猜错，不如把口径写在标签上。
fn render_detail(v: &Value, indent: &str) {
    let rows: [(&str, String); 10] = [
        ("id", str_field(v, "id")),
        ("状态", str_field(v, "status")),
        ("类型", str_field(v, "task_type")),
        ("模型", str_field(v, "model_profile")),
        ("tokens", tokens_cell(v)),
        (
            "一致度",
            match v.get("agreement_score").and_then(|x| x.as_f64()) {
                Some(s) => format!("{s:.2}"),
                None => "-".into(),
            },
        ),
        ("创建(UTC)", ts(&str_field(v, "created_at"))),
        ("更新(UTC)", ts(&str_field(v, "updated_at"))),
        ("完成(UTC)", ts(&str_field(v, "finished_at"))),
        ("目标", trunc(&str_field(v, "goal"), 96)),
    ];
    for (k, val) in rows {
        if val == "-" {
            continue;
        }
        println!("{indent}{}{}", pad(k, 11), val);
    }
    let steps = v
        .get("plan")
        .and_then(|p| p.get("steps"))
        .and_then(|s| s.as_array())
        .map(|s| s.len());
    if let Some(n) = steps {
        println!("{indent}{}{} 步", pad("计划", 11), n);
    }
    if let Some(s) = v.get("result_summary").and_then(|x| x.as_str()) {
        if !s.trim().is_empty() {
            println!("{indent}摘要");
            for line in s.lines().take(12) {
                println!("{indent}  {line}");
            }
        }
    }
}

fn str_field(v: &Value, k: &str) -> String {
    v.get(k)
        .and_then(|x| x.as_str())
        .map(|s| s.to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "-".into())
}

fn num_field(v: &Value, k: &str) -> u64 {
    v.get(k).and_then(|x| x.as_u64()).unwrap_or(0)
}

/// `1234/40000`：预算为 0 时只显示用量，不拼一个假的 `/0`。
fn tokens_cell(v: &Value) -> String {
    let used = num_field(v, "tokens_used");
    let budget = num_field(v, "budget_tokens");
    if budget > 0 {
        format!("{used}/{budget}")
    } else {
        used.to_string()
    }
}

/// `2026-11-14T15:30:12.123456+08:00` → `2026-11-14 15:30:12`。
fn ts(s: &str) -> String {
    if s == "-" {
        return s.to_string();
    }
    let t = s.replace('T', " ");
    let head: String = t.chars().take(19).collect();
    if head.len() >= 19 {
        head
    } else {
        t
    }
}

/// 显示宽度：CJK / 全角算 2 格，其余算 1 格。
///
/// 不引 `unicode-width`：这里只对齐中英混排的表格，覆盖到常见区间就够了，
/// 而且宁可估错一两格，也不要为对齐多加一个依赖。
fn dwidth(s: &str) -> usize {
    s.chars()
        .map(|c| {
            let u = c as u32;
            let wide = (0x1100..=0x115F).contains(&u)
                || (0x2E80..=0xA4CF).contains(&u)
                || (0xAC00..=0xD7A3).contains(&u)
                || (0xF900..=0xFAFF).contains(&u)
                || (0xFE30..=0xFE6F).contains(&u)
                || (0xFF00..=0xFF60).contains(&u)
                || (0xFFE0..=0xFFE6).contains(&u)
                || (0x20000..=0x3FFFD).contains(&u);
            if wide {
                2
            } else {
                1
            }
        })
        .sum()
}

fn pad(s: &str, w: usize) -> String {
    let d = dwidth(s);
    let mut out = s.to_string();
    for _ in d..w {
        out.push(' ');
    }
    out
}

/// 按显示宽度截断，超长补 `…`。
fn trunc(s: &str, w: usize) -> String {
    if dwidth(s) <= w {
        return s.to_string();
    }
    let mut out = String::new();
    let mut used = 0usize;
    for c in s.chars() {
        let cw = dwidth(&c.to_string());
        if used + cw > w.saturating_sub(1) {
            break;
        }
        out.push(c);
        used += cw;
    }
    out.push('…');
    out
}

/// 查询参数里只转义会破坏 URL 结构的字符。
///
/// 不为中文做百分号编码：hyper 侧接受原始 UTF-8 字节，
/// 而手工百分号编码反而容易把用户搜索词弄坏（双重编码是这类 bug 的常客）。
fn urlencode(s: &str) -> String {
    let mut out = String::new();
    for c in s.chars() {
        match c {
            '&' => out.push_str("%26"),
            '#' => out.push_str("%23"),
            '+' => out.push_str("%2B"),
            '=' => out.push_str("%3D"),
            ' ' => out.push_str("%20"),
            _ => out.push(c),
        }
    }
    out
}

/// 路径段里 `/` 会把 id 变成两级路径，必须转义。
fn urlencode_path(s: &str) -> String {
    urlencode(s).replace('/', "%2F")
}
