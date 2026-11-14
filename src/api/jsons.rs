//! 带错误契约的请求提取器。
//!
//! 直接用 axum 的 `Json` / `Query` / `Path` 时，反序列化失败会被 axum 默认拒绝处理成
//! **纯文本**（例如 `Failed to deserialize the JSON body ... missing field \`email\``），
//! 前端既拿不到 `code`，也拿不到字段名，只能哑着提示"请求失败"。
//!
//! 这里把三类提取错误统一收敛成 `ApiError`，于是全站错误体形状只有一种：
//! ```json
//! { "code": "unprocessable_entity", "message": "请求体缺少必填字段",
//!   "details": { "email": "必填" } }
//! ```
//! 前端据此可直接在对应输入框下挂红字。

use crate::error::ApiError;
use axum::extract::rejection::{JsonRejection, PathRejection, QueryRejection};
use axum::extract::{FromRequest, FromRequestParts, Request};
use axum::http::request::Parts;
use serde::de::DeserializeOwned;
use serde::Serialize;
use std::collections::BTreeMap;

/// 与 axum `Json` 用法一致的请求体提取器，但拒绝时返回统一 Problem JSON。
pub struct ApiJson<T>(pub T);

impl<T, S> FromRequest<S> for ApiJson<T>
where
    T: DeserializeOwned,
    S: Send + Sync,
    axum::Json<T>: FromRequest<S, Rejection = JsonRejection>,
{
    type Rejection = ApiError;

    async fn from_request(req: Request, state: &S) -> Result<Self, Self::Rejection> {
        match axum::Json::<T>::from_request(req, state).await {
            Ok(axum::Json(value)) => Ok(ApiJson(value)),
            Err(rej) => Err(map_json_rejection(rej)),
        }
    }
}

/// 与 axum `Query` 用法一致的查询串提取器。
pub struct ApiQuery<T>(pub T);

impl<T, S> FromRequestParts<S> for ApiQuery<T>
where
    T: DeserializeOwned,
    S: Send + Sync,
    axum::extract::Query<T>: FromRequestParts<S, Rejection = QueryRejection>,
{
    type Rejection = ApiError;

    async fn from_request_parts(parts: &mut Parts, state: &S) -> Result<Self, Self::Rejection> {
        match axum::extract::Query::<T>::from_request_parts(parts, state).await {
            Ok(axum::extract::Query(value)) => Ok(ApiQuery(value)),
            Err(rej) => Err(map_query_rejection(rej)),
        }
    }
}

/// 与 axum `Path` 用法一致的路径参数提取器。
pub struct ApiPath<T>(pub T);

impl<T, S> FromRequestParts<S> for ApiPath<T>
where
    T: DeserializeOwned + Send,
    S: Send + Sync,
    axum::extract::Path<T>: FromRequestParts<S, Rejection = PathRejection>,
{
    type Rejection = ApiError;

    async fn from_request_parts(parts: &mut Parts, state: &S) -> Result<Self, Self::Rejection> {
        match axum::extract::Path::<T>::from_request_parts(parts, state).await {
            Ok(axum::extract::Path(value)) => Ok(ApiPath(value)),
            Err(rej) => Err(map_path_rejection(rej)),
        }
    }
}

fn map_json_rejection(rej: JsonRejection) -> ApiError {
    let raw = rej.body_text();
    let (field, hint) = parse_serde_error(&raw);
    match field {
        Some(f) => ApiError::Validation(vec![(f, hint)]),
        None => ApiError::BadRequest(format!("请求体格式不正确：{hint}")),
    }
}

/// 查询串错误**一律** 422 字段级：与请求体校验同一形状。
///
/// 早前这里对"抽不出字段名"的情况返回 400 并附带 serde 英文原文，
/// 结果是前端要同时处理 400/422 两条分支，且错误文案不可直接展示给用户。
/// 现在收敛为一种：`422 { code, message, details: { <参数名>: <中文原因> } }`。
fn map_query_rejection(rej: QueryRejection) -> ApiError {
    let raw = rej.body_text();
    let (field, hint) = parse_serde_error(&raw);
    let field = field.unwrap_or_else(|| "_query".to_string());
    ApiError::Validation(vec![(field, hint)])
}

