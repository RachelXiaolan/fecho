"""大家在网页上提的建议和问题，可以带截图。

- 每个人能提、能看到自己提过的和处理状态；admin 能看全员的、标记处理状态
- 图片只有提的人自己和 admin 能打开
- 图片类型按文件头认，不信浏览器报的 mime：伪装成图片的 SVG / 网页一律不收
- 大小有上限：线上跑在 Vercel，一次请求最多 4.5 MB，base64 还会再胖三分之一
"""
import base64
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import accounts, db, store

MAX_IMAGES = 4
MAX_IMAGE_BYTES = 1_000_000          # 每张，解码后
MAX_TOTAL_BYTES = 3_000_000          # 一条反馈里所有图加起来，base64 后约 4 MB
MAX_BODY_CHARS = 5000
STATUSES = ("new", "seen", "done")
PAGES = {"today", "review", "tasks", "reports", "system", "settings", "feedback", "admin"}

_SIGNATURES = (
    ("image/png", b"\x89PNG\r\n\x1a\n"),
    ("image/jpeg", b"\xff\xd8\xff"),
    ("image/gif", b"GIF8"),
)


def sniff(raw: bytes) -> Optional[str]:
    """按文件头认图片类型。认不出来就不是我们收的图片。"""
    for mime, signature in _SIGNATURES:
        if raw.startswith(signature):
            return mime
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _decode(images: Iterable[Any]) -> List[Tuple[str, bytes]]:
    decoded, total = [], 0
    for img in images:
        data = img.get("data") if isinstance(img, dict) else None
        try:
            raw = base64.b64decode(data or "", validate=True)
        except (ValueError, TypeError):
            raise ValueError("有一张图片的数据坏了，请删掉重新添加")
        if not raw:
            raise ValueError("有一张图片是空的，请删掉重新添加")
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError("有一张图太大了（%.1f MB），每张最多 %.1f MB"
                             % (len(raw) / 1e6, MAX_IMAGE_BYTES / 1e6))
        mime = sniff(raw)
        if not mime:
            raise ValueError("只支持 PNG、JPEG、WebP、GIF 图片")
        total += len(raw)
        decoded.append((mime, raw))
    if total > MAX_TOTAL_BYTES:
        raise ValueError("图片加起来太大了（%.1f MB），最多 %.1f MB，删掉一两张再提交"
                         % (total / 1e6, MAX_TOTAL_BYTES / 1e6))
    return decoded


def submit(author: str, body: str, images: Optional[Iterable[Any]] = None,
           page: Optional[str] = None) -> Dict[str, Any]:
    body = (body or "").strip()
    images = list(images or [])
    if not body and not images:
        raise ValueError("先写点内容或加一张图")
    if len(body) > MAX_BODY_CHARS:
        raise ValueError("内容太长了（%d 字），最多 %d 字" % (len(body), MAX_BODY_CHARS))
    if len(images) > MAX_IMAGES:
        raise ValueError("最多 %d 张图" % MAX_IMAGES)
    decoded = _decode(images)

    feedback_id, now = str(uuid.uuid4()), store.now_iso()
    with db.cursor() as conn:
        conn.execute(
            "INSERT INTO feedback (feedback_id, author, body, page, status, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (feedback_id, author, body, page if page in PAGES else None, "new", now, now))
        for position, (mime, raw) in enumerate(decoded):
            conn.execute(
                "INSERT INTO feedback_images (image_id, feedback_id, position, mime, data, size, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), feedback_id, position, mime,
                 base64.b64encode(raw).decode("ascii"), len(raw), now))
    return get(feedback_id)


def _attach_images(conn: Any, rows: Iterable[Any]) -> List[Dict[str, Any]]:
    out = []
    for row in rows:
        item = dict(row)
        item["images"] = [r["image_id"] for r in conn.execute(
            "SELECT image_id FROM feedback_images WHERE feedback_id=? ORDER BY position",
            (item["feedback_id"],)).fetchall()]
        out.append(item)
    return out


def get(feedback_id: str) -> Optional[Dict[str, Any]]:
    with db.cursor() as conn:
        rows = conn.execute("SELECT * FROM feedback WHERE feedback_id=?", (feedback_id,)).fetchall()
        items = _attach_images(conn, rows)
    return items[0] if items else None


def list_mine(author: str) -> List[Dict[str, Any]]:
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT feedback_id, author, body, page, status, created_at, updated_at FROM feedback"
            " WHERE author=? ORDER BY created_at DESC LIMIT 100", (author,)).fetchall()
        return _attach_images(conn, rows)


def list_all() -> List[Dict[str, Any]]:
    """admin 看全员的反馈。调用方负责确认是 admin。"""
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT f.feedback_id, f.author, f.body, f.page, f.status, f.created_at, f.updated_at,"
            " u.display_name FROM feedback f LEFT JOIN users u ON u.author=f.author"
            " ORDER BY f.created_at DESC LIMIT 300").fetchall()
        return _attach_images(conn, rows)


def image_for(image_id: str, viewer: str) -> Optional[Tuple[str, bytes]]:
    """只给提的人自己和 admin。别人拿到 ID 也打不开——对他们来说这张图不存在。"""
    with db.cursor() as conn:
        row = conn.execute(
            "SELECT i.mime, i.data, f.author FROM feedback_images i"
            " JOIN feedback f ON f.feedback_id=i.feedback_id WHERE i.image_id=?",
            (image_id,)).fetchone()
    if not row:
        return None
    if row["author"] != viewer and not accounts.is_admin(viewer):
        return None
    return row["mime"], base64.b64decode(row["data"])


def set_status(feedback_id: str, status: str, admin: str) -> Dict[str, Any]:
    if not accounts.is_admin(admin):
        raise accounts.AccessDenied("只有 admin 能改反馈的处理状态")
    if status not in STATUSES:
        raise ValueError("状态只能是 %s" % " / ".join(STATUSES))
    with db.cursor() as conn:
        n = conn.execute("UPDATE feedback SET status=?, updated_at=? WHERE feedback_id=?",
                         (status, store.now_iso(), feedback_id)).rowcount
    if not n:
        raise ValueError("没有这条反馈")
    return get(feedback_id)
