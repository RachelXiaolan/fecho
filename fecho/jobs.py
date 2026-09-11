"""云端版的后台任务队列。

出日报要好几分钟（一次交叉验证 + 一次日报 + 一次口播稿，都是等模型回话），
网页请求等不了这么久。所以网页上点「重新生成」、每天到点出日报，都只是往 jobs 表
里插一行；真正干活的是 lu2 上常驻的后台程序（worker），它不断从表里领任务。

用数据库当队列而不是另起一个队列服务：量很小（一人一天一两个任务），
Supabase 已经在那了，少一个要运维的东西。
"""
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from . import db


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def enqueue(author: str, kind: str, date: str, run_after: Optional[str] = None) -> Dict[str, Any]:
    """排一个任务。每天到点的 daily 任务一人一天只有一个，重复排会被忽略。"""
    job_id = str(uuid.uuid4())
    now = _now_iso()
    with db.cursor() as conn:
        if kind == "daily":
            existing = conn.execute(
                "SELECT * FROM jobs WHERE author=? AND kind='daily' AND date=?",
                (author, date)).fetchone()
            if existing:
                return dict(existing)
        # 同一天已经有排着或正在跑的重新生成，就不再叠一个
        if kind == "regenerate":
            existing = conn.execute(
                "SELECT * FROM jobs WHERE author=? AND kind='regenerate' AND date=?"
                " AND status IN ('queued','running')", (author, date)).fetchone()
            if existing:
                return dict(existing)
        conn.execute(
            "INSERT INTO jobs (job_id, author, kind, date, status, attempts, run_after,"
            " created_at) VALUES (?,?,?,?,?,?,?,?)",
            (job_id, author, kind, date, "queued", 0, run_after or now, now))
    return {"job_id": job_id, "author": author, "kind": kind, "date": date,
            "status": "queued"}


def latest(author: str, date: str) -> Optional[Dict[str, Any]]:
    """这个人这一天最近的一个任务，给网页显示「正在生成 / 失败原因」用。"""
    with db.cursor() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE author=? AND date=? ORDER BY created_at DESC LIMIT 1",
            (author, date)).fetchone()
    return dict(row) if row else None
