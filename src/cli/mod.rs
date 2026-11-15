//! 命令行层：一个二进制，两种身份。
//!
//! `super-agents`（不带子命令）= 现在这套 HTTP 后端，行为与加 CLI 之前**逐字节一致**；
//! 带子命令时执行本地动作：
//!
//! | 子命令   | 干什么                         | 是否需要服务在跑 |
//! |----------|--------------------------------|------------------|
//! | `serve`  | 起 HTTP 后端（默认）           | —                |
//! | `doctor` | 只读自检，服务起不来也能跑     | 否               |
//! | `login`  | 换令牌并落盘                   | 是               |
//! | `logout` | 删本地令牌                     | 否               |
//! | `submit` | 提交任务                       | 是               |
//! | `ls`     | 列任务                         | 是               |
//! | `show`   | 看单个任务                     | 是               |
//!
//! ## 这些子命令存在的理由
//!
//! 后端做完了，但每天真正要干的活是"提交一个任务、看它跑成什么样"。
//! 没有 CLI 时，这动作要在浏览器/`curl` 里拼 JSON、手工复制 Bearer token——
//! 于是人人都会另写一个一次性脚本，二十个脚本二十套参数拼法，
//! 最后没人知道哪个才是当前契约。把这条路径收进二进制，契约只有一份。
//!
//! ## 为什么不拆成第二个二进制
//!
//! CLI 要读的是同一套 `SA_*` 环境变量、同一套 `Config` 校验、同一套槽位枚举。
//! 拆成两个 bin 就是两份 `from_env`、两份默认值、两份"非法值回落"，
//! 迟早会出现"`doctor` 说配置没问题、`serve` 却起不来"——
//! 这类故障最难查，因为两个入口各自都能自圆其说。
//!
//! ## 为什么默认（无参数）必须是 serve
//!
//! 全部门禁脚本（大半）都是以 `[exe]` 无参启动进程再打端口。
//! 只要默认行为漂移一格，回归矩阵会集体变红，而那时最容易被误判成"功能坏了"。
//! 所以 `Cmd` 用 `Option`，`None` 与 `Serve` 走同一条路，且这条路是原封不动的老代码。

pub mod auth;
pub mod doctor;
pub mod http;
pub mod session;
pub mod task;

use clap::{Args, Parser, Subcommand};

#[derive(Debug, Parser)]
#[command(
    name = "super-agents",
    version,
    about = "个人超级 Agents 后端：不带子命令时启动 HTTP 服务",
    long_about = "个人超级 Agents 后端。\n\n\
                  不带子命令（或 `serve`）启动 HTTP 服务，读 SA_* 环境变量；\n\
                  `doctor` 做只读自检，服务起不来时也能跑；\n\
                  `login` / `logout` 管本地令牌；`submit` / `ls` / `show` 提交与查看任务。\n\n\
                  除 `doctor` 与 `logout` 外，其余子命令都需要服务已在运行。"
)]
pub struct Cli {
    #[command(subcommand)]
    pub cmd: Option<Cmd>,
}

#[derive(Debug, Subcommand)]
pub enum Cmd {
    /// 启动 HTTP 后端（不带任何子命令时的默认行为）
    Serve,
    /// 只读自检：打印各槽位真实取值与可达性，不启动服务、不写业务状态
    Doctor(DoctorArgs),
    /// 登录并把令牌存到本地（后续 submit / ls / show 自动使用）
    Login(LoginArgs),
    /// 删掉本地令牌（不动服务端会话，换账号时用）
    Logout(LogoutArgs),
    /// 提交一个任务（字段与 POST /api/v1/tasks 一一对应）
    Submit(SubmitArgs),
    /// 列出任务（默认第一页；服务端字段怎么给就怎么显示）
    #[command(visible_alias = "list")]
    Ls(LsArgs),
    /// 看单个任务详情（含结果摘要）
    #[command(visible_alias = "get")]
    Show(ShowArgs),
}

/// 所有需要联网的子命令共用这两项。
///
/// 刻意不提供 `--base-url` 这种"整条 URL"参数：CLI 只连本机这台服务，
/// 让人填 scheme 只会多一种出错方式（`http://` 拼两遍、末尾多一个 `/`）。
#[derive(Debug, Args, Clone)]
pub struct ConnArgs {
    /// 服务地址，形如 127.0.0.1:8088；缺省取 SA_BIND，再缺省 127.0.0.1:8088
    #[arg(long)]
    pub addr: Option<String>,
    /// 直接给 access_token（缺省先看 SA_TOKEN，再看本地令牌文件）
    #[arg(long)]
    pub token: Option<String>,
}

#[derive(Debug, Args)]
pub struct LoginArgs {
    #[command(flatten)]
    pub conn: ConnArgs,
    /// 用户名或邮箱；缺省取 SA_ACCOUNT
    #[arg(long)]
    pub account: Option<String>,
    /// 密码；缺省取 SA_PASSWORD（**推荐**用环境变量，别写进命令行历史）
    #[arg(long)]
    pub password: Option<String>,
    /// 把 access_token 原样打印出来（默认不打印，避免进日志与门禁证据）
    #[arg(long)]
    pub show_token: bool,
}

#[derive(Debug, Args)]
pub struct LogoutArgs {
    #[command(flatten)]
    pub conn: ConnArgs,
}

#[derive(Debug, Args)]
pub struct SubmitArgs {
    #[command(flatten)]
    pub conn: ConnArgs,
    /// 任务目标（自然语言，原样提交，不做改写）
    pub goal: String,
    /// 任务类型：coding / writing / research / planning / ideation / collaboration
    #[arg(long)]
    pub task_type: Option<String>,
    /// sync（等跑完再返回）或 async（立刻返回，用 show 看进展）；缺省由服务端决定
    #[arg(long)]
    pub mode: Option<String>,
    /// 模型档位，原样透传
    #[arg(long)]
    pub model_profile: Option<String>,
    /// 本次任务 token 预算
    #[arg(long)]
    pub budget_tokens: Option<u64>,
    /// 提交后跟随事件流直到终态（只对异步受理有意义；跟随中 Ctrl+C 只退跟随，任务照跑）
    #[arg(long)]
    pub follow: bool,
    /// 机器可读输出（完整响应 JSON）
    #[arg(long)]
    pub json: bool,
}

#[derive(Debug, Args)]
pub struct LsArgs {
    #[command(flatten)]
    pub conn: ConnArgs,
    /// 每页条数（服务端缺省生效）
    #[arg(long)]
    pub limit: Option<u32>,
    /// 跳过前 N 条
    #[arg(long)]
    pub offset: Option<u32>,
    /// 按状态过滤
    #[arg(long)]
    pub status: Option<String>,
    /// 按任务类型过滤
    #[arg(long)]
    pub task_type: Option<String>,
    /// 关键词搜索
    #[arg(long)]
    pub q: Option<String>,
    /// 机器可读输出（完整响应 JSON）
    #[arg(long)]
    pub json: bool,
}

#[derive(Debug, Args)]
pub struct ShowArgs {
    #[command(flatten)]
    pub conn: ConnArgs,
    /// 任务 id
    pub id: String,
    /// 机器可读输出（完整响应 JSON）
    #[arg(long)]
    pub json: bool,
}


#[derive(Debug, Args)]
pub struct DoctorArgs {
    /// 机器可读输出（给脚本 / 门禁消费）
    #[arg(long)]
    pub json: bool,
    /// 把 WARN 也当作失败（退出码 1）：CI 里用
    #[arg(long)]
    pub strict: bool,
}
