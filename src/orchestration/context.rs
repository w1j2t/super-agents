//! 上下文装配：懒加载 + 预算裁剪 + 共享记忆检索（对话共享记忆与上下文优化的落点）。

use crate::domain::memory::{MemoryEntry, MemoryScope};
use crate::domain::user::User;
use crate::store::Store;

/// 单次任务允许装配的上下文预算（字符近似 token）
pub const CONTEXT_BUDGET: usize = 6_000;

#[derive(Debug, Clone)]
pub struct AssembledContext {
    pub summary: String,
    pub memory_refs: Vec<String>,
    pub used_tokens: u64,
    pub truncated: bool,
    /// 懒加载省下的上下文开销：`全量可见记忆的装配成本 - 实际装入成本`。
    ///
    /// 口径为什么这样定：`sa_context_tokens_saved_total` 要能回答"懒加载到底省了多少"，
    /// 唯一可复核的基线就是"如果不做懒加载、把全部可见记忆都塞进上下文会花多少"。
    /// 用别的算法（比如按候选分数加权）都会变成一个没人能重算出来的数字。
    pub tokens_saved: u64,
}

fn rough_tokens(s: &str) -> u64 {
    // 中文约 1 字 ≈ 1 token，英文约 4 字符 ≈ 1 token，取折中估算
    ((s.chars().count() as f64) / 1.4).ceil() as u64
}

/// 记忆可见性：private 仅 owner 可见；shared / system 对全部已认证用户可见。
fn visible(m: &MemoryEntry, user: &User) -> bool {
    match m.scope {
        MemoryScope::Private => m.owner_id == user.id,
        MemoryScope::Shared | MemoryScope::System => true,
    }
}

/// 相关度：key 命中权重高于 tags，tags 高于 content。
fn relevance(m: &MemoryEntry, goal_terms: &[String]) -> f64 {
    let key = m.key.to_lowercase();
    let content = m.content.to_lowercase();
    let mut score = 0.0;
    for t in goal_terms {
        if key.contains(t) {
            score += 0.5;
        }
        if m.tags.iter().any(|tag| tag.to_lowercase().contains(t)) {
            score += 0.3;
        }
        if content.contains(t) {
            score += 0.1;
        }
    }
    // 访问热度与置信度带来温和加成（数据飞轮）
    score + (m.access_count.min(50) as f64) * 0.002 + m.confidence * 0.05
}

/// 轻量切词：按非字母数字边界切分，中文补二元组，避免整句被当成一个词。
fn terms(goal: &str) -> Vec<String> {
    fn push_segment(segment: &str, out: &mut Vec<String>) {
        let chars: Vec<char> = segment.chars().collect();
        if chars.len() < 2 {
            return;
        }
        out.push(segment.to_string());
        // 汉字以字为语义单位，二元组显著提升召回（如「注意写作风格」→「写作」）
        if chars.len() <= 8 {
            for w in chars.windows(2) {
                out.push(w.iter().collect());
            }
        }
    }

    let mut out: Vec<String> = Vec::new();
    let mut buf = String::new();
    for ch in goal.chars() {
        if ch.is_alphanumeric() {
            buf.push(ch.to_ascii_lowercase());
        } else if !buf.is_empty() {
            push_segment(&buf, &mut out);
            buf.clear();
        }
    }
    if !buf.is_empty() {
        push_segment(&buf, &mut out);
    }

    out.sort();
    out.dedup();
    out.truncate(24);
    out
}

