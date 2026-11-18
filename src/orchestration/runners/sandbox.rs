//! 进程级资源沙箱（Windows Job Object）。
//!
//! # 为什么需要这一层
//!
//! 三方审查 H1 第 2 条：原实现的隔离只有"画像约束 + 路径白名单 + 单步硬超时"。
//! 这些都是**事后**手段——脚本开了大分配、起了几十个进程，超时才发现，
//! 而那时机器已经在换页了。作业对象（Job Object）是内核级的**事前**约束：
//!
//! - `JOB_OBJECT_LIMIT_PROCESS_MEMORY` / `JOB_OBJECT_LIMIT_JOB_MEMORY`：
//!   内存超限时分配直接失败，脚本自己收到异常，整机不受牵连；
//! - `JOB_OBJECT_LIMIT_ACTIVE_PROCESS`：限制作业内并发进程数，挡住 fork 炸弹；
//! - `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`：本进程崩溃/退出时连带杀掉整个进程树，
//!   不留孤儿——这是 process runner 头部第 4 条"不留僵尸"在进程树上的延伸。
//!
//! # 为什么手写 FFI 而不引 windows-sys
//!
//! 本仓库走 `x86_64-pc-windows-gnu`，这里只需要四个 Kernel32 符号。
//! 引一个整 crate 会连带动 Cargo.lock、镜像同步与锁定依赖，而这段代码的
//! 失效模式恰恰是"限制其实没生效却看起来生效了"——所以它必须能被一眼读懂、
//! 能被单测按住。四个符号的代价远小于一个新依赖。
//!
//! # 仍没有做到的部分（如实记录，不含糊）
//!
//! 没有降权账户（仍以当前用户身份跑）、没有网络隔离、没有文件系统隔离。
//! 补这两项需要另一个量级的改动（CreateProcessAsUser / AppContainer + WFP 规则），
//! 本条修法只承诺"资源层面炸不了整机"，不承诺"恶意脚本偷不到数据"。

/// 沙箱上限。全部可由环境变量覆盖，便于按机器与脚本画像调整。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct JobLimits {
    /// 单进程提交内存上限（MB）。0 = 不限。
    pub mem_mb: u64,
    /// 作业内允许的并发进程数。0 = 不限。
    pub max_procs: u32,
    /// 单进程用户态 CPU 时间上限（秒）。0 = 不限。
    pub cpu_secs: u64,
}

impl Default for JobLimits {
    fn default() -> Self {
        // 缺省值取"够跑普通脚本、又拦得住失控分配"的量级：
        // 2GB 覆盖 2B 级本地模型的峰值，同时远小于整机内存——脚本写错
        // `bytearray(10**10)` 时是被作业拒绝，而不是让整台机器开始换页。
        Self {
            mem_mb: 2048,
            max_procs: 8,
            cpu_secs: 0,
        }
    }
}

impl JobLimits {
    pub fn from_env() -> Self {
        let d = Self::default();
        Self {
            mem_mb: read_u64("SA_RUNNER_MEM_MB").unwrap_or(d.mem_mb),
            max_procs: read_u64("SA_RUNNER_MAX_PROCS")
                .map(|v| v.min(u32::MAX as u64) as u32)
                .unwrap_or(d.max_procs),
            cpu_secs: read_u64("SA_RUNNER_CPU_SECS").unwrap_or(d.cpu_secs),
        }
    }

    /// 是否被显式关掉。`SA_RUNNER_SANDBOX=off` 与 `SA_RUNNER_ALLOW_SHELL=1` 同一种纪律：
    /// 默认收紧，放开必须是一个**有意识、留痕**的动作，而不是"跑不起来就默默降级"。
    pub fn disabled() -> bool {
        match std::env::var("SA_RUNNER_SANDBOX") {
            Ok(v) => matches!(
                v.trim().to_ascii_lowercase().as_str(),
                "off" | "0" | "false" | "none"
            ),
            Err(_) => false,
        }
    }
}

fn read_u64(key: &str) -> Option<u64> {
    std::env::var(key).ok()?.trim().parse::<u64>().ok()
}

#[cfg(windows)]
pub use win::Job;

#[cfg(windows)]
mod win {
    use super::JobLimits;
    use std::ffi::c_void;
    use std::os::windows::io::AsRawHandle;
    use std::process::Child;

    type Handle = *mut c_void;

    // Kernel32：作业对象的三个动作 + 句柄回收。签名与 winnt.h 逐字对应。
    #[link(name = "kernel32")]
    extern "system" {
        fn CreateJobObjectW(attrs: *mut c_void, name: *const u16) -> Handle;
        fn SetInformationJobObject(job: Handle, class: i32, info: *mut c_void, len: u32) -> i32;
        fn AssignProcessToJobObject(job: Handle, process: Handle) -> i32;
        fn CloseHandle(h: Handle) -> i32;
    }

