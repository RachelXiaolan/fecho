"""日报里贴的图。

- 改日报、手写日报时粘贴或拖进来，先传上来拿到地址，再以 `![图片|480](地址)` 写进日报
- 只有本人和 admin 能打开：admin 用「以某人身份查看」看日报时也要看得到图
- 类型按文件头认（和反馈截图同一套），伪装成图片的 SVG / 网页不收
- 一次只传一张：线上跑在 Vercel，一次请求最多 4.5 MB，base64 还会再胖三分之一
"""
import base64
import uuid
from typing import Any, Dict, Optional, Tuple

from . import accounts, db, store
from .feedback import sniff

MAX_IMAGE_BYTES = 2_000_000          # 解码后。网页会先把大图压到 1 MB 以内


def save(author: str, data: Any) -> Dict[str, Any]:
    try:
        raw = base64.b64decode(data or "", validate=True)
    except (ValueError, TypeError):
        raise ValueError("图片数据坏了，请重新粘贴")
    if not raw:
        raise ValueError("图片是空的，请重新粘贴")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("图太大了（%.1f MB），最多 %.1f MB"
                         % (len(raw) / 1e6, MAX_IMAGE_BYTES / 1e6))
    mime = sniff(raw)
    if not mime:
        raise ValueError("只支持 PNG、JPEG、WebP、GIF 图片")
    image_id = str(uuid.uuid4())
    with db.cursor() as conn:
        conn.execute(
            "INSERT INTO report_images (image_id, author, mime, data, size, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (image_id, author, mime, base64.b64encode(raw).decode("ascii"), len(raw),
             store.now_iso()))
    return {"image_id": image_id, "url": "/api/reports/images/%s" % image_id}


def image_for(image_id: str, viewer: str) -> Optional[Tuple[str, bytes]]:
    """只给本人和 admin。别人拿到地址也打不开——对他们来说这张图不存在。"""
    with db.cursor() as conn:
        row = conn.execute("SELECT mime, data, author FROM report_images WHERE image_id=?",
                           (image_id,)).fetchone()
    if not row:
        return None
    if row["author"] != viewer and not accounts.is_admin(viewer):
        return None
    return row["mime"], base64.b64decode(row["data"])
