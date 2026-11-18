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

/// 三方审查低危 3：自实现 JWT 原先只校验 alg/exp/typ/sub，没有 iss/aud，
/// 令牌可被拿去其它同密钥服务重放。现在把签发者/受众固化进 Claims 并在
/// `verify` 里强校验——密钥相同但域不同的服务之间不再互认。
pub const ISSUER: &str = "sa-backend";
pub const AUDIENCE: &str = "sa-frontend";

fn default_iss() -> String {
    ISSUER.to_string()
}

fn default_aud() -> String {
    AUDIENCE.to_string()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Claims {
    pub sub: String,
    pub role: String,
    pub typ: String,
    pub iat: i64,
    pub exp: i64,
    pub jti: String,
    /// 会话 id：把 access 令牌绑到某一次登录上。
    ///
    /// 没有它，"登出"只是删掉 refresh 令牌——已经发出去的 access 令牌还能继续用到过期
    /// （默认 30 分钟），改密/踢下线同理。有了它，鉴权时可以顺带问一句"这次登录还在不在"。
    ///
    /// `serde(default)`：改造前签发的令牌没有这个字段，缺失按空串处理，
    /// 提取器据此跳过会话校验——在线用户不会因为一次升级被迫全部重登。
    #[serde(default)]
    pub sid: String,
    /// 签发者。`serde(default)` 只为兼容改造前已签发、尚未过期的令牌：
    /// 缺失时按本服务默认值补齐，行为与旧版一致而不是让在线用户掉线。
    #[serde(default = "default_iss")]
    pub iss: String,
    /// 受众。同上。
    #[serde(default = "default_aud")]
    pub aud: String,
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
    #[error("token 签发者不被接受")]
    WrongIssuer,
    #[error("token 受众不被接受")]
    WrongAudience,
    #[error("token 头部算法不被接受")]
    BadAlgorithm,
    #[error("token 签发失败（序列化或签名初始化异常）")]
    Encode,
}

#[derive(Serialize, Deserialize)]
struct Header {
    alg: String,
    typ: String,
}

pub fn issue(secret: &[u8], claims: &Claims) -> Result<String, JwtError> {
    let header = Header {
        alg: "HS256".into(),
        typ: "JWT".into(),
    };
    // 签发路径不 panic（三方审查 M2）：序列化/HMAC 初始化失败时把错误交回调用方。
    // 这里的失败在类型上几乎不可能发生，但"几乎不可能"不该由 unwrap 来赌。
    let h = URL_SAFE_NO_PAD.encode(serde_json::to_vec(&header).map_err(|_| JwtError::Encode)?);
    // iss/aud 由本模块单点写入：调用方即使漏填或填错，签出去的令牌也一定是
    // 本服务域内有效的。反过来把这一步交给每个调用点，就会出现"某个签发点忘带
    // iss → 令牌签得出来却一律 verify 失败"这类只在运行时才炸的错。
    let bounded = Claims {
        iss: ISSUER.to_string(),
        aud: AUDIENCE.to_string(),
        ..claims.clone()
    };
    let p = URL_SAFE_NO_PAD.encode(serde_json::to_vec(&bounded).map_err(|_| JwtError::Encode)?);
    let signing_input = format!("{h}.{p}");
    let sig = sign(secret, signing_input.as_bytes())?;
    Ok(format!("{signing_input}.{}", URL_SAFE_NO_PAD.encode(sig)))
}

/// 校验签名、算法、过期时间、token 类型与签发域（iss/aud）；
/// `now` 由调用方注入便于测试。
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

    let expected_sig = sign(secret, format!("{h}.{p}").as_bytes())?;
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
    // 三方审查低危 3：签发者/受众双向绑定。密钥相同但域不同的服务之间不再互认，
    // 令牌被挪到别处使用会在这里被拒，而不是静默通过。
    if claims.iss != ISSUER {
        return Err(JwtError::WrongIssuer);
    }
    if claims.aud != AUDIENCE {
        return Err(JwtError::WrongAudience);
    }
    Ok(claims)
}

fn sign(secret: &[u8], input: &[u8]) -> Result<Vec<u8>, JwtError> {
    let mut mac = HmacSha256::new_from_slice(secret).map_err(|_| JwtError::Encode)?;
    mac.update(input);
    Ok(mac.finalize().into_bytes().to_vec())
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
            // 会话绑定字段：本单测只验证签名往返，不涉及会话存续，留空即"不做会话校验"
            sid: String::new(),
            iss: ISSUER.into(),
            aud: AUDIENCE.into(),
        }
    }

    #[test]
    fn roundtrip_ok() {
        let secret = b"unit-test-secret-key-0123456789abcdef";
        let token = issue(secret, &claims()).expect("签发应成功");
        let got = verify(secret, &token, TOKEN_TYPE_ACCESS, 1_500).unwrap();
        assert_eq!(got.sub, "user-1");
    }

    #[test]
    fn rejects_tampered_payload() {
        let secret = b"unit-test-secret-key-0123456789abcdef";
        let token = issue(secret, &claims()).expect("签发应成功");
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
        let token = issue(secret, &claims()).expect("签发应成功");
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

    /// 三方审查低危 3：iss/aud 必须双向绑定。
    /// ① `issue` 是单点写入——调用方给了外部域也会被覆写成本域（防漏填、防填错）；
    /// ② `verify` 强校验——签名合法但域不对的令牌一律拒绝（防跨服务重放）。
    #[test]
    fn issuer_and_audience_are_pinned() {
        let secret = b"unit-test-secret-key-0123456789abcdef";

        // ① 覆写：即便调用方填了外部域，签出来的仍是本域令牌
        let token = issue(
            secret,
            &Claims {
                iss: "other-service".into(),
                aud: "other-app".into(),
                ..claims()
            },
        )
        .expect("签发应成功");
        assert!(
            verify(secret, &token, TOKEN_TYPE_ACCESS, 1_500).is_ok(),
            "issue 必须把 iss/aud 拉回本域，否则调用点一处漏填就会签出不可用令牌"
        );

        // ② 拒绝：手工拼一个签名合法、域为外部的令牌（签名校验会通过，只差 iss/aud）
        let forge = |iss: &str, aud: &str| {
            let c = Claims {
                iss: iss.into(),
                aud: aud.into(),
                ..claims()
            };
            let h = URL_SAFE_NO_PAD.encode(br#"{"alg":"HS256","typ":"JWT"}"#);
            let p = URL_SAFE_NO_PAD.encode(serde_json::to_vec(&c).unwrap());
            let sig = sign(secret, format!("{h}.{p}").as_bytes()).unwrap();
            format!("{h}.{p}.{}", URL_SAFE_NO_PAD.encode(sig))
        };
        assert!(matches!(
            verify(
                secret,
                &forge("other-service", AUDIENCE),
                TOKEN_TYPE_ACCESS,
                1_500
            ),
            Err(JwtError::WrongIssuer)
        ));
        assert!(matches!(
            verify(
                secret,
                &forge(ISSUER, "other-app"),
                TOKEN_TYPE_ACCESS,
                1_500
            ),
            Err(JwtError::WrongAudience)
        ));
    }
}
