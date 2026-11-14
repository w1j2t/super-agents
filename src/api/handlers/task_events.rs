//! 任务进度实时流（Server-Sent Events）。
//!
//! # 为什么用 SSE 而不是 WebSocket
//!
//! 这里的需求是**单向**的：服务端推任务状态，客户端只读。SSE 走普通 HTTP/1.1、
//! 能穿代理、浏览器原生 `EventSource` 自带断线重连，比 WebSocket 少一整套握手与心跳协议。
//! 真正的双向交互（例如"边跑边改指令"）再上 WebSocket 也不迟——那时只需替换本模块，
//! 因为状态变化的出口在 `persist()`，与传输层无关。
//!
//! # 语义约定（前端按这个写就不会错）
//!
//! - 事件名固定为 `status`，`data` 是一条 `TaskEvent` JSON；
//! - **首帧**是连接瞬间的任务快照，`seq = 0`，表示"不是广播事件，而是当前状态"；
//! - 任务进入终态后服务端主动结束流（HTTP EOF），客户端不必自己判断何时关闭；
//! - 服务端每 15s 发一条心跳（SSE 注释行），避免中间设备把闲置连接掐掉；
//! - 前端应据 `seq` 是否连续判断要不要回查 `GET /tasks/{id}`：
//!   慢消费者会丢中间态（服务端会计数），但**终态一定在缓冲里**，因为一条任务只产生 3 条事件。
//! - 每帧都带 `id: <seq>`，因此浏览器 `EventSource` 断线重连会自动带上 `Last-Event-ID`；
//!   服务端据此**只补发漏掉的帧**（不重推快照）。若客户端离线太久、锚点已滚出回放窗口
//!   （`EventBus::replay` 返回 `None`），则退化为推一次当前快照，并计入
//!   `sa_sse_replay_gap_total` —— 宁可明说"跟丢了"，也不给一份缺格的假进度。
//!
//! # 权限
//!
//! 与 `GET /tasks/{id}` 完全同源：复用 `task::load_owned`，
//! 不存在"详情要鉴权、长连接不要鉴权"的缝隙。

use crate::api::extract::AuthUser;
use crate::api::handlers::task::load_owned;
use crate::api::jsons::ApiPath;
use crate::error::ApiError;
use crate::domain::event::TaskEvent;
use crate::state::SharedState;
use axum::extract::State;
use axum::http::{HeaderMap, HeaderValue};
use axum::response::sse::{Event, KeepAlive, Sse};
use futures_core::Stream;
use std::collections::VecDeque;
use std::convert::Infallible;
use std::pin::Pin;
use std::task::{Context, Poll};
use std::time::Duration;
use tokio::sync::mpsc;

/// 心跳间隔：15s 是常见 NAT / 代理空闲超时（通常 60s 起）的安全值。
const HEARTBEAT: Duration = Duration::from_secs(15);

/// 把一条事件转成 SSE 帧。序列化失败理论上不可能（字段全是可序列化类型），
/// 但仍要给出一条可诊断的帧而不是 panic——一条坏帧不该拖垮整条连接。
///
/// # 为什么每帧都带 `id`
///
/// `id:` 是 SSE 的断点续传锚点：浏览器 `EventSource` 会自动记住最后一个 `id`，
/// 断线重连时通过 `Last-Event-ID` 请求头带回来。**不发 `id` 就等于放弃续传能力**——
/// 客户端不知道自己在哪，服务端也无从补齐，只能整段重来。
fn to_sse(event: &TaskEvent) -> Event {
    match Event::default()
        .id(event.seq.to_string())
        .event("status")
        .json_data(event)
    {
        Ok(frame) => frame,
        Err(err) => Event::default()
            .event("error")
            .data(format!("事件序列化失败：{err}")),
    }
}

/// 解析 `Last-Event-ID`。非法值一律视为"没有"（首次连接），
/// 而不是报 400：一个坏头不该让用户看不到自己的任务进度。
fn parse_last_event_id(raw: Option<&HeaderValue>) -> u64 {
    raw.and_then(|v| v.to_str().ok())
        .and_then(|s| s.trim().parse::<u64>().ok())
        .unwrap_or(0)
}

