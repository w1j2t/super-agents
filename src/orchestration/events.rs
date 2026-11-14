//! 任务事件总线 —— SSE 进度的数据源，也是"可插拔观察者"的第一块拼图。
//!
//! # 为什么单独成模块，而不是塞进 executor
//!
//! 进度推送是**观察**行为，不该和治理链（鉴权 / 预算 / 重试 / 熔断 / 审计）纠缠在一起。
//! 执行器只负责"我改了这条任务"，由持久化的唯一出口统一广播；
//! 将来要把 SSE 换成 WebSocket 或消息队列（Redis / NATS），只替换这一层，
//! 执行器、handler、存储一行都不用动——这就是模块化想要的效果。
//!
//! 事件本身的形状定义在 [`crate::domain::event::TaskEvent`]（领域层），
//! 这里只负责"怎么分发"。
//!
//! # 为什么是"每订阅者一条 channel"而不是一条广播通道
//!
//! tokio 的 `broadcast` 在 1.54 上已不公开 `poll_recv`（只有 async `recv`），
//! 而 `Stream::poll_next` 里没法保存一个借用 `self` 的 future（自引用）。
//! 换成"发布方持有一张订阅者表、逐个 `try_send`"之后：
//! - `mpsc::Receiver` 有 `poll_recv`，`Stream` 实现干净；
//! - 每个订阅者独立缓冲，互不拖累；
//! - 不存在"一个卡死的连接让整个进程堆内存"的风险。
//!
//! # 不变量
//!
//! - 序号 `seq` 进程内单调递增，用来判断"前端有没有漏事件"（由领域层承载）；
//! - **发布永不被消费者阻塞**：某订阅者缓冲满时，该事件对它丢弃并计入 [`EventBus::dropped`]，
//!   发布方继续往下走。宁可让一个卡住的浏览器少一条中间态，也不能拖住任务执行；
//! - 订阅者断开后不需要显式退订：**发布**时会顺手摘掉，**读取订阅数**时也会摘掉。
//!   后者不是可有可无的洁癖——没有新任务在跑时就没有发布，断开的连接会一直挂在表里，
//!   `sa_sse_subscribers` 于是只涨不落，看板上会变成一条假的"连接泄漏"曲线。

use crate::domain::event::TaskEvent;
use crate::domain::task::TaskRecord;
use std::collections::VecDeque;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use tokio::sync::mpsc;

pub use crate::domain::event::TaskEvent as Event;

/// 每个订阅者的独立缓冲长度。单个任务的生命周期只产生 3 条事件
/// （受理 / 执行中 / 终态），256 条足够覆盖"刚连上时补齐最近状态"的场景。
const SUBSCRIBER_BUFFER: usize = 256;

/// 可回放历史的事件条数上限。
///
/// 为什么需要历史：SSE 断线重连（浏览器 `EventSource` 自带，或手机切网络）时，
/// 客户端会把最后收到的 `seq` 通过 `Last-Event-ID` 头带回来。没有历史，
/// 服务端只能回一句"我重新告诉你当前状态"——中间那几步 `running` 就永久丢了，
/// 前端进度条会莫名其妙跳一格。有了历史，可以精确补发"你漏的那几条"。
///
/// 为什么是 512：一条任务只产生 3 条事件（受理 / 执行中 / 终态），
/// 512 条足够覆盖上百个任务的近期尾巴；再大只是白占内存，
/// 补不齐时我们已经设计了诚实的降级路径（回推快照，并计数 `sa_sse_replay_gap_total`）。
const HISTORY_LIMIT: usize = 512;

/// 事件总线：一张订阅者表 + 一个单调序号。
pub struct EventBus {
    subscribers: Mutex<Vec<mpsc::Sender<TaskEvent>>>,
    seq: AtomicU64,
    /// 因缓冲满而被丢弃的事件累计数（用于判断"是不是真有慢消费者"）
    dropped: AtomicU64,
    /// 最近 [`HISTORY_LIMIT`] 条事件，供断线重连补发。
    ///
    /// 与"每个订阅者一条 channel"是两种不同性质的东西：
    /// channel 是**传送带**（读过即消失），history 是**环形台账**（读过还在）。
    /// 用同一把锁、同一个模块，是因为它们永远同步更新，分开只会多一个不一致点。
    history: Mutex<VecDeque<TaskEvent>>,
}

impl EventBus {
    pub fn new() -> Self {
        Self {
            subscribers: Mutex::new(Vec::new()),
            seq: AtomicU64::new(0),
            dropped: AtomicU64::new(0),
            history: Mutex::new(VecDeque::with_capacity(HISTORY_LIMIT)),
        }
    }

