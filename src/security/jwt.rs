//! 自实现 HS256 JWT（避免 ring/openssl 等 C 依赖，Windows 上开箱可编译）。

use crate::security::password::ct_eq;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine as _;
use hmac::{Hmac, Mac};
use serde::{Deserialize, Serialize};
use sha2::Sha256;

type HmacSha256 = Hmac<Sha256>;

pub const TOKEN_TYPE_ACCESS: &str = "access";
pub const TOKEN_TYPE_REFRESH: &str = "refresh";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Claims {
    pub sub: String,
    pub role: String,
    pub typ: String,
    pub iat: i64,
    pub exp: i64,
    pub jti: String,
}

#[derive(Debug, thiserror::Error)]
pub enum JwtError {
    #[error("token 格式非法")]
    Malformed,
    #[error("token 签名不匹配")]
    BadSignature,
    #[error("token 已过期")]
    Expired,
    #[error("token 类型不匹配")]
    WrongType,
    #[error("token 头部算法不被接受")]
    BadAlgorithm,
}

#[derive(Serialize, Deserialize)]
struct Header {
    alg: String,
    typ: String,
}

pub fn issue(secret: &[u8], claims: &Claims) -> String {
    let header = Header {
        alg: "HS256".into(),
        typ: "JWT".into(),
    };
    let h = URL_SAFE_NO_PAD.encode(serde_json::to_vec(&header).expect("header 可序列化"));
    let p = URL_SAFE_NO_PAD.encode(serde_json::to_vec(claims).expect("claims 可序列化"));
    let signing_input = format!("{h}.{p}");
    let sig = sign(secret, signing_input.as_bytes());
    format!("{signing_input}.{}", URL_SAFE_NO_PAD.encode(sig))
}

/// 校验签名、算法、过期时间与 token 类型；`now` 由调用方注入便于测试。
pub fn verify(
    secret: &[u8],
    token: &str,
    expected_type: &str,
    now: i64,
) -> Result<Claims, JwtError> {
    let mut it = token.split('.');
    let (h, p, s) = match (it.next(), it.next(), it.next(), it.next()) {
        (Some(h), Some(p), Some(s), None) => (h, p, s),
        _ => return Err(JwtError::Malformed),
    };

    let header_raw = URL_SAFE_NO_PAD.decode(h).map_err(|_| JwtError::Malformed)?;
    let header: Header =
        serde_json::from_slice(&header_raw).map_err(|_| JwtError::Malformed)?;
    if !header.alg.eq_ignore_ascii_case("HS256") {
        return Err(JwtError::BadAlgorithm);
    }

    let expected_sig = sign(secret, format!("{h}.{p}").as_bytes());
    let actual_sig = URL_SAFE_NO_PAD.decode(s).map_err(|_| JwtError::Malformed)?;
    if !ct_eq(&expected_sig, &actual_sig) {
        return Err(JwtError::BadSignature);
    }

    let payload_raw = URL_SAFE_NO_PAD.decode(p).map_err(|_| JwtError::Malformed)?;
    let claims: Claims = serde_json::from_slice(&payload_raw).map_err(|_| JwtError::Malformed)?;

    if claims.exp <= now {
        return Err(JwtError::Expired);
    }
    if claims.typ != expected_type {
        return Err(JwtError::WrongType);
    }
    Ok(claims)
}

fn sign(secret: &[u8], input: &[u8]) -> Vec<u8> {
    let mut mac = HmacSha256::new_from_slice(secret).expect("hmac 接受任意长度密钥");
    mac.update(input);
    mac.finalize().into_bytes().to_vec()
}

/// 供 refresh token 入库用的稳定指纹（不落原文）。
pub fn fingerprint(token: &str) -> String {
    use sha2::Digest;
    let mut hasher = Sha256::new();
    hasher.update(token.as_bytes());
    URL_SAFE_NO_PAD.encode(hasher.finalize())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn claims() -> Claims {
        Claims {
            sub: "user-1".into(),
            role: "member".into(),
            typ: TOKEN_TYPE_ACCESS.into(),
            iat: 1_000,
            exp: 2_000,
            jti: "jti-1".into(),
        }
    }

    #[test]
    fn roundtrip_ok() {
        let secret = b"unit-test-secret-key-0123456789abcdef";
        let token = issue(secret, &claims());
        let got = verify(secret, &token, TOKEN_TYPE_ACCESS, 1_500).unwrap();
        assert_eq!(got.sub, "user-1");
    }

    #[test]
    fn rejects_tampered_payload() {
        let secret = b"unit-test-secret-key-0123456789abcdef";
        let token = issue(secret, &claims());
        let mut parts: Vec<&str> = token.split('.').collect();
        let forged = URL_SAFE_NO_PAD.encode(
            serde_json::to_vec(&Claims {
                sub: "user-2".into(),
                ..claims()
            })
            .unwrap(),
        );
        parts[1] = &forged;
        let tampered = parts.join(".");
        assert!(matches!(
            verify(secret, &tampered, TOKEN_TYPE_ACCESS, 1_500),
            Err(JwtError::BadSignature)
        ));
    }

    #[test]
    fn rejects_expired_and_wrong_type() {
        let secret = b"unit-test-secret-key-0123456789abcdef";
        let token = issue(secret, &claims());
        assert!(matches!(
            verify(secret, &token, TOKEN_TYPE_ACCESS, 2_001),
            Err(JwtError::Expired)
        ));
        assert!(matches!(
            verify(secret, &token, TOKEN_TYPE_REFRESH, 1_500),
            Err(JwtError::WrongType)
        ));
    }

    #[test]
    fn rejects_alg_none() {
        let secret = b"unit-test-secret-key-0123456789abcdef";
        let header = URL_SAFE_NO_PAD.encode(br#"{"alg":"none","typ":"JWT"}"#);
        let payload = URL_SAFE_NO_PAD.encode(serde_json::to_vec(&claims()).unwrap());
        let token = format!("{header}.{payload}.");
        assert!(matches!(
            verify(secret, &token, TOKEN_TYPE_ACCESS, 1_500),
            Err(JwtError::BadAlgorithm)
        ));
    }
}