/// 单条任务的事件流。
///
/// 直接实现 `Stream`（而不引入 tokio-stream / async-stream）：逻辑就是一个循环——
/// 先吐连接时的快照帧，再从自己的 channel 里取事件，终态吐完即 EOF。
pub(crate) struct TaskEventStream {
    rx: mpsc::Receiver<TaskEvent>,
    task_id: String,
    /// 连接时补发的当前快照（若有）
    pending: VecDeque<TaskEvent>,
    /// 已把终态推给客户端：下一次 poll 直接结束，让前端拿到 EOF
    done: bool,
}

impl Stream for TaskEventStream {
    type Item = Result<Event, Infallible>;

    fn poll_next(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        // 所有字段都是 Unpin，可以直接取可变引用
        let this = self.get_mut();
        if this.done {
            return Poll::Ready(None);
        }
        loop {
            if let Some(event) = this.pending.pop_front() {
                if event.status.is_terminal() {
                    this.done = true;
                }
                return Poll::Ready(Some(Ok(to_sse(&event))));
            }
            match this.rx.poll_recv(cx) {
                Poll::Ready(Some(event)) => {
                    // 每个订阅者会收到全站事件，只留自己的。
                    // 单帧很小，丢弃成本远低于"为每个任务各开一条总线"的复杂度。
                    if event.task_id != this.task_id {
                        continue;
                    }
                    if event.status.is_terminal() {
                        this.done = true;
                    }
                    return Poll::Ready(Some(Ok(to_sse(&event))));
                }
                Poll::Ready(None) => return Poll::Ready(None),
                Poll::Pending => return Poll::Pending,
            }
        }
    }
}

/// `GET /api/v1/tasks/{id}/events` — 订阅单个任务的状态流。
///
/// 带 `Last-Event-ID` 时走**续传**：只补发该 seq 之后的事件，不再推快照。
/// 补不齐（滚出历史窗口 / 进程重启）则退化为推快照，并计入 `sa_sse_replay_gap_total`，
/// 让"跟丢了"这件事在指标上可见，而不是悄悄给前端一份看着正常、实际缺格的进度。
pub async fn stream(
    State(state): State<SharedState>,
    user: AuthUser,
    ApiPath(id): ApiPath<String>,
    headers: HeaderMap,
) -> Result<Sse<impl Stream<Item = Result<Event, Infallible>>>, ApiError> {
    // 先鉴权再订阅：绝不"先建长连接再校验"，否则 401 之前资源已经被占用。
    let task = load_owned(&state, &user, &id)?;

    let last_seq = parse_last_event_id(headers.get("last-event-id"));
    let mut pending = VecDeque::new();
    let mut done = false;

    match if last_seq == 0 {
        None
    } else {
        state.events.replay(&id, last_seq)
    } {
        // 续传成功：补发漏掉的帧。若补发为空且任务已终态，说明客户端本已看到结局，
        // 直接 EOF——没必要让一条"永远不会有下一条"的连接白挂着占一个订阅位。
        Some(events) => {
            state.metrics.inc("sa_sse_replay_total");
            if events.is_empty() && task.status.is_terminal() {
                done = true;
            }
            pending.extend(events);
        }
        // 首次连接 + 续传失败（`None`）都走快照：客户端拿到的是一份**当前状态**，
        // 而不是"你以为的上一帧"，语义上永远安全。
        None => {
            if last_seq != 0 {
                state.metrics.inc("sa_sse_replay_gap_total");
            }
            pending.push_back(TaskEvent::snapshot(&task));
        }
    }

    state.metrics.inc("sa_sse_streams_opened_total");
    let stream = TaskEventStream {
        rx: state.events.subscribe(),
        task_id: id,
        pending,
        done,
    };
    Ok(Sse::new(stream).keep_alive(KeepAlive::new().interval(HEARTBEAT).text("ping")))
}