    /// 广播一条任务状态。返回**实际送达**的订阅者数量（0 表示当前无人观看或缓冲已满，不是错误）。
    pub fn publish(&self, task: &TaskRecord) -> usize {
        let seq = self.seq.fetch_add(1, Ordering::Relaxed) + 1;
        let event = TaskEvent::build(task, seq);
        let mut subs = match self.subscribers.lock() {
            Ok(guard) => guard,
            // 锁中毒：持有者 panic 过。数据只是一张 sender 表，不存在半更新，
            // 直接取回内部值继续用，比让整个发布路径崩掉更合理。
            Err(poisoned) => poisoned.into_inner(),
        };
        let mut delivered = 0usize;
        let mut dropped = 0u64;
        subs.retain(|tx| match tx.try_send(event.clone()) {
            Ok(()) => {
                delivered += 1;
                true
            }
            Err(mpsc::error::TrySendError::Full(_)) => {
                // 订阅者跟不上：丢这一条，但保留订阅关系（它可能只是卡了一下）
                dropped += 1;
                true
            }
            // 订阅者已经断了（浏览器关页面 / 客户端超时）：顺手摘掉
            Err(mpsc::error::TrySendError::Closed(_)) => false,
        });
        if dropped > 0 {
            self.dropped.fetch_add(dropped, Ordering::Relaxed);
        }

        // 入历史台账：断线重连靠它补齐中间态。这里多一次 clone（一条事件几十字节），
        // 换来"重连不丢帧"，比让前端自己轮询便宜得多。
        // 溢出从**最旧**的一端丢：越老的越可能已经没人关心。
        // 注意顺序：先发布再入账，两步都在同一把 subscribers 锁释放之后，
        // 避免"持有订阅表锁的同时去抢历史锁"这种不必要的嵌套。
        let mut hist = match self.history.lock() {
            Ok(guard) => guard,
            Err(poisoned) => poisoned.into_inner(),
        };
        if hist.len() >= HISTORY_LIMIT {
            hist.pop_front();
        }
        hist.push_back(event);
        drop(hist);

        delivered
    }

    /// 断线重连补发：返回该任务在 `after_seq` 之后的事件（按 seq 升序）。
    ///
    /// 返回 `None` 表示**补不齐**——`after_seq` 已经滚出历史窗口（进程重启，
    /// 或客户端离线太久）。这必须是一个可区分的结果而不是空 `Vec`：
    /// 空 `Vec` 的含义是"你已经是全量了，等新事件即可"，两者对客户端的意义完全相反。
    ///
    /// 判定缺口的依据是 `front().seq > after_seq + 1`：
    /// 全局 `seq` 连续单调，若历史里最老的一条都比"下一条"还大，
    /// 说明 `after_seq` 与历史之间有洞。这里宁可误报缺口（退化成回推快照，多传一帧）
    /// 也不漏报——漏报的后果是前端少一格进度，而且无从察觉。
    pub fn replay(&self, task_id: &str, after_seq: u64) -> Option<Vec<TaskEvent>> {
        let hist = match self.history.lock() {
            Ok(guard) => guard,
            Err(poisoned) => poisoned.into_inner(),
        };
        // 空历史 + 客户端声称看过某个 seq：只可能来自上一个进程（或伪造），无法补
        if hist.is_empty() {
            return if after_seq == 0 { Some(Vec::new()) } else { None };
        }
        if let Some(front) = hist.front() {
            if front.seq > after_seq + 1 {
                return None;
            }
        }
        Some(
            hist.iter()
                .filter(|e| e.task_id == task_id && e.seq > after_seq)
                .cloned()
                .collect(),
        )
    }

    /// 历史台账中现存的事件条数（运维核对"回放窗口有多大"用）。
    pub fn history_len(&self) -> usize {
        match self.history.lock() {
            Ok(guard) => guard.len(),
            Err(poisoned) => poisoned.into_inner().len(),
        }
    }

    /// 新建一个订阅。调用方拿到的是独立缓冲，与其它订阅者互不影响。
    pub fn subscribe(&self) -> mpsc::Receiver<TaskEvent> {
        let (tx, rx) = mpsc::channel(SUBSCRIBER_BUFFER);
        let mut subs = match self.subscribers.lock() {
            Ok(guard) => guard,
            Err(poisoned) => poisoned.into_inner(),
        };
        subs.push(tx);
        rx
    }