/// 路径参数错误同样收敛成 422 字段级（字段名固定 `_path`）。
fn map_path_rejection(rej: PathRejection) -> ApiError {
    let raw = rej.body_text();
    let hint = translate_reason(&raw);
    ApiError::Validation(vec![("_path".to_string(), hint)])
}

/// 把 serde 的类型名翻成中文，避免把 `usize` / `String` 这类词抛给用户。
fn translate_type_name(raw: &str) -> String {
    let t = raw.trim().trim_end_matches(',');
    match t {
        "usize" | "u8" | "u16" | "u32" | "u64" => "非负整数".into(),
        "isize" | "i8" | "i16" | "i32" | "i64" => "整数".into(),
        "f32" | "f64" => "数字".into(),
        "bool" => "true / false".into(),
        "string" | "string or map" | "a string" => "字符串".into(),
        other => other.to_string(),
    }
}

/// 把 serde 的原因片段翻成中文（`parse_serde_error` 的底层翻译器）。
fn translate_reason(reason: &str) -> String {
    let lower = reason.to_lowercase();
    if lower.contains("invalid digit") {
        return "必须是非负整数".into();
    }
    if lower.contains("number too large") || lower.contains("value too large") {
        return "数值超出允许范围".into();
    }
    if lower.contains("invalid type") {
        if let Some(rest) = reason.split("expected ").nth(1) {
            let expect = rest.split(" at ").next().unwrap_or(rest);
            return format!("字段类型不正确，期望 {}", translate_type_name(expect));
        }
        return "字段类型不正确".into();
    }
    if lower.contains("unknown field") {
        if let Some(rest) = reason.split("unknown field `").nth(1) {
            if let Some(name) = rest.split('`').next() {
                return format!("未知字段 `{name}`，请检查拼写或查阅接口文档");
            }
        }
        return "存在未知字段".into();
    }
    if lower.contains("eof") || lower.contains("expected value") || lower.contains("syntax") {
        return "格式不合法".into();
    }
    if lower.contains("invalid length") {
        return "长度超出允许范围".into();
    }
    if lower.contains("missing field") {
        if let Some(rest) = reason.split("missing field `").nth(1) {
            if let Some(name) = rest.split('`').next() {
                return format!("缺少必填字段 `{name}`");
            }
        }
        return "缺少必填字段".into();
    }
    reason.trim().to_string()
}

