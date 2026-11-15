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

// ---------------------------------------------------------------------------
// SSE 事件流：一次性的 `request()` 读不出"进行中"
// ---------------------------------------------------------------------------

/// 按行读一条事件流，**边到边抛**；回调返回 false 表示客户端主动收工。
///
/// 为什么不能复用 `request()`：它要读到 EOF 才返回，而事件流的 EOF 恰好等于
/// "任务到了终态"。拿它做 `submit --follow`，用户看到的是"提交完一声不响干等
/// 90 秒，然后进度哗一下全刷出来"——那不叫跟随，叫延迟回放。
///
/// 读超时按**心跳的三倍**（45s）给，而不是普通请求的 300s：事件流有"每 15s 一条
/// ping"的契约，45s 一个字节都没有就是链路死了。挂着等救不回来，也不如报错清楚。
pub fn stream_lines(
    addr: &str,
    path: &str,
    bearer: Option<&str>,
    mut on_line: impl FnMut(&str) -> bool,
) -> Result<(), String> {
    use std::io::{BufRead, BufReader};

    let sock = addr
        .to_socket_addrs()
        .map_err(|e| format!("地址解析失败 {addr}：{e}"))?
        .next()
        .ok_or_else(|| format!("地址解析结果为空：{addr}"))?;
    let mut stream = TcpStream::connect_timeout(&sock, Duration::from_secs(3)).map_err(|e| {
        format!("连不上 {addr}：{e}\n提示：服务没起？先跑 `super-agents doctor` 看监听端口那一项")
    })?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(45)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(10)));

    let mut req = format!(
        "GET {path} HTTP/1.1\r\nHost: {addr}\r\nAccept: text/event-stream\r\n\
         Cache-Control: no-cache\r\nConnection: close\r\n"
    );
    if let Some(t) = bearer {
        req.push_str("Authorization: Bearer ");
        req.push_str(t);
        req.push_str("\r\n");
    }
    req.push_str("\r\n");
    stream
        .write_all(req.as_bytes())
        .map_err(|e| format!("发送请求失败：{e}"))?;

    let mut br = BufReader::new(stream);
    let mut status = 0u16;
    let mut chunked = false;
    let mut head = String::new();
    loop {
        head.clear();
        let n = br
            .read_line(&mut head)
            .map_err(|e| format!("读取事件流响应头失败：{e}"))?;
        if n == 0 {
            return Err("响应头没读完连接就断了".into());
        }
        let l = head.trim_end_matches(['\r', '\n']);
        if l.is_empty() {
            break;
        }
        let low = l.to_ascii_lowercase();
        if let Some(rest) = low.strip_prefix("http/1.1 ") {
            status = rest
                .split_whitespace()
                .next()
                .and_then(|s| s.parse().ok())
                .unwrap_or(0);
        } else if low.starts_with("transfer-encoding:") {
            chunked = low.contains("chunked");
        }
    }
    if status != 200 {
        // 不在这里决定提示口径：上层要能区分 401（可刷令牌重连）与真错。
        return Err(format!("事件流返回 HTTP {status}"));
    }
    if !chunked {
        // 没有分块编码就没有"哪一段结束了"的边界，硬按字节切会把多字节汉字劈成两半。
        // 本服务用 axum::Sse，一定是 chunked；万一真不是，如实报错，别猜着读。
        return Err("事件流没有 chunked 编码，无法增量读取".into());
    }

    let mut chunk: Vec<u8> = Vec::new();
    // chunk 边界可以落在任何位置，包括一行中间，甚至一个汉字中间。
    // 所以按字节攒，遇到 '\n' 才切出整行——这样多字节字符永远不会被截断。
    let mut carry: Vec<u8> = Vec::new();
    loop {
        let mut size_line = String::new();
        let n = br.read_line(&mut size_line).map_err(describe_read_err)?;
        if n == 0 {
            break;
        }
        let s = size_line.trim();
        if s.is_empty() {
            continue; // chunk 与 chunk 之间的空行
        }
        let hex = s.split(';').next().unwrap_or("0").trim();
        let size = usize::from_str_radix(hex, 16).map_err(|_| format!("chunk 长度非法：{s:?}"))?;
        if size == 0 {
            break; // 末块
        }
        chunk.clear();
        (&mut br)
            .take(size as u64)
            .read_to_end(&mut chunk)
            .map_err(describe_read_err)?;
        let mut tail = String::new();
        let _ = br.read_line(&mut tail); // 吃掉 chunk 末尾的 CRLF
        carry.extend_from_slice(&chunk);
        while let Some(pos) = carry.iter().position(|b| *b == b'\n') {
            let mut line: Vec<u8> = carry.drain(..=pos).collect();
            line.pop(); // '\n'
            if line.last() == Some(&b'\r') {
                line.pop();
            }
            let text = String::from_utf8_lossy(&line).to_string();
            if !on_line(&text) {
                return Ok(());
            }
        }
    }
    if !carry.is_empty() {
        // 最后一行可能没有换行符收尾
        let mut line = carry;
        if line.last() == Some(&b'\r') {
            line.pop();
        }
        let text = String::from_utf8_lossy(&line).to_string();
        if !text.is_empty() {
            let _ = on_line(&text);
        }
    }
    Ok(())
}

/// 读超时要说人话：这里是"链路死了"，不是"服务慢"。
fn describe_read_err(e: std::io::Error) -> String {
    if matches!(
        e.kind(),
        std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
    ) {
        "45 秒没收到任何数据（心跳也没来），当成断链".to_string()
    } else {
        format!("读取事件流失败：{e}")
    }
}