    /// 当前在线订阅者数量，用于判断"推流是否真的被使用"。
    ///
    /// 读的时候顺手把已断开（接收端 drop、`try_send` 会返回 `Closed`）的通道摘掉。
    /// 为什么不能只依赖发布路径清理：空闲时段没有任务、也就没有发布，
    /// 断开的连接会一直留在表里，指标变成一条只涨不落的假泄漏曲线。
    /// 这里做一次 `is_closed()` 判定成本极低（订阅者以个位数计），换来干净的数字。
    pub fn subscribers(&self) -> usize {
        let mut subs = match self.subscribers.lock() {
            Ok(guard) => guard,
            Err(poisoned) => poisoned.into_inner(),
        };
        subs.retain(|tx| !tx.is_closed());
        subs.len()
    }

    /// 累计被丢弃的事件数（慢消费者）。
    pub fn dropped(&self) -> u64 {
        self.dropped.load(Ordering::Relaxed)
    }
}

impl Default for EventBus {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::task::{ModelProfile, TaskStatus, TaskType};
    use chrono::Utc;

    fn task(id: &str, status: TaskStatus) -> TaskRecord {
        let now = Utc::now();
        TaskRecord {
            id: id.into(),
            owner_id: "u1".into(),
            goal: "验证事件总线".into(),
            task_type: TaskType::Coding,
            requested_model: Some(ModelProfile::Standard),
            status,
            plan: Vec::new(),
            results: Vec::new(),
            trace: Vec::new(),
            result_summary: "x".repeat(500), // 超长摘要必须被截断
            tokens_used: 0,
            budget_tokens: 1000,
            created_at: now,
            updated_at: now,
            finished_at: None,
            memory_refs: Vec::new(),
            agreement_score: 0.9,
        }
    }

    #[test]
    fn publish_without_subscriber_is_not_an_error() {
        let bus = EventBus::new();
        assert_eq!(bus.publish(&task("t1", TaskStatus::Queued)), 0);
        assert_eq!(bus.subscribers(), 0);
        assert_eq!(bus.dropped(), 0);
    }

    #[tokio::test]
    async fn every_subscriber_receives_events_in_order() {
        let bus = EventBus::new();
        let mut a = bus.subscribe();
        let mut b = bus.subscribe();
        assert_eq!(bus.subscribers(), 2);

        assert_eq!(bus.publish(&task("t1", TaskStatus::Queued)), 2);
        assert_eq!(bus.publish(&task("t1", TaskStatus::Succeeded)), 2);

        for rx in [&mut a, &mut b] {
            let first = rx.recv().await.expect("第一条事件");
            let second = rx.recv().await.expect("第二条事件");
            assert_eq!((first.seq, first.status), (1, TaskStatus::Queued));
            assert_eq!((second.seq, second.status), (2, TaskStatus::Succeeded));
            // 摘要必须被截断：事件不该比任务详情还大
            assert_eq!(
                second.summary.chars().count(),
                crate::domain::event::SUMMARY_LIMIT
            );
        }
    }

    #[tokio::test]
    async fn publisher_never_blocks_on_a_slow_subscriber() {
        let bus = EventBus::new();
        let mut slow = bus.subscribe();
        // 塞满缓冲（SUBSCRIBER_BUFFER 条）后再发 10 条：发布不能卡，多出来的被丢弃并计数
        for i in 0..SUBSCRIBER_BUFFER {
            assert_eq!(bus.publish(&task(&format!("t{i}"), TaskStatus::Running)), 1);
        }
        for i in 0..10 {
            // 缓冲已满：这一条对慢订阅者丢弃，函数返回"送达 0 人"，但**不阻塞、不报错**
            assert_eq!(bus.publish(&task(&format!("x{i}"), TaskStatus::Running)), 0);
        }
        assert_eq!(bus.dropped(), 10);
        // 订阅者仍能继续读到自己缓冲里的旧事件（不是被踢掉）
        assert!(slow.recv().await.is_some());
        assert_eq!(bus.subscribers(), 1);
    }

    #[tokio::test]
    async fn closed_subscriber_is_reaped_on_read_and_on_publish() {
        let bus = EventBus::new();
        let rx = bus.subscribe();
        drop(rx); // 模拟浏览器关页面
        // 读取时就该摘掉：空闲时段没有任何发布，不能指望"下一条事件"来清理，
        // 否则在线订阅数只涨不落，看板上会出现一条假的连接泄漏曲线。
        assert_eq!(bus.subscribers(), 0, "断开的订阅者不应计入在线数");

        // 发布路径同样要能清理（还没人读过计数的场景）
        let rx2 = bus.subscribe();
        drop(rx2);
        assert_eq!(bus.publish(&task("t1", TaskStatus::Queued)), 0);
        assert_eq!(bus.subscribers(), 0, "发布路径也必须摘掉断开的订阅者");
    }

