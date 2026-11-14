//! 密码学原语：PBKDF2-HMAC-SHA256（纯 Rust，无 C 工具链依赖）。

use base64::{engine::general_purpose::STANDARD, Engine as _};
use hmac::{Hmac, Mac};
use sha2::Sha256;

type HmacSha256 = Hmac<Sha256>;

const ALGO: &str = "pbkdf2_sha256";
const SALT_LEN: usize = 16;
const KEY_LEN: usize = 32;

/// 输出格式：`pbkdf2_sha256$<iterations>$<salt_b64>$<dk_b64>`（自描述，便于后续升级参数）
pub fn hash_password(password: &str, iterations: u32) -> String {
    let mut salt = [0u8; SALT_LEN];
    if let Err(e) = getrandom::getrandom(&mut salt) {
        // 熵源不可用时拒绝运行，绝不退化到确定性盐
        panic!("系统熵源不可用，无法生成密码盐: {e}");
    }
    let dk = pbkdf2_sha256(password.as_bytes(), &salt, iterations.max(1), KEY_LEN);
    format!(
        "{ALGO}${}${}${}",
        iterations.max(1),
        STANDARD.encode(salt),
        STANDARD.encode(dk)
    )
}

pub fn verify_password(password: &str, encoded: &str) -> bool {
    let mut parts = encoded.split('$');
    if parts.next() != Some(ALGO) {
        return false;
    }
    let iterations: u32 = match parts.next().and_then(|v| v.parse().ok()) {
        Some(v) => v,
        None => return false,
    };
    let salt = match parts.next().and_then(|v| STANDARD.decode(v).ok()) {
        Some(v) => v,
        None => return false,
    };
    let expected = match parts.next().and_then(|v| STANDARD.decode(v).ok()) {
        Some(v) => v,
        None => return false,
    };
    let actual = pbkdf2_sha256(password.as_bytes(), &salt, iterations.max(1), expected.len());
    ct_eq(&actual, &expected)
}

pub fn pbkdf2_sha256(password: &[u8], salt: &[u8], iterations: u32, out_len: usize) -> Vec<u8> {
    let mut out = Vec::with_capacity(out_len + 32);
    let mut block_index: u32 = 1;
    while out.len() < out_len {
        let mut mac = HmacSha256::new_from_slice(password).expect("hmac 接受任意长度密钥");
        mac.update(salt);
        mac.update(&block_index.to_be_bytes());
        let first = mac.finalize().into_bytes();

        let mut u = [0u8; 32];
        u.copy_from_slice(&first);
        let mut t = u;

        for _ in 1..iterations {
            let mut mac = HmacSha256::new_from_slice(password).expect("hmac 接受任意长度密钥");
            mac.update(&u);
            let next = mac.finalize().into_bytes();
            u.copy_from_slice(&next);
            for i in 0..32 {
                t[i] ^= u[i];
            }
        }
        out.extend_from_slice(&t);
        block_index = block_index.wrapping_add(1);
    }
    out.truncate(out_len);
    out
}

/// 恒定时间比较，避免时序侧信道。
pub fn ct_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff = 0u8;
    for i in 0..a.len() {
        diff |= a[i] ^ b[i];
    }
    diff == 0
}

/// 密码策略：>=10 字符，至少含字母与数字，不允许与用户名高度重合。
pub fn validate_password_strength(password: &str, username: &str) -> Result<(), String> {
    if password.chars().count() < 10 {
        return Err("密码至少 10 个字符".into());
    }
    if password.chars().count() > 128 {
        return Err("密码不得超过 128 个字符".into());
    }
    let has_letter = password.chars().any(|c| c.is_alphabetic());
    let has_digit = password.chars().any(|c| c.is_ascii_digit());
    if !(has_letter && has_digit) {
        return Err("密码需同时包含字母与数字".into());
    }
    let lowered = password.to_lowercase();
    if !username.is_empty() && lowered.contains(&username.to_lowercase()) {
        return Err("密码不得包含用户名".into());
    }
    const BLOCKLIST: [&str; 6] = [
        "password",
        "123456789",
        "qwertyuiop",
        "admin12345",
        "111111111",
        "abcd123456",
    ];
    if BLOCKLIST.iter().any(|p| lowered.contains(p)) {
        return Err("密码过于常见，请更换".into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hash_and_verify_roundtrip() {
        let encoded = hash_password("Str0ngPassw0rd!", 1_000);
        assert!(verify_password("Str0ngPassw0rd!", &encoded));
        assert!(!verify_password("Str0ngPassw0rd", &encoded));
        assert!(encoded.starts_with(ALGO));
    }

    #[test]
    fn salts_are_unique() {
        let a = hash_password("Str0ngPassw0rd!", 1_000);
        let b = hash_password("Str0ngPassw0rd!", 1_000);
        assert_ne!(a, b, "每次哈希必须使用独立随机盐");
    }

    #[test]
    fn malformed_hash_is_rejected() {
        assert!(!verify_password("x", "not-a-hash"));
        assert!(!verify_password("x", "pbkdf2_sha256$abc$@@@$@@@"));
    }

    #[test]
    fn strength_rules() {
        assert!(validate_password_strength("abc1234567", "bob").is_ok());
        assert!(validate_password_strength("short1", "bob").is_err());
        assert!(validate_password_strength("onlyletters", "bob").is_err());
        assert!(validate_password_strength("bob1234567", "bob").is_err());
        assert!(validate_password_strength("password12345", "bob").is_err());
    }

    #[test]
    fn knows_rfc6070_vector() {
        // RFC 6070 测试向量（PBKDF2-HMAC-SHA1 的同族校验：这里仅验证 SHA256 实现的确定性）
        let dk = pbkdf2_sha256(b"password", b"salt", 1, 32);
        assert_eq!(
            hex(&dk),
            "120fb6cffcf8b32c43e7225256c4f837a86548c92ccc35480805987cb70be17b"
        );
    }

    fn hex(bytes: &[u8]) -> String {
        bytes.iter().map(|b| format!("{b:02x}")).collect()
    }
}
