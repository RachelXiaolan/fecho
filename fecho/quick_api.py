"""临时快速 API 凭据。

原文 token 只在签发响应里出现，数据库只留哈希；每个作者始终只有一把有效的
快速 API key，重新签发会覆盖旧哈希，让上一把立即失效。
"""
import hashlib
import secrets
from threading import Lock
from typing import Optional

from . import db, store

TOKEN_PREFIX = "fecho_quick_"
TABLE_SQL = """
CREATE TABLE IF NOT EXISTS quick_api_keys (
    author        TEXT PRIMARY KEY,
    token_hash    TEXT NOT NULL,
    created_at    TEXT NOT NULL
)
"""
_table_ready = False
_table_lock = Lock()


def ensure_table() -> None:
    """按需建表，适配 Vercel 冷启动不执行全量 db.init() 的部署方式。"""
    global _table_ready
    if _table_ready:
        return
    with _table_lock:
        if _table_ready:
            return
        with db.cursor() as conn:
            conn.execute(TABLE_SQL)
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_quick_api_token_hash"
                " ON quick_api_keys(token_hash)"
            )
            if db.backend() == "postgres":
                conn.execute("ALTER TABLE quick_api_keys ENABLE ROW LEVEL SECURITY")
        _table_ready = True


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue(author: str) -> str:
    """签发一把新的 key，并让该作者之前的 key 立即失效。"""
    ensure_table()
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
    """把临时 key 解析为作者；原文不会写入数据库。"""
    ensure_table()
    if not raw or len(raw) > 256:
        return None
    with db.cursor() as conn:
        row = conn.execute(
            "SELECT author FROM quick_api_keys WHERE token_hash=?",
            (_hash(raw),),
        ).fetchone()
    return row["author"] if row else None


def from_headers(authorization: Optional[str], api_key: Optional[str]) -> Optional[str]:
    """兼容标准 Bearer 和便于脚本使用的 X-API-Key。"""
    raw = (authorization or "").strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    elif raw:
        raw = ""
    return raw or (api_key or "").strip() or None