    const JOB_OBJECT_LIMIT_PROCESS_TIME: u32 = 0x0000_0002;
    const JOB_OBJECT_LIMIT_ACTIVE_PROCESS: u32 = 0x0000_0008;
    const JOB_OBJECT_LIMIT_PROCESS_MEMORY: u32 = 0x0000_0100;
    const JOB_OBJECT_LIMIT_JOB_MEMORY: u32 = 0x0000_0200;
    const JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: u32 = 0x0000_2000;
    const JOB_OBJECT_EXTENDED_LIMIT_INFORMATION: i32 = 9;

    #[repr(C)]
    #[derive(Default)]
    struct IoCounters {
        read_operation_count: u64,
        write_operation_count: u64,
        other_operation_count: u64,
        read_transfer_count: u64,
        write_transfer_count: u64,
        other_transfer_count: u64,
    }

    /// 布局必须与 `JOBOBJECT_BASIC_LIMIT_INFORMATION` 一模一样：
    /// 字段顺序或宽度错一个，`SetInformationJobObject` 会静默按"另一个结构"解读，
    /// 表现为"设了限制但完全不生效"——最难查的一类失效。
    #[repr(C)]
    #[derive(Default)]
    struct BasicLimitInformation {
        per_process_user_time_limit: i64,
        per_job_user_time_limit: i64,
        limit_flags: u32,
        minimum_working_set_size: usize,
        maximum_working_set_size: usize,
        active_process_limit: u32,
        affinity: usize,
        priority_class: u32,
        scheduling_class: u32,
    }

    #[repr(C)]
    #[derive(Default)]
    struct ExtendedLimitInformation {
        basic: BasicLimitInformation,
        io: IoCounters,
        process_memory_limit: usize,
        job_memory_limit: usize,
        peak_process_memory_used: usize,
        peak_job_memory_used: usize,
    }

    /// 一个已设好上限的作业对象。`Drop` 时关闭句柄——因为带了
    /// `KILL_ON_JOB_CLOSE`，这一步同时也是"把还在跑的进程树一起收掉"。
    pub struct Job {
        handle: Handle,
        limits: JobLimits,
    }

    impl Job {
        /// 建作业并设上限。返回 `None` 表示沙箱不可用（含"限制设不上"），
        /// 调用方据此如实记录"这一步没有沙箱保护"，而不是假装受控。
        pub fn attach(limits: JobLimits) -> Option<Job> {
            if JobLimits::disabled() {
                return None;
            }
            unsafe {
                let handle = CreateJobObjectW(std::ptr::null_mut(), std::ptr::null());
                if handle.is_null() {
                    return None;
                }
                let mut info: ExtendedLimitInformation = std::mem::zeroed();
                // KILL_ON_JOB_CLOSE 无条件设：后端崩溃时不留孤儿脚本。
                let mut flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                if limits.mem_mb > 0 {
                    flags |= JOB_OBJECT_LIMIT_PROCESS_MEMORY | JOB_OBJECT_LIMIT_JOB_MEMORY;
                    let bytes = (limits.mem_mb as usize).saturating_mul(1024 * 1024);
                    info.process_memory_limit = bytes;
                    info.job_memory_limit = bytes;
                }
                if limits.max_procs > 0 {
                    flags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS;
                    info.basic.active_process_limit = limits.max_procs;
                }
                if limits.cpu_secs > 0 {
                    flags |= JOB_OBJECT_LIMIT_PROCESS_TIME;
                    // FILETIME 单位：100 纳秒。
                    info.basic.per_process_user_time_limit = (limits.cpu_secs as i64) * 10_000_000;
                }
                info.basic.limit_flags = flags;
                let ok = SetInformationJobObject(
                    handle,
                    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                    &mut info as *mut _ as *mut c_void,
                    std::mem::size_of::<ExtendedLimitInformation>() as u32,
                );
                if ok == 0 {
                    // 限设置不上就等于没沙箱。留着一个空作业句柄只会让上层
                    // 误以为受控——关掉、返回 None，让"未受控"这件事显式存在。
                    CloseHandle(handle);
                    return None;
                }
                Some(Job { handle, limits })
            }
        }

        /// 把已 spawn 的子进程挂进作业。必须紧贴 spawn，中间不要插别的逻辑。
        pub fn assign(&self, child: &Child) -> bool {
            unsafe { AssignProcessToJobObject(self.handle, child.as_raw_handle() as Handle) != 0 }
        }

        pub fn limits(&self) -> JobLimits {
            self.limits
        }
    }

    impl Drop for Job {
        fn drop(&mut self) {
            unsafe {
                CloseHandle(self.handle);
            }
        }
    }

    // 句柄本身不携带 Rust 侧别名信息，跨线程移动/共享由内核保证安全。
    unsafe impl Send for Job {}
    unsafe impl Sync for Job {}
}

#[cfg(not(windows))]
pub use other::Job;

#[cfg(not(windows))]
mod other {
    //! 非 Windows 的空实现：接口同名，方便调用点只写一条路径。
    //! Linux 上真正的隔离应接 cgroup v2（memory.max / pids.max），
    //! 这里不假装做到了——`attach` 返回 None，调用方会记录"无沙箱"。
    use super::JobLimits;
    use std::process::Child;

    pub struct Job;

