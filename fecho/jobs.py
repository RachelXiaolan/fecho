"""云端版的后台任务队列。

出日报要好几分钟（一次交叉验证 + 一次日报 + 一次口播稿，都是等模型回话），
网页请求等不了这么久。所以网页上点「重新生成」、每天到点出日报，都只是往 jobs 表
里插一行；真正干活的是 lu2 上常驻的后台程序（worker），它不断从表里领任务。

用数据库当队列而不是另起一个队列服务：量很小（一人一天一两个任务），
Supabase 已经在那了，少一个要运维的东西。
"""
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

from . import db


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# 扫描上传后自动重出：攒一会儿再跑。本机脚本常常分好几批上传（9/23 一个小时里传了 5 次），
# 以前每传一批就从头重写一遍整天的日报，既慢又白烧模型
REFRESH_DELAY = timedelta(minutes=10)


def enqueue(author: str, kind: str, date: str, run_after: Optional[str] = None) -> Dict[str, Any]:
    """排一个任务。每天到点的 daily 任务一人一天只有一个，重复排会被忽略。"""
    job_id = str(uuid.uuid4())
    now = _now_iso()
    with db.cursor() as conn:
        if kind == "refresh":
            # 已经有一个在等的就并进去；正在跑的不算——它开跑之后才传上来的进展它看不到，
            # 以前在跑就直接忽略，晚到的进展可能一直进不了日报
            existing = conn.execute(
                "SELECT * FROM jobs WHERE author=? AND kind='refresh' AND date=? AND status='queued'",
                (author, date)).fetchone()
            if existing:
                return dict(existing)
            run_after = run_after or (datetime.now(timezone.utc) + REFRESH_DELAY).isoformat(timespec="seconds")
        elif kind == "daily":
            existing = conn.execute(
                "SELECT * FROM jobs WHERE author=? AND kind='daily' AND date=?",
                (author, date)).fetchone()
            if existing:
                return dict(existing)
        # 其余几种（regenerate / refresh / voice / learn）：同一天已经有同一种排着或正在跑的，
        # 就不再叠一个——连着改两次日报，不该排两次重出口播稿
        else:
            existing = conn.execute(
                "SELECT * FROM jobs WHERE author=? AND kind=? AND date=?"
                " AND status IN ('queued','running')", (author, kind, date)).fetchone()
            if existing:
                return dict(existing)
        conn.execute(
            "INSERT INTO jobs (job_id, author, kind, date, status, attempts, run_after,"
            " created_at) VALUES (?,?,?,?,?,?,?,?)",
            (job_id, author, kind, date, "queued", 0, run_after or now, now))
    return {"job_id": job_id, "author": author, "kind": kind, "date": date,
            "status": "queued"}


# 出日报的几步，按先后。网页的进度条按这个顺序画格子；文字在网页那边翻译
STAGES = ("sync", "verify", "aliases", "daily", "voice")
# 会改日报正文的任务种类。口播稿重出、学写作偏好这些不算，进度条不管它们
REPORT_KINDS = ("daily", "regenerate", "refresh")
HEARTBEAT_KEY = "worker_heartbeat"


def heartbeat() -> None:
    """后台程序还活着。网页据此区分「在排队」和「后台根本没在跑」。"""
    now = _now_iso()
    with db.cursor() as conn:
        conn.execute("INSERT INTO app_settings (key, value, updated_at) VALUES (?,?,?)"
                     " ON CONFLICT (key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                     (HEARTBEAT_KEY, now, now))


def set_progress(job_id: str, stage: str, detail: Optional[str] = None) -> None:
    """记下这个任务做到哪一步。顺便算一次心跳：长任务跑着的时候后台没空去领新任务。"""
    blob = json.dumps({"stage": stage, "at": _now_iso(), "detail": detail}, ensure_ascii=False)
    with db.cursor() as conn:
        conn.execute("UPDATE jobs SET progress=? WHERE job_id=?", (blob, job_id))
    heartbeat()


def reporter(job_id: str) -> Callable[..., None]:
    """给出日报的各个环节用的回调：写进度失败绝不能把日报本身拖垮。"""
    def report(stage: str, detail: Optional[str] = None) -> None:
        try:
            set_progress(job_id, stage, detail)
        except Exception:                          # noqa: BLE001
            pass
    return report


def status(author: str, date: str) -> Dict[str, Any]:
    """网页进度条要的全部信息：这天最近一个出日报任务、前面排了几个、后台上次露面是什么时候。"""
    marks = ",".join("?" * len(REPORT_KINDS))
    with db.cursor() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE author=? AND date=? AND kind IN (" + marks + ")"
            " ORDER BY created_at DESC LIMIT 1", (author, date) + REPORT_KINDS).fetchone()
        job = dict(row) if row else None
        ahead = running = 0
        if job and job["status"] == "queued":
            ahead = conn.execute(
                "SELECT COUNT(*) n FROM jobs WHERE status='queued' AND run_after<=? AND created_at<?",
                (_now_iso(), job["created_at"])).fetchone()["n"]
        running = conn.execute("SELECT COUNT(*) n FROM jobs WHERE status='running'").fetchone()["n"]
        beat = conn.execute("SELECT value FROM app_settings WHERE key=?", (HEARTBEAT_KEY,)).fetchone()
    if job:
        try:
            job["progress"] = json.loads(job.get("progress") or "null")
        except ValueError:
            job["progress"] = None
    return {"job": job, "queue_ahead": ahead, "running": running,
            "worker_seen_at": beat["value"] if beat else None, "now": _now_iso(),
            "stages": list(STAGES)}


def latest(author: str, date: str) -> Optional[Dict[str, Any]]:
    """这个人这一天最近的一个任务，给网页显示「正在生成 / 失败原因」用。"""
    with db.cursor() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE author=? AND date=? ORDER BY created_at DESC LIMIT 1",
            (author, date)).fetchone()
    return dict(row) if row else None
