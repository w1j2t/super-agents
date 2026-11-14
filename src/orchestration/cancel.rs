//! 任务取消登记簿：让 `TaskStatus::Cancelled` 成为**真能到达**的状态。
//!
//! # 为什么需要这个模块
//!
//! 执行引擎原本是"一步跑到底"的同步循环，用户没有任何手段叫停：一个接了真实执行
//! 后端（`SA_RUNNER=process`）的任务，每步可能跑十几秒，用户只能干等，前端还会
//! 因为 HTTP 超时先断开。状态枚举里那个 `cancelled` 于是成了死状态——
//! 声明了却永远到不了，等于说谎。
//!
//! 这里用最小的机制把它做实：**每任务一面只写的旗子**。
//! - 提交方（HTTP handler）登记旗子；
//! - 执行方（`Executor`）在**每个步骤边界**、以及**每次重试退避之前**读一次；
//! - 取消方（`POST /tasks/{id}/cancel`）把旗子立起来。
//!
//! 选择"旗子"而不是"直接杀线程"：执行中的单步（模型调用 / 脚本）无法安全打断，
//! 强行终止会留下半写状态。协作式取消只保证"不再开始下一步"，语义诚实、可审计。
//!
//! # 与热插拔的关系
//!
//! 这个登记簿是**进程内**状态，不进 `ports` 注册表：它不是可替换部件，而是
//! "异步执行"这一模式自带的配套设施。换任何执行后端都不影响它。

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

/// 一个任务的取消旗子。`true` = 已请求取消。
pub type CancelFlag = Arc<AtomicBool>;

/// 任务 id → 旗子。任务结束即摘除，避免长期驻留。
#[derive(Default)]
pub struct Cancellations {
    inner: Mutex<HashMap<String, CancelFlag>>,
}

impl Cancellations {
    pub fn new() -> Self {
        Self::default()
    }

    /// 登记一个即将执行的任务，返回它的旗子。重复登记会覆盖旧旗子（旧任务已结束）。
    pub fn register(&self, task_id: &str) -> CancelFlag {
        let flag: CancelFlag = Arc::new(AtomicBool::new(false));
        self.lock().insert(task_id.to_string(), flag.clone());
        flag
    }

    /// 取运行中任务的旗子；没有则说明它不在此进程内执行（已结束 / 重启前遗留）。
    pub fn handle(&self, task_id: &str) -> Option<CancelFlag> {
        self.lock().get(task_id).cloned()
    }

    /// 请求取消。
    ///
    /// 返回 `true` 表示"确实有一个在跑的任务被立了旗子"；
    /// 返回 `false` 表示找不到对应执行体，调用方应据此区分"已请求取消"与
    /// "根本没有东西在跑"——不要对后者谎称取消成功。
    pub fn request(&self, task_id: &str) -> bool {
        match self.lock().get(task_id) {
            Some(flag) => {
                flag.store(true, Ordering::SeqCst);
                true
            }
            None => false,
        }
    }

    /// 任务结束（无论成功失败）后摘除旗子，防止 map 无限增长。
    pub fn finish(&self, task_id: &str) {
        self.lock().remove(task_id);
    }

    /// 当前在跑 / 排队中的任务数（供 overview 展示，让前端知道后台有没有活）。
    pub fn active(&self) -> usize {
        self.lock().len()
    }

    /// 锁中毒时取回内部数据继续用：登记簿里只有旗子，不存在"写坏一半"的语义，
    /// 因为一次 panic 就让全站取消功能瘫痪，代价远大于收益。
    fn lock(&self) -> std::sync::MutexGuard<'_, HashMap<String, CancelFlag>> {
        self.inner.lock().unwrap_or_else(|e| e.into_inner())
    }
}

/// 判断一个旗子是否已立起（`None` 视为"不可取消"，永远返回 false）。
pub fn is_cancelled(flag: Option<&AtomicBool>) -> bool {
    flag.map(|f| f.load(Ordering::SeqCst)).unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn register_request_finish_roundtrip() {
        let reg = Cancellations::new();
        let flag = reg.register("t1");
        assert!(!is_cancelled(Some(flag.as_ref())), "刚登记必须是未取消");
        assert!(reg.request("t1"), "在跑的任务必须能立旗子");
        assert!(is_cancelled(Some(flag.as_ref())), "旗子必须真的立起来");
        assert_eq!(reg.active(), 1);
        reg.finish("t1");
        assert_eq!(reg.active(), 0, "结束后必须摘除，避免长期驻留");
        assert!(!reg.request("t1"), "已结束的任务不得谎称取消成功");
    }

    #[test]
    fn request_on_unknown_task_is_false_not_panic() {
        let reg = Cancellations::new();
        assert!(!reg.request("never-existed"));
        assert!(reg.handle("never-existed").is_none());
        assert!(!is_cancelled(None), "没有旗子即不可取消");
    }

    #[test]
    fn register_twice_returns_fresh_unflagged_handle() {
        let reg = Cancellations::new();
        let first = reg.register("dup");
        reg.request("dup");
        let second = reg.register("dup");
        assert!(is_cancelled(Some(first.as_ref())));
        assert!(!is_cancelled(Some(second.as_ref())), "新任务不能继承旧的取消标记");
        assert_eq!(reg.active(), 1, "同 id 只保留一个旗子");
    }

    #[test]
    fn concurrent_requests_are_all_observed() {
        let reg = Arc::new(Cancellations::new());
        let flag = reg.register("race");
        let mut handles = Vec::new();
        for _ in 0..8 {
            let r = Arc::clone(&reg);
            handles.push(std::thread::spawn(move || r.request("race")));
        }
        assert!(handles.into_iter().all(|h| h.join().unwrap()));
        assert!(is_cancelled(Some(flag.as_ref())));
    }
}
