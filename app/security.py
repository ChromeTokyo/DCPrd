"""Telegram 登录校验、分享码、随机码、签名 Cookie。"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time

from itsdangerous import BadSignature, URLSafeTimedSerializer

SHARE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
SHARE_LEN = 12
TELEGRAM_AUTH_MAX_AGE = 86400


def verify_telegram_auth(bot_token: str, data: dict[str, str], now: float | None = None) -> tuple[bool, str]:
    """按 Telegram Login Widget 规范校验 hash 与 auth_date。返回 (是否通过, 失败原因)。"""
    if not bot_token:
        return False, "服务器未配置 Bot Token"
    received = data.get("hash", "")
    if not received or "id" not in data or "auth_date" not in data:
        return False, "缺少必要字段"
    pairs = [f"{k}={v}" for k, v in sorted(data.items()) if k != "hash"]
    check_string = "\n".join(pairs)
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    expected = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return False, "签名校验失败"
    try:
        auth_date = int(data["auth_date"])
    except ValueError:
        return False, "auth_date 非法"
    now = time.time() if now is None else now
    if now - auth_date >= TELEGRAM_AUTH_MAX_AGE:
        return False, "登录信息已过期，请重试"
    return True, ""


def sign_telegram_auth(bot_token: str, data: dict[str, str]) -> str:
    """测试/工具用：为一组字段计算合法 hash（与 Widget 相同算法）。"""
    pairs = [f"{k}={v}" for k, v in sorted(data.items()) if k != "hash"]
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    return hmac.new(secret_key, "\n".join(pairs).encode(), hashlib.sha256).hexdigest()


def new_share_code(conn: sqlite3.Connection) -> str:
    """12 位分享码，在 documents 与 requirements 两张表中全局唯一。"""
    while True:
        code = "".join(secrets.choice(SHARE_ALPHABET) for _ in range(SHARE_LEN))
        used = conn.execute(
            "SELECT 1 FROM documents WHERE share_code = ? UNION ALL SELECT 1 FROM requirements WHERE share_code = ?",
            (code, code),
        ).fetchone()
        if not used:
            return code


def new_invite_code() -> str:
    return secrets.token_urlsafe(32)


def new_session_id() -> str:
    return secrets.token_urlsafe(32)


def new_csrf() -> str:
    return secrets.token_urlsafe(24)


class Signer:
    """对 Cookie 值做签名（session id / 邀请码 / flash）。"""

    def __init__(self, secret_key: str):
        self._s = URLSafeTimedSerializer(secret_key)

    def dumps(self, value, salt: str) -> str:
        return self._s.dumps(value, salt=salt)

    def loads(self, token: str | None, salt: str, max_age: int | None = None):
        if not token:
            return None
        try:
            return self._s.loads(token, salt=salt, max_age=max_age)
        except BadSignature:
            return None
