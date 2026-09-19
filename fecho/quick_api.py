"""快速 API 的钥匙。

给大模型用的临时凭据：原文只在签发那一次的响应里出现，数据库只留 sha256。
每个人同时只有一把有效的钥匙，重新签发会覆盖旧哈希，上一把立刻失效。

**这把钥匙是受限的**：只能打 `/api/quick-api/*` 那几个口，碰不到 `/api/*` 的其它接口。
个人 token（`api_tokens`）是全权的，所以要发给第三方大模型的，只能是这一把。
"""
import hashlib
import secrets
from typing import Optional

from . import db, store

TOKEN_PREFIX = "fecho_quick_"


def _hash(raw: str) -> str:
    # 钥匙本身是 32 字节随机数，不是人记的密码，不需要慢哈希加盐
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue(author: str) -> str:
    """签发一把新钥匙，并让这个人之前那把立刻失效。返回的原文不会再出现第二次。"""
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    with db.cursor() as conn:
        conn.execute(
            "INSERT INTO quick_api_keys (author, token_hash, created_at) VALUES (?,?,?)"
            " ON CONFLICT(author) DO UPDATE SET token_hash=excluded.token_hash,"
            " created_at=excluded.created_at",
            (author, _hash(raw), store.now_iso()),
        )
    return raw


def resolve(raw: str) -> Optional[str]:
    """把钥匙换成人。钥匙是谁的，记录就进谁的账号。"""
    if not raw or len(raw) > 256:
        return None
    with db.cursor() as conn:
        row = conn.execute(
            "SELECT author FROM quick_api_keys WHERE token_hash=?", (_hash(raw),)
        ).fetchone()
    return row["author"] if row else None


def from_headers(authorization: Optional[str], api_key: Optional[str]) -> Optional[str]:
    """两种写法都认：标准的 `Authorization: Bearer xxx`，和脚本里更好写的 `X-API-Key: xxx`。"""
    raw = (authorization or "").strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    elif raw:
        raw = ""
    return raw or (api_key or "").strip() or None