/// serde 的英文报错翻译成"人能直接照做"的中文，并尽量抽出字段名。
///
/// 支持两种来源：
/// - 请求体（`JsonRejection`）：`missing field \`email\` at line 1 column 28`
/// - 查询串（`QueryRejection`，serde_urlencoded）：`limit: invalid digit found in string`
///
/// 后者带 `字段名: 原因` 前缀，早前未识别，导致前端只能看到 400 + 英文原文。
fn parse_serde_error(raw: &str) -> (Option<String>, String) {
    // serde_urlencoded / axum 会在真正原因前面套一层英文前缀，先剥掉再判字段名。
    // 例：`Failed to deserialize query string: limit: invalid digit found in string`
    const PREFIXES: [&str; 3] = [
        "Failed to deserialize query string: ",
        "Failed to deserialize the JSON body into the target type: ",
        "Failed to deserialize path parameters: ",
    ];
    let raw = raw.trim();
    for p in PREFIXES {
        if let Some(rest) = raw.strip_prefix(p) {
            return parse_serde_error(rest);
        }
    }

    // 再尝试 `field: reason` 形式（查询串）。字段名只允许标识符字符，避免误判英文句子。
    if let Some((head, tail)) = raw.split_once(": ") {
        let head = head.trim();
        let looks_like_field = !head.is_empty()
            && head.len() <= 40
            && head
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-' || c == '.');
        if looks_like_field && !tail.trim().is_empty() {
            return (Some(head.to_string()), translate_reason(tail));
        }
    }

    let lower = raw.to_lowercase();

    if let Some(rest) = raw.split("missing field `").nth(1) {
        if let Some(name) = rest.split('`').next() {
            return (Some(name.to_string()), "必填".into());
        }
    }

    if lower.contains("unknown field") {
        if let Some(rest) = raw.split("unknown field `").nth(1) {
            if let Some(name) = rest.split('`').next() {
                return (
                    Some(name.to_string()),
                    "未知字段，请检查拼写或查阅接口文档".into(),
                );
            }
        }
    }

    if lower.contains("invalid type") {
        if lower.contains("expected") {
            let expect = raw
                .split("expected ")
                .nth(1)
                .map(|s| s.split(" at line").next().unwrap_or(s).trim().to_string())
                .unwrap_or_default();
            return (None, format!("字段类型不正确，期望 {expect}"));
        }
        return (None, "字段类型不正确".into());
    }

    if lower.contains("invalid value") || lower.contains("data did not match") {
        return (None, "取值不在允许范围内".into());
    }
    if lower.contains("eof") || lower.contains("expected value") || lower.contains("syntax") {
        return (None, "不是合法的 JSON".into());
    }
    if lower.contains("invalid length") || lower.contains("value too large") {
        return (None, "长度或数值超出允许范围".into());
    }

    (None, raw.trim().to_string())
}

// ---------------------------------------------------------------------------
// 分页：全站列表接口共用一个外壳，前端一套逻辑即可渲染所有列表
// ---------------------------------------------------------------------------

/// 列表查询参数（所有列表接口共用）。
#[derive(Debug, Clone, serde::Deserialize)]
pub struct PageQuery {
    #[serde(default)]
    pub limit: Option<usize>,
    #[serde(default)]
    pub offset: Option<usize>,
}

/// 归一化分页窗口（供 `serde_urlencoded` 场景使用）。
///
/// 注意：**查询串不能用 `#[serde(flatten)]`** —— `serde_urlencoded` 不支持 flatten，
/// 会在运行时把整个请求判为 400。因此各 Query 结构显式声明 `limit` / `offset`，
/// 再调用本函数归一化。
pub fn window(
    limit: Option<usize>,
    offset: Option<usize>,
    default_limit: usize,
    max_limit: usize,
) -> (usize, usize) {
    (
        limit.unwrap_or(default_limit).clamp(1, max_limit),
        offset.unwrap_or(0),
    )
}

/// 归一化分页窗口，**越界则 422 而不是静默夹持**（列表接口统一用它）。
///
/// 为什么要和 `window()` 分开：静默把 `limit=0` 改成 1、把 `limit=100000` 改成上限，
/// 前端看到的是 `200`，实际拿到的条数与它请求的不一致，属于"静默改语义"。
/// 列表接口一律走本函数，越界即明确报错并给出允许范围。
pub fn window_checked(
    limit: Option<usize>,
    offset: Option<usize>,
    default_limit: usize,
    max_limit: usize,
) -> Result<(usize, usize), ApiError> {
    let limit = match limit {
        None => default_limit,
        Some(v) if (1..=max_limit).contains(&v) => v,
        Some(v) => {
            return Err(ApiError::Validation(vec![(
                "limit".to_string(),
                format!("允许范围 1..={max_limit}，不传则默认 {default_limit}；收到 {v}"),
            )]));
        }
    };
    Ok((limit, offset.unwrap_or(0)))
}

