"""云端版的身份：谁在用、凭什么信他、他能看什么。

三种凭证，都只在库里存哈希——库被整个读走，也拿不到一个能用的：

- **agent token**：粘回给 agent 的那串，agent 每次调 MCP 都带着。
- **网页会话**：浏览器里的 cookie，打开 Dashboard 用。
- **Mobius 授权**：服务器替每个人去 Mobius 同步 issue。这个必须能还原（要拿去
  调 Mobius），所以不是哈希而是加密存，密钥只在服务器环境变量里。

有效期是**滑动**的：每用一次顺延 7 天，连续 7 天没用才失效。天天在用的人
永远不用重新 onboarding；请假一周多回来要重新登录一次。
"""
import base64
import hashlib
import hmac
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from . import config, db

TOKEN_TTL = timedelta(days=7)
SESSION_TTL = timedelta(days=7)
# 每个请求都顺延一次就是每个请求都写一次库。一小时顺延一次，效果一样。
BUMP_EVERY = timedelta(hours=1)
TOKEN_PREFIX = "fecho_"


class AccessDenied(PermissionError):
    """登录了但没资格（域名不对、不是真人账号、不是 admin）。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------- 用户 ----------

def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def email_allowed(email: str) -> bool:
    email = normalize_email(email)
    return bool(email) and email.endswith("@" + config.ALLOWED_EMAIL_DOMAIN)


def upsert_user(email: str, display_name: str = "",
                mobius_user_id: Optional[str] = None) -> Dict[str, Any]:
    """登录成功时调。第一次来建号；配置里列的初始 admin 直接给 admin。"""
    author = normalize_email(email)
    if not email_allowed(author):
        raise AccessDenied("只允许 @%s 的邮箱登录" % config.ALLOWED_EMAIL_DOMAIN)
    now = _iso(_now())
    bootstrap = 1 if author in config.BOOTSTRAP_ADMINS else 0
    with db.cursor() as conn:
        conn.execute(
            "INSERT INTO users (author, display_name, mobius_user_id, is_admin,"
            " created_at, last_seen_at) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT (author) DO UPDATE SET display_name=excluded.display_name,"
            " mobius_user_id=excluded.mobius_user_id, last_seen_at=excluded.last_seen_at",
            (author, display_name or author.split("@")[0], mobius_user_id, bootstrap, now, now))
        if bootstrap:
            # 已经存在的号后来被加进初始名单，也要补上 admin
            conn.execute("UPDATE users SET is_admin=1 WHERE author=?", (author,))
    return get_user(author) or {}


def get_user(author: str) -> Optional[Dict[str, Any]]:
    with db.cursor() as conn:
        row = conn.execute("SELECT * FROM users WHERE author=?", (author,)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["is_admin"] = bool(out.get("is_admin"))
    return out


def is_admin(author: str) -> bool:
    user = get_user(author)
    return bool(user and user["is_admin"])


def list_users() -> List[Dict[str, Any]]:
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT u.author, u.display_name, u.is_admin, u.daily_time, u.last_seen_at,"
            " (SELECT COUNT(*) FROM updates p WHERE p.author=u.author"
            "  AND p.status='active') AS update_count,"
            " (SELECT MAX(p.date) FROM updates p WHERE p.author=u.author"
            "  AND p.status='active') AS last_active_date"
            " FROM users u ORDER BY u.display_name").fetchall()
    return [{**dict(r), "is_admin": bool(r["is_admin"])} for r in rows]


SCAN_LEAD_MINUTES = 15   # 本机扫描比出日报早这么多


def set_daily_time(author: str, daily_time: str) -> str:
    """出日报的时间。本机扫描自动提前 15 分钟，所以最早只能设 00:15——
    再早扫描就跑到前一天去了，扫的是昨天的对话、交的是今天的日报。"""
    from . import clock
    daily_time = clock.validate_daily_time(daily_time)
    hour, minute = map(int, daily_time.split(":"))
    if hour * 60 + minute < SCAN_LEAD_MINUTES:
        raise ValueError("最早只能设 00:%02d：扫描要提前 %d 分钟跑" % (SCAN_LEAD_MINUTES,
                                                                  SCAN_LEAD_MINUTES))
    with db.cursor() as conn:
        conn.execute("UPDATE users SET daily_time=? WHERE author=?", (daily_time, author))
    return daily_time


# ---------- agent token ----------

def issue_token(author: str, label: str = "agent") -> str:
    """发一个新 token，只在这一刻能看到原文。"""
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    now = _now()
    with db.cursor() as conn:
        conn.execute(
            "INSERT INTO api_tokens (token_hash, author, label, created_at, last_used_at,"
            " expires_at) VALUES (?,?,?,?,?,?)",
            (_hash(raw), author, label, _iso(now), _iso(now), _iso(now + TOKEN_TTL)))
    return raw


def resolve_token(raw: str) -> Optional[str]:
    """token 有效就返回主人，并顺延有效期；过期、撤销、不存在都返回 None。"""
    return _resolve("api_tokens", "token_hash", raw, TOKEN_TTL, has_revoke=True)


def revoke_tokens(author: str) -> int:
    with db.cursor() as conn:
        return conn.execute(
            "UPDATE api_tokens SET revoked_at=? WHERE author=? AND revoked_at IS NULL",
            (_iso(_now()), author)).rowcount


# ---------- 网页会话 ----------

def create_session(author: str) -> str:
    raw = secrets.token_urlsafe(32)
    now = _now()
    with db.cursor() as conn:
        conn.execute(
            "INSERT INTO web_sessions (session_hash, author, created_at, expires_at)"
            " VALUES (?,?,?,?)", (_hash(raw), author, _iso(now), _iso(now + SESSION_TTL)))
    return raw


def resolve_session(raw: str) -> Optional[str]:
    return _resolve("web_sessions", "session_hash", raw, SESSION_TTL, has_revoke=False)


def end_session(raw: str) -> None:
    with db.cursor() as conn:
        conn.execute("DELETE FROM web_sessions WHERE session_hash=?", (_hash(raw),))


def _resolve(table: str, key_col: str, raw: str, ttl: timedelta,
             has_revoke: bool) -> Optional[str]:
    if not raw:
        return None
    now = _now()
    with db.cursor() as conn:
        row = conn.execute("SELECT * FROM %s WHERE %s=?" % (table, key_col),
                           (_hash(raw),)).fetchone()
        if not row:
            return None
        if has_revoke and row["revoked_at"]:
            return None
        expires = _parse(row["expires_at"])
        if expires <= now:
            return None
        # 滑动有效期：剩余时间少于 TTL - 1 小时才顺延，避免每个请求都写库
        if expires - now < ttl - BUMP_EVERY:
            sets, args = ["expires_at=?"], [_iso(now + ttl)]
            if table == "api_tokens":
                sets.append("last_used_at=?")
                args.append(_iso(now))
            conn.execute("UPDATE %s SET %s WHERE %s=?" % (table, ",".join(sets), key_col),
                         (*args, _hash(raw)))
    return row["author"]


# ---------- admin 申请 ----------

def request_admin(author: str) -> Dict[str, Any]:
    """点「申请 admin」。已经是 admin 或已有待审批的，不重复建。"""
    if is_admin(author):
        return {"status": "already-admin"}
    with db.cursor() as conn:
        row = conn.execute("SELECT * FROM admin_requests WHERE author=? AND status='pending'",
                           (author,)).fetchone()
        if row:
            return dict(row)
        rid = str(uuid.uuid4())
        conn.execute("INSERT INTO admin_requests (request_id, author, status, created_at)"
                     " VALUES (?,?,?,?)", (rid, author, "pending", _iso(_now())))
    return {"request_id": rid, "author": author, "status": "pending"}


def pending_request(author: str) -> Optional[Dict[str, Any]]:
    with db.cursor() as conn:
        row = conn.execute("SELECT * FROM admin_requests WHERE author=? AND status='pending'",
                           (author,)).fetchone()
    return dict(row) if row else None


def admin_requests(status: str = "pending") -> List[Dict[str, Any]]:
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT r.*, u.display_name FROM admin_requests r"
            " LEFT JOIN users u ON u.author=r.author"
            " WHERE r.status=? ORDER BY r.created_at", (status,)).fetchall()
    return [dict(r) for r in rows]


def decide_admin_request(request_id: str, approver: str, approve: bool) -> Dict[str, Any]:
    if not is_admin(approver):
        raise AccessDenied("只有 admin 能审批")
    with db.cursor() as conn:
        row = conn.execute("SELECT * FROM admin_requests WHERE request_id=?",
                           (request_id,)).fetchone()
        if not row:
            raise ValueError("申请不存在: %s" % request_id)
        if row["status"] != "pending":
            raise ValueError("这个申请已经处理过了（%s）" % row["status"])
        status = "approved" if approve else "denied"
        conn.execute("UPDATE admin_requests SET status=?, decided_by=?, decided_at=?"
                     " WHERE request_id=?", (status, approver, _iso(_now()), request_id))
        if approve:
            conn.execute("UPDATE users SET is_admin=1 WHERE author=?", (row["author"],))
    return {"request_id": request_id, "author": row["author"], "status": status}


# ---------- 加密与签名 ----------

def _require_secret() -> bytes:
    if len(config.SECRET_KEY) < 32:
        raise RuntimeError("FECHO_SECRET_KEY 没设或太短（至少 32 个字符）")
    return config.SECRET_KEY.encode("utf-8")


def _fernet() -> Any:
    from cryptography.fernet import Fernet
    key = base64.urlsafe_b64encode(hashlib.sha256(b"fecho-enc:" + _require_secret()).digest())
    return Fernet(key)


def encrypt(value: Optional[str]) -> Optional[str]:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii") if value else None


def decrypt(value: Optional[str]) -> Optional[str]:
    return _fernet().decrypt(value.encode("ascii")).decode("utf-8") if value else None


def sign(payload: Dict[str, Any], ttl_seconds: int = 600) -> str:
    """带过期时间的签名串。登录跳转去 Mobius 再回来的这段路上，用它记住
    state 和 PKCE 验证码——服务器可能跑在好几个实例上，放内存里会丢。"""
    body = dict(payload, exp=int(_now().timestamp()) + ttl_seconds)
    data = base64.urlsafe_b64encode(json.dumps(body, sort_keys=True).encode()).decode()
    mac = hmac.new(_require_secret(), data.encode(), hashlib.sha256).hexdigest()
    return data + "." + mac


def unsign(token: str) -> Dict[str, Any]:
    try:
        data, mac = token.rsplit(".", 1)
    except (AttributeError, ValueError):
        raise AccessDenied("登录状态无效，请重新登录")
    good = hmac.new(_require_secret(), data.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, good):
        raise AccessDenied("登录状态被篡改，请重新登录")
    body = json.loads(base64.urlsafe_b64decode(data.encode()))
    if int(body.get("exp", 0)) < int(_now().timestamp()):
        raise AccessDenied("登录超时，请重新登录")
    return body


# ---------- 服务器级小配置 ----------

def get_setting(key: str) -> Optional[str]:
    with db.cursor() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with db.cursor() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?,?,?)"
            " ON CONFLICT (key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, _iso(_now())))