    #[tokio::test]
    async fn sequence_numbers_are_monotonic_across_tasks() {
        let bus = EventBus::new();
        let mut rx = bus.subscribe();
        bus.publish(&task("a", TaskStatus::Queued));
        bus.publish(&task("b", TaskStatus::Queued));
        bus.publish(&task("a", TaskStatus::Succeeded));
        let seqs: Vec<u64> = [rx.recv().await, rx.recv().await, rx.recv().await]
            .into_iter()
            .map(|e| e.expect("事件").seq)
            .collect();
        assert_eq!(seqs, vec![1, 2, 3]);
    }

    #[test]
    fn counting_subscribers_prunes_connections_that_already_closed() {
        // 场景：开着看板连了 SSE，用户关页面（接收端 drop），此时没有新任务在跑，
        // 也就没有 publish 可以顺手清理。若读取时不清理，指标会把这条死连接一直算着。
        let bus = EventBus::new();
        assert_eq!(bus.subscribers(), 0);

        let rx1 = bus.subscribe();
        let rx2 = bus.subscribe();
        assert_eq!(bus.subscribers(), 2, "两条活连接都应被计入");

        drop(rx1);
        assert_eq!(
            bus.subscribers(),
            1,
            "断开的那条应在读取时被摘掉，而不是等下一条事件发布"
        );

        drop(rx2);
        assert_eq!(bus.subscribers(), 0);
    }

    // ---- 断线续传（Last-Event-ID 回放）----

    #[test]
    fn replay_on_fresh_bus_reports_nothing_to_send() {
        // 首次连接（客户端还没收到过任何带 id 的帧）：补发为空是"正确且完整"的语义，
        // 必须与下面的 None（补不齐）区分开——两者对调用方的动作完全不同。
        let bus = EventBus::new();
        assert!(matches!(bus.replay("t1", 0), Some(v) if v.is_empty()));
        assert_eq!(bus.history_len(), 0);
    }

    #[test]
    fn replay_returns_only_missed_frames_of_that_task() {
        let bus = EventBus::new();
        bus.publish(&task("a", TaskStatus::Queued)); // seq 1
        bus.publish(&task("b", TaskStatus::Queued)); // seq 2（别的任务）
        bus.publish(&task("a", TaskStatus::Running)); // seq 3
        bus.publish(&task("a", TaskStatus::Succeeded)); // seq 4

        // 客户端说"我收到 seq=1 了"：只该补 a 的 3、4，别把 b 的 2 也塞进来
        let missed = bus.replay("a", 1).expect("不应判定为缺口");
        assert_eq!(
            missed.iter().map(|e| e.seq).collect::<Vec<_>>(),
            vec![3, 4]
        );
        assert!(missed.iter().all(|e| e.task_id == "a"));

        // 客户端已经拿到终态：补发为空，但仍是"完整"（Some）
        assert!(matches!(bus.replay("a", 4), Some(v) if v.is_empty()));
    }

    #[test]
    fn replay_reports_gap_when_anchor_fell_out_of_window() {
        // 客户端离线很久，它记住的锚点已经被环形缓冲挤掉。
        // 此时必须返回 None（让上层退化成"重推当前快照"），
        // 而不是返回一段"看着有内容、中间其实是断的"事件。
        let bus = EventBus::new();
        for i in 0..(HISTORY_LIMIT + 20) {
            bus.publish(&task("t1", TaskStatus::Running));
            let _ = i;
        }
        assert_eq!(bus.history_len(), HISTORY_LIMIT, "历史台账必须有硬上限");

        // 最老的锚点（seq=1）已经不在窗口里
        assert!(bus.replay("t1", 1).is_none());
        // 窗口内相邻的锚点仍可精确续传：此时最老一条是 seq = len-本，取倒数第二条试
        let tail = (HISTORY_LIMIT + 20) as u64;
        assert!(bus.replay("t1", tail - 1).is_some());
    }

    #[test]
    fn replay_after_restart_is_a_gap_not_an_empty_success() {
        // 进程重启：新的 EventBus 历史为空，但客户端手里还握着上个进程的 seq。
        // 若这里返回 Some(Vec::new())，客户端会以为"我没有漏任何东西"，
        // 实际上它跟本进程完全脱节 —— 必须判为缺口。
        let bus = EventBus::new();
        assert!(bus.replay("t1", 7).is_none());
    }
}