/// 归一化搜索关键词：去首尾空白、空串视为不搜索、超长明确 422。
///
/// 为什么要有上限：关键词会进 `contains` 全量比对，超长关键词等于把
/// "一次请求做 O(条数 × 关键词长度) 的匹配"交给外部随意放大。
/// 上限按**字符**算（不是字节），中文关键词不会被误伤。
pub fn normalize_keyword(
    raw: Option<&str>,
    field: &str,
    max_chars: usize,
) -> Result<Option<String>, ApiError> {
    match raw.map(str::trim).filter(|s| !s.is_empty()) {
        None => Ok(None),
        Some(s) if s.chars().count() > max_chars => Err(ApiError::Validation(vec![(
            field.to_string(),
            format!("搜索关键词最多 {max_chars} 个字符"),
        )])),
        Some(s) => Ok(Some(s.to_lowercase())),
    }
}

impl PageQuery {
    /// 归一化分页窗口：limit 夹在 [1, max]，offset 允许 0。
    pub fn window(&self, default_limit: usize, max_limit: usize) -> (usize, usize) {
        let limit = self.limit.unwrap_or(default_limit).clamp(1, max_limit);
        let offset = self.offset.unwrap_or(0);
        (limit, offset)
    }
}

/// 统一列表外壳：`total` 是过滤后的全量数，`count` 是本页条数，`has_more` 供无限滚动。
#[derive(Debug, Serialize)]
pub struct Page<T> {
    pub total: usize,
    pub count: usize,
    pub limit: usize,
    pub offset: usize,
    pub has_more: bool,
    pub items: Vec<T>,
}

impl<T> Page<T> {
    pub fn new(items: Vec<T>, total: usize, limit: usize, offset: usize) -> Self {
        let count = items.len();
        Self {
            total,
            count,
            limit,
            offset,
            has_more: offset + count < total,
            items,
        }
    }
}

