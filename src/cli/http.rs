//! 最小 HTTP/1.1 客户端：只为"跟本机这台服务说话"。
//!
//! ## 为什么不引 reqwest / hyper-util
//!
//! 三个理由，按重要性排：
//!
//! 1. **`doctor` 的承诺是"依赖都没装好时也能跑"**。它已经用 `TcpStream` 手写了一版
//!    GET（见 `doctor::http_status`）。CLI 若换一套 client crate，就出现了两条网络路径：
//!    一条能跑一条不能跑，出问题时"到底谁在超时"要多查一遍。
//! 2. 这里要的协议面窄得可怜：本机、明文、HTTP/1.1、五个动词、JSON。
//!    引一个带连接池 / 重定向 / 代理 / TLS 的 client，等于为 5% 的能力付 100% 的编译时间。
//! 3. 门禁 0 比的是**源码内容指纹**，`Cargo.toml` 加一行就会触发全量重编。
//!    少一个依赖，少一次全量的理由。
//!
//! ## `Connection: close` 不是省事，是**把解析难度降级**
//!
//! 一请求一连接、读完 EOF 即结束，于是不需要实现 keep-alive 的分帧状态机。
//! 服务端（hyper）见到这个头就一定在响应后关闭连接，边界天然清晰。
//!
//! chunked 解码仍然实现了一份：宁可多 30 行，也不要在某个代理/中间件插进来之后
//! 因为 `Transfer-Encoding: chunked` 而拿到半截 JSON 却报"JSON 解析失败"。

use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::time::Duration;

/// 响应实体上限。正常情况下最大的响应是 `GET /api/v1/tasks` 的一页列表（几十 KB）。
/// 设上限是为了在"对端不是本服务"时（比如误连到别的 HTTP 端口）不至于把内存读穿。
const MAX_BODY: usize = 16 * 1024 * 1024;

pub struct Response {
    pub status: u16,
    pub body: String,
}

impl Response {
    /// 2xx 才算成功。3xx 不跟：本服务不该有重定向，跟着走反而会掩盖配置错误。
    pub fn is_ok(&self) -> bool {
        (200..300).contains(&self.status)
    }
}

/// 发一个请求。`addr` 形如 `127.0.0.1:8080`（不带 scheme）。
pub fn request(
    addr: &str,
    method: &str,
    path: &str,
    bearer: Option<&str>,
    json_body: Option<&str>,
) -> Result<Response, String> {
    let sock = addr
        .to_socket_addrs()
        .map_err(|e| format!("地址解析失败 {addr}：{e}"))?
        .next()
        .ok_or_else(|| format!("地址解析结果为空：{addr}"))?;

    let mut stream = TcpStream::connect_timeout(&sock, Duration::from_secs(3)).map_err(|e| {
        format!("连不上 {addr}：{e}\n提示：服务没起？先跑 `super-agents doctor` 看监听端口那一项")
    })?;
    // 读超时给得很宽，因为 `submit` 默认跟随服务端的 mode 缺省（sync），
    // 真模型下一次同步任务实测过 129.9s。这里若按"接口该很快"设 5s，就会误杀正常任务。
    let _ = stream.set_read_timeout(Some(Duration::from_secs(300)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(10)));

    let mut req = format!(
        "{method} {path} HTTP/1.1\r\nHost: {addr}\r\nAccept: application/json\r\nConnection: close\r\n"
    );
    if let Some(t) = bearer {
        req.push_str("Authorization: Bearer ");
        req.push_str(t);
        req.push_str("\r\n");
    }
    match json_body {
        Some(b) => {
            req.push_str("Content-Type: application/json\r\n");
            req.push_str(&format!("Content-Length: {}\r\n\r\n", b.as_bytes().len()));
            req.push_str(b);
        }
        None => req.push_str("\r\n"),
    }

    stream
        .write_all(req.as_bytes())
        .map_err(|e| format!("发送请求失败：{e}"))?;
    let _ = stream.flush();

    let mut raw = Vec::new();
    let mut buf = [0u8; 8192];
    loop {
        match stream.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => {
                raw.extend_from_slice(&buf[..n]);
                if raw.len() > MAX_BODY {
                    return Err(format!(
                        "响应超过 {MAX_BODY} 字节上限，已放弃读取（对端像是别的服务？）"
                    ));
                }
            }
            Err(e) => return Err(format!("读取响应失败：{e}")),
        }
    }

    parse(&raw)
}

fn parse(raw: &[u8]) -> Result<Response, String> {
    let sep = find(raw, b"\r\n\r\n").ok_or_else(|| {
        let head = String::from_utf8_lossy(&raw[..raw.len().min(120)]);
        format!("响应不完整（没有头部结束标记）：{head}")
    })?;
    let head = String::from_utf8_lossy(&raw[..sep]).to_string();
    let mut lines = head.lines();
    let status_line = lines.next().unwrap_or_default();
    let status: u16 = status_line
        .split_whitespace()
        .nth(1)
        .and_then(|s| s.parse().ok())
        .ok_or_else(|| format!("无法解析状态行：{status_line:?}"))?;

    let mut chunked = false;
    let mut content_length: Option<usize> = None;
    for line in lines {
        let Some((k, v)) = line.split_once(':') else {
            continue;
        };
        match k.trim().to_ascii_lowercase().as_str() {
            "transfer-encoding" => {
                if v.to_ascii_lowercase().contains("chunked") {
                    chunked = true;
                }
            }
            "content-length" => content_length = v.trim().parse().ok(),
            _ => {}
        }
    }

    let rest = &raw[sep + 4..];
    let body = if chunked {
        dechunk(rest)?
    } else if let Some(cl) = content_length {
        rest[..rest.len().min(cl)].to_vec()
    } else {
        rest.to_vec()
    };

    Ok(Response {
        status,
        body: String::from_utf8_lossy(&body).to_string(),
    })
}

fn find(hay: &[u8], needle: &[u8]) -> Option<usize> {
    hay.windows(needle.len()).position(|w| w == needle)
}

/// `23\r\n{...}\r\n0\r\n\r\n` → `{...}`。畸形数据不 panic，能解多少解多少。
fn dechunk(mut data: &[u8]) -> Result<Vec<u8>, String> {
    let mut out = Vec::new();
    loop {
        let Some(pos) = find(data, b"\r\n") else {
            return Err("chunked 编码缺少长度行".into());
        };
        let size_line = String::from_utf8_lossy(&data[..pos]).to_string();
        let size_hex = size_line.split(';').next().unwrap_or("").trim().to_string();
        let size = usize::from_str_radix(&size_hex, 16)
            .map_err(|_| format!("chunked 长度非法：{size_hex:?}"))?;
        data = &data[pos + 2..];
        if size == 0 {
            return Ok(out);
        }
        if data.len() < size {
            return Err("chunked 数据被截断".into());
        }
        out.extend_from_slice(&data[..size]);
        data = &data[size..];
        // 每个 chunk 后面跟一个 CRLF，吃掉；最后一个 chunk 后面没有，靠上面的 size==0 收尾。
        if data.starts_with(b"\r\n") {
            data = &data[2..];
        }
    }
}