    impl Job {
        pub fn attach(_limits: JobLimits) -> Option<Job> {
            None
        }
        pub fn assign(&self, _child: &Child) -> bool {
            false
        }
        pub fn limits(&self) -> JobLimits {
            JobLimits::default()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn limits_default_is_bounded_but_not_starving() {
        let d = JobLimits::default();
        // 下限：必须真的拦住"一次分配吃光机器"，所以不能是 0（0 等于不限）。
        assert!(d.mem_mb >= 512, "缺省内存上限太小会误杀正常脚本：{}", d.mem_mb);
        // 上限：必须明显小于常见整机内存，否则拦不住失控分配。
        assert!(d.mem_mb <= 8192, "缺省内存上限太大等于没设：{}", d.mem_mb);
        assert!(d.max_procs >= 1 && d.max_procs <= 64);
    }

    #[test]
    fn env_overrides_are_honoured() {
        // 环境变量是全局的，这里用一个互不干扰的键集合，并在结束时还原，
        // 避免与并行跑的其他测试互相污染。
        let keys = ["SA_RUNNER_MEM_MB", "SA_RUNNER_MAX_PROCS", "SA_RUNNER_CPU_SECS"];
        let saved: Vec<Option<String>> = keys.iter().map(|k| std::env::var(k).ok()).collect();
        std::env::set_var("SA_RUNNER_MEM_MB", "777");
        std::env::set_var("SA_RUNNER_MAX_PROCS", "3");
        std::env::set_var("SA_RUNNER_CPU_SECS", "9");
        let l = JobLimits::from_env();
        assert_eq!((l.mem_mb, l.max_procs, l.cpu_secs), (777, 3, 9));
        for (k, v) in keys.iter().zip(saved) {
            match v {
                Some(v) => std::env::set_var(k, v),
                None => std::env::remove_var(k),
            }
        }
    }

    #[cfg(windows)]
    #[test]
    fn job_is_created_and_a_real_child_is_assigned() {
        use std::process::{Command, Stdio};
        let job = Job::attach(JobLimits::default())
            .expect("Windows 上建作业对象不该失败（除非 SA_RUNNER_SANDBOX=off）");
        let mut child = Command::new("cmd")
            .arg("/C")
            .arg("exit 0")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn cmd");
        assert!(job.assign(&child), "AssignProcessToJobObject 失败——沙箱没挂上");
        let _ = child.wait();
    }

    /// 这条测的不是"句柄建好了"，而是**限制真的生效**。
    /// 只断言 `attach().is_some()` 等于测了一个常量：作业设没设上限都能过。
    ///
    /// 第一版写法是"脚本末尾打印 ALLOCATED-ALL，断言它不出现"。**那是错的测量**：
    /// PowerShell 的默认 `ErrorActionPreference` 是 Continue，分配失败的
    /// `OutOfMemoryException` 属 .NET 终止性错误被吞掉后**循环继续**，
    /// 末尾照样打印 ALLOCATED-ALL —— 于是限制生效与否都判红/判绿，
    /// 测出来的是"PowerShell 会不会吞异常"，不是"内存上限有没有生效"。
    /// 现在改成数成功次数，并且加一组不挂作业的对照：
    /// 对照必须能分（否则测试恒真），实验必须分不动。
    #[cfg(windows)]
    fn alloc_probe(job: Option<&Job>) -> Option<u32> {
        use std::process::{Command, Stdio};
        let script = "$ok=0; 1..4 | % { try { $null = New-Object byte[] (512MB); $ok++ } catch { } }; \"OKCOUNT=$ok\"";
        let mut child = Command::new("powershell")
            .args(["-NoProfile", "-NonInteractive", "-Command", script])
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn powershell");
        if let Some(j) = job {
            assert!(j.assign(&child), "沙箱没挂上，这条断言就没有意义");
        }
        let out = child.wait_with_output().expect("wait");
        let stdout = String::from_utf8_lossy(&out.stdout);
        stdout
            .split("OKCOUNT=")
            .nth(1)
            .and_then(|s| s.split_whitespace().next())
            .and_then(|s| s.parse::<u32>().ok())
    }

    #[cfg(windows)]
    #[test]
    fn memory_limit_actually_stops_runaway_allocation() {
        // ① 对照：不挂作业时必须真能分配成功。少了这一步，测试可能因为
        //    "PowerShell 在这台机器上根本分配不了内存"而恒绿——那就成了假绿。
        let free = alloc_probe(None);
        assert!(
            free.unwrap_or(0) >= 1,
            "对照组（无作业）都分配不出 512MB，这台机器上这条测试没有判定力：{free:?}"
        );

        // ② 实验：同样 4 次 512MB 分配，塞进 128MB 的作业里，必须一次都成不了。
        let job = Job::attach(JobLimits {
            mem_mb: 128,
            max_procs: 4,
            cpu_secs: 0,
        })
        .expect("建作业");
        match alloc_probe(Some(&job)) {
            // 0 次成功 = 上限生效；None = 进程连启动都被限额拦住，同样生效且更严。
            Some(0) | None => {}
            Some(n) => panic!("128MB 作业里成功分配了 {n} 次 512MB —— 内存上限没生效"),
        }
    }
}