/// 装配上下文：按相关度取前 N 条记忆，超预算按“摘要优先、全文裁剪”的方式收敛。
pub fn assemble(store: &Store, user: &User, goal: &str) -> AssembledContext {
    let goal_terms = terms(goal);
    let (picked, all_cost): (Vec<MemoryEntry>, u64) = store.read(|db| {
        let mut candidates: Vec<(f64, MemoryEntry)> = db
            .memories
            .values()
            .filter(|m| visible(m, user))
            .map(|m| (relevance(m, &goal_terms), m.clone()))
            .collect();
        candidates.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
        // 全量成本基线：把"所有可见记忆"都装进去要花的 token。
        // 与下面的懒加载结果相减，就是 `sa_context_tokens_saved_total` 的来源，
        // 谁都能拿同一份 store 复算出同一个数。
        let all_cost: u64 = candidates
            .iter()
            .map(|(_, m)| rough_tokens(&format!("[{:?}/{}] {}", m.scope, m.key, m.content)))
            .sum();
        let picked = candidates
            .iter()
            .filter(|(score, _)| *score > 0.05)
            .take(5)
            .map(|(_, m)| m.clone())
            .collect();
        (picked, all_cost)
    });

    let mut refs = Vec::new();
    let mut lines = Vec::new();
    let mut used = 0u64;
    let mut truncated = false;

    for m in &picked {
        let head = format!("[{:?}/{}] {}", m.scope, m.key, m.content);
        let cost = rough_tokens(&head);
        if (used as usize) + cost as usize > CONTEXT_BUDGET {
            // 预算不足：只保留摘要式头部，不再追加全文
            let brief: String = head.chars().take(120).collect();
            let cost_brief = rough_tokens(&brief);
            if (used as usize) + cost_brief as usize <= CONTEXT_BUDGET {
                lines.push(format!("{brief}…（已裁剪）"));
                used += cost_brief;
                refs.push(m.id.clone());
            }
            truncated = true;
            break;
        }
        lines.push(head);
        used += cost;
        refs.push(m.id.clone());
    }

    // 记录访问热度（数据飞轮：被用到的记忆权重上升）
    if !refs.is_empty() {
        store.write(|db| {
            for id in &refs {
                if let Some(m) = db.memories.get_mut(id) {
                    m.access_count += 1;
                }
            }
        });
    }

    let summary = if lines.is_empty() {
        "（无相关历史记忆，本次按通用策略执行）".to_string()
    } else {
        lines.join("\n")
    };

    AssembledContext {
        summary,
        memory_refs: refs,
        used_tokens: used,
        truncated,
        // 用 saturating_sub：`all_cost` 统计的是"未裁剪的全文"，理论上恒 ≥ used；
        // 但一旦有人改动上面两条口径（例如给 head 加前缀而不更新基线），
        // 这里宁可归零也不产生一个负数的 u64 回绕（那会变成天文数字，直接污染监控）。
        tokens_saved: all_cost.saturating_sub(used),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::memory::MemoryScope;
    use crate::security::rbac::Role;
    use chrono::Utc;

    fn user(id: &str) -> User {
        let now = Utc::now();
        User {
            id: id.into(),
            username: id.into(),
            username_lower: id.into(),
            email: format!("{id}@example.com"),
            email_lower: format!("{id}@example.com"),
            display_name: id.into(),
            password_hash: "x".into(),
            role: Role::Owner,
            status: crate::domain::user::UserStatus::Active,
            locale: "zh-CN".into(),
            timezone: "Asia/Shanghai".into(),
            created_at: now,
            updated_at: now,
            last_login_at: None,
            failed_login_count: 0,
            locked_until: None,
        }
    }

    fn mem(id: &str, owner: &str, scope: MemoryScope, key: &str, content: &str) -> MemoryEntry {
        let now = Utc::now();
        MemoryEntry {
            id: id.into(),
            scope,
            owner_id: owner.into(),
            key: key.into(),
            content: content.into(),
            tags: vec!["写作".into()],
            version: 1,
            written_by: "super-agent".into(),
            created_at: now,
            updated_at: now,
            access_count: 0,
            confidence: 0.8,
        }
    }

    #[test]
    fn picks_relevant_and_hides_others_private() {
        let store = Store::in_memory();
        store.write(|db| {
            db.memories.insert(
                "m1".into(),
                mem("m1", "u1", MemoryScope::Private, "写作风格", "偏好简洁书面语"),
            );
            db.memories.insert(
                "m2".into(),
                mem("m2", "u2", MemoryScope::Private, "写作风格", "别人的私密记忆"),
            );
        });
        let ctx = assemble(&store, &user("u1"), "帮我写文档，注意写作风格");
        assert_eq!(ctx.memory_refs, vec!["m1".to_string()]);
        assert!(!ctx.summary.contains("别人的私密记忆"));
    }

    #[test]
    fn respects_budget_and_reports_truncation() {
        let store = Store::in_memory();
        store.write(|db| {
            for i in 0..5 {
                db.memories.insert(
                    format!("m{i}"),
                    mem(
                        &format!("m{i}"),
                        "u1",
                        MemoryScope::Shared,
                        &format!("写作文档{i}"),
                        &"很长的内容".repeat(900),
                    ),
                );
            }
        });
        let ctx = assemble(&store, &user("u1"), "写文档");
        assert!(ctx.used_tokens <= CONTEXT_BUDGET as u64 + 200);
        assert!(ctx.truncated);
    }

    #[test]
    fn empty_when_no_match() {
        let store = Store::in_memory();
        let ctx = assemble(&store, &user("u1"), "完全无关的目标");
        assert!(ctx.memory_refs.is_empty());
        assert!(ctx.summary.contains("无相关历史记忆"));
    }
}