/// 字段级错误明细的便捷构造（供 handler 复用）。
pub fn details(pairs: &[(&str, &str)]) -> BTreeMap<String, String> {
    pairs
        .iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn missing_field_becomes_field_level_hint() {
        let raw = "Failed to deserialize the JSON body into the target type: missing field `email` at line 1 column 28";
        let (field, hint) = parse_serde_error(raw);
        assert_eq!(field.as_deref(), Some("email"));
        assert_eq!(hint, "必填");
    }

    #[test]
    fn invalid_type_is_translated() {
        let raw = "Failed to deserialize the JSON body into the target type: invalid type: integer `5`, expected a string at line 1 column 12";
        let (field, hint) = parse_serde_error(raw);
        assert!(field.is_none());
        assert!(hint.contains("类型不正确"), "实际: {hint}");
    }

    #[test]
    fn unknown_field_is_translated() {
        let raw = "unknown field `foo`, expected one of `scope`, `key`";
        let (field, hint) = parse_serde_error(raw);
        assert_eq!(field.as_deref(), Some("foo"));
        assert!(hint.contains("未知字段"));
    }

    #[test]
    fn window_clamps_and_defaults() {
        let q = PageQuery {
            limit: Some(0),
            offset: None,
        };
        assert_eq!(q.window(50, 200), (1, 0));
        let q = PageQuery {
            limit: Some(9999),
            offset: Some(20),
        };
        assert_eq!(q.window(50, 200), (200, 20));
    }

    #[test]
    fn page_computes_has_more() {
        let p = Page::new(vec![1, 2], 5, 2, 0);
        assert!(p.has_more);
        let p = Page::new(vec![5], 5, 2, 4);
        assert!(!p.has_more);
    }

    // ---- 查询串错误：必须能抽出字段名并翻成中文（此前会漏成 400 + 英文原文）----

    #[test]
    fn query_invalid_digit_becomes_field_level_chinese_hint() {
        let raw = "limit: invalid digit found in string";
        let (field, hint) = parse_serde_error(raw);
        assert_eq!(field.as_deref(), Some("limit"));
        assert_eq!(hint, "必须是非负整数");
    }

    #[test]
    fn query_invalid_type_never_leaks_rust_type_name() {
        let raw = "offset: invalid type: string \"abc\", expected usize";
        let (field, hint) = parse_serde_error(raw);
        assert_eq!(field.as_deref(), Some("offset"));
        assert!(hint.contains("非负整数"), "实际: {hint}");
        assert!(!hint.contains("usize"), "不应把 Rust 类型名抛给用户: {hint}");
    }

    #[test]
    fn query_error_never_expects_english_sentence_head() {
        // 英文句子里的 ": " 不能被误判成“字段名: 原因”
        let raw = "Failed to deserialize query string: expected value at line 1 column 1";
        let (field, _hint) = parse_serde_error(raw);
        assert!(
            field.is_none() || field.as_deref() == Some("_query"),
            "误判字段名: {field:?}"
        );
    }

    #[test]
    fn axum_query_prefix_is_stripped_before_field_detection() {
        // axum 的实际形态：英文前缀 + `字段名: serde 原因`，两层都要剥
        let raw = "Failed to deserialize query string: limit: invalid digit found in string";
        let (field, hint) = parse_serde_error(raw);
        assert_eq!(field.as_deref(), Some("limit"));
        assert_eq!(hint, "必须是非负整数");
    }

    #[test]
    fn body_prefix_is_stripped_before_field_detection() {
        let raw = "Failed to deserialize the JSON body into the target type: missing field `email` at line 1 column 28";
        let (field, hint) = parse_serde_error(raw);
        assert_eq!(field.as_deref(), Some("email"));
        assert_eq!(hint, "必填");
    }

    #[test]
    fn keyword_is_trimmed_lowercased_and_bounded() {
        assert_eq!(normalize_keyword(Some("  Rust  "), "q", 200).unwrap(), Some("rust".into()));
        assert_eq!(normalize_keyword(Some("   "), "q", 200).unwrap(), None);
        assert_eq!(normalize_keyword(None, "q", 200).unwrap(), None);
        // 按字符算：200 个汉字应通过，201 个拒绝（不被 UTF-8 字节长度误伤）
        let cn200: String = "汉".repeat(200);
        assert!(normalize_keyword(Some(&cn200), "q", 200).is_ok());
        let cn201: String = "汉".repeat(201);
        match normalize_keyword(Some(&cn201), "q", 200).unwrap_err() {
            ApiError::Validation(v) => {
                assert_eq!(v[0].0, "q");
                assert!(v[0].1.contains("最多 200 个字符"), "实际: {}", v[0].1);
            }
            other => panic!("期望 Validation，实际 {other:?}"),
        }
    }

    // ---- 分页窗口：越界必须 422，不许静默改语义 ----

    #[test]
    fn limit_zero_is_rejected_instead_of_silently_clamped() {
        let err = window_checked(Some(0), None, 50, 200).unwrap_err();
        match err {
            ApiError::Validation(v) => {
                let (field, hint) = &v[0];
                assert_eq!(field, "limit");
                assert!(hint.contains("1..=200"), "实际: {hint}");
                assert!(hint.contains("收到 0"), "实际: {hint}");
            }
            other => panic!("期望 Validation，实际 {other:?}"),
        }
    }

    #[test]
    fn limit_over_max_is_rejected_with_range() {
        let err = window_checked(Some(100000), Some(20), 50, 200).unwrap_err();
        match err {
            ApiError::Validation(v) => {
                assert_eq!(v[0].0, "limit");
                assert!(v[0].1.contains("收到 100000"), "实际: {}", v[0].1);
            }
            other => panic!("期望 Validation，实际 {other:?}"),
        }
    }

    #[test]
    fn limit_in_range_and_default_pass_through() {
        assert_eq!(window_checked(None, None, 50, 200).unwrap(), (50, 0));
        assert_eq!(window_checked(Some(1), Some(0), 50, 200).unwrap(), (1, 0));
        assert_eq!(window_checked(Some(200), Some(999999), 50, 200).unwrap(), (200, 999999));
    }
}
