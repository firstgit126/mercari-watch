"""纯 Python 的 ES256(ECDSA P-256) 签名与 DPoP 令牌生成。

Mercari 的 v2 接口要求每个请求带一个 DPoP 头（一个用 ES256 签名的 JWT）。
标准库没有椭圆曲线签名，所以这里用整数运算直接实现 P-256 曲线，
k 的生成遵循 RFC 6979（确定性 k），避免每次都依赖随机源。

没有任何第三方依赖，Python 3.8+ 可用。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid

# ---- P-256 曲线参数 (FIPS 186-4 / secp256r1) -------------------------------
P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
A = P - 3
B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5
G = (GX, GY)

Point = "tuple[int, int] | None"


# ---- 椭圆曲线运算 ----------------------------------------------------------
def _inv_p(x: int) -> int:
    """模曲线素数 P 的逆元（点运算用）。"""
    return pow(x, -1, P)


def _inv_n(x: int) -> int:
    """模群阶 N 的逆元（签名运算用）。"""
    return pow(x, -1, N)


def point_add(p1: Point, p2: Point) -> Point:
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2:
        if (y1 + y2) % P == 0:
            return None
        lam = (3 * x1 * x1 + A) * _inv_p(2 * y1) % P
    else:
        lam = (y2 - y1) * _inv_p(x2 - x1) % P
    x3 = (lam * lam - x1 - x2) % P
    y3 = (lam * (x1 - x3) - y1) % P
    return (x3, y3)


def point_mul(k: int, pt: Point) -> Point:
    result: Point = None
    addend = pt
    while k:
        if k & 1:
            result = point_add(result, addend)
        addend = point_add(addend, addend)
        k >>= 1
    return result


# ---- RFC 6979 确定性 k -----------------------------------------------------
def _rfc6979_k(priv: int, digest: bytes) -> int:
    x = priv.to_bytes(32, "big")
    v = b"\x01" * 32
    k = b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + x + digest, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + x + digest, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        candidate = int.from_bytes(v, "big")
        if 1 <= candidate < N:
            return candidate
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


# ---- DER 编码 / 解码 -------------------------------------------------------
def _der_uint(value: int) -> bytes:
    raw = value.to_bytes((value.bit_length() + 7) // 8 or 1, "big")
    if raw[0] & 0x80:
        raw = b"\x00" + raw
    return b"\x02" + bytes([len(raw)]) + raw


def _der_decode_signature(der: bytes) -> tuple[int, int]:
    if not der or der[0] != 0x30:
        raise ValueError("签名不是 DER SEQUENCE")
    idx = 2 if der[1] < 0x80 else 2 + (der[1] & 0x7F)
    out = []
    for _ in range(2):
        if der[idx] != 0x02:
            raise ValueError("签名不是 DER INTEGER")
        length = der[idx + 1]
        out.append(int.from_bytes(der[idx + 2 : idx + 2 + length], "big"))
        idx += 2 + length
    return out[0], out[1]


# ---- 签名 / 验签 -----------------------------------------------------------
def sign_es256(priv: int, message: bytes) -> bytes:
    """对 message 做 SHA-256 后签名，返回 DER 编码的 (r, s)。"""
    digest = hashlib.sha256(message).digest()
    e = int.from_bytes(digest, "big")
    k = _rfc6979_k(priv, digest)
    point = point_mul(k, G)
    assert point is not None
    r = point[0] % N
    if r == 0:
        raise ValueError("签名失败：r 为 0")
    s = (_inv_n(k) * (e + r * priv)) % N
    if s == 0:
        raise ValueError("签名失败：s 为 0")
    if s > N // 2:  # low-S 规范化
        s = N - s
    body = _der_uint(r) + _der_uint(s)
    return b"\x30" + bytes([len(body)]) + body


def verify_es256(pub: Point, message: bytes, signature: bytes) -> bool:
    if pub is None:
        return False
    r, s = _der_decode_signature(signature)
    if not (1 <= r < N and 1 <= s < N):
        return False
    e = int.from_bytes(hashlib.sha256(message).digest(), "big")
    w = _inv_n(s)
    u1 = e * w % N
    u2 = r * w % N
    point = point_add(point_mul(u1, G), point_mul(u2, pub))
    if point is None:
        return False
    return point[0] % N == r


# ---- 密钥与 JWK ------------------------------------------------------------
def new_private_key() -> int:
    while True:
        candidate = secrets.randbelow(N - 1) + 1
        if candidate:
            return candidate


def public_key(priv: int) -> tuple[int, int]:
    point = point_mul(priv, G)
    assert point is not None
    return point


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_int(value: int) -> str:
    return _b64u(value.to_bytes(32, "big"))


def jwk_from_private(priv: int) -> dict:
    x, y = public_key(priv)
    return {"kty": "EC", "crv": "P-256", "x": _b64u_int(x), "y": _b64u_int(y)}


def export_key(priv: int) -> dict:
    """导出为可持久化的 JSON（十进制字符串，避免精度问题）。"""
    return {"d": str(priv), **jwk_from_private(priv)}


def load_key(data: dict) -> int:
    return int(data["d"])


# ---- DPoP ------------------------------------------------------------------
def _jwt_segment(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return _b64u(raw)


def make_dpop(priv: int, url: str, method: str = "POST", nonce: str | None = None) -> str:
    """生成 DPoP 证明 JWT。url 必须与真实请求 URL 完全一致。"""
    header = {"typ": "dpop+jwt", "alg": "ES256", "jwk": jwk_from_private(priv)}
    claims = {
        "iat": int(time.time()),
        "jti": str(uuid.uuid4()),
        "htu": url,
        "htm": method.upper(),
    }
    if nonce:
        claims["nonce"] = nonce
    signing_input = f"{_jwt_segment(header)}.{_jwt_segment(claims)}"
    signature = sign_es256(priv, signing_input.encode("ascii"))
    return f"{signing_input}.{_b64u(signature)}"


# ---- 自检 ------------------------------------------------------------------
def self_test() -> tuple[bool, str]:
    """签名+验签往返测试，用于 doctor。"""
    try:
        priv = new_private_key()
        pub = public_key(priv)
        message = b"mercari-watch self test"
        signature = sign_es256(priv, message)
        if not verify_es256(pub, message, signature):
            return False, "验签失败"
        if verify_es256(pub, message + b"x", signature):
            return False, "错误消息竟然验签通过"
        x, y = pub
        if (y * y - (x * x * x + A * x + B)) % P != 0:
            return False, "公钥不在曲线上"
        token = make_dpop(priv, "https://api.mercari.jp/v2/entities:search")
        if token.count(".") != 2:
            return False, "DPoP 令牌格式错误"
        return True, "ES256/DPoP 正常"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
