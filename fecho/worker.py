"""跑在 lu2 上的后台程序：出日报的慢活都在这里。

为什么单独一个程序：出一份日报要好几分钟（先交叉验证归属，再写日报，再写口播稿，
全是等模型回话），网页请求等不了这么久。所以网页只往 jobs 表里排队，真正干活的是
这个常驻程序。

它做两件事，每 30 秒一轮：

1. **到点排队**。每个人自己设了几点出日报，到点就给他排一个当天的任务。
2. **领任务去做**。同步 Mobius issue → 交叉验证归属 → 出日报和口播稿。

失败补跑一次，间隔 30 分钟；再失败就记下原因，由「该扫了吗」那条通道带给本机弹通知。
同时跑几个人：慢在等模型回话，不占 CPU，所以并发几个就能把一屋子人的日报在几十分钟
内跑完，真正的上限是 LiteLLM 网关一次能接几个请求。

LiteLLM 的 key 只在这台机器上。
"""
import os
import signal
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from . import accounts, clock, config, db, jobs, store

POLL_SECONDS = int(os.getenv("FECHO_WORKER_POLL", "30"))
CONCURRENCY = int(os.getenv("FECHO_WORKER_CONCURRENCY", "3"))
MAX_ATTEMPTS = 2                      # 一次正常 + 一次补跑
RETRY_AFTER = timedelta(minutes=30)
_stop = threading.Event()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg: str) -> None:
    print("[%s] %s" % (_now_iso(), msg), flush=True)


# ---------- 到点排队 ----------

def enqueue_due(now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """给到点的人排当天的日报任务。

    每人一天一个：jobs 表上有唯一索引，重复排会被忽略，所以这里不用自己去重。
    """
    local = clock.now(now)
    today = local.strftime("%Y-%m-%d")
    minutes = local.hour * 60 + local.minute
    out = []
    with db.cursor() as conn:
        users = conn.execute("SELECT author, daily_time FROM users").fetchall()
    for u in users:
        hh, mm = map(int, (u["daily_time"] or "21:00").split(":"))
        if minutes < hh * 60 + mm:
            continue
        job = jobs.enqueue(u["author"], "daily", today)
        if job.get("status") == "queued" and job.get("job_id"):
            out.append(job)
    return out


# ---------- 领任务 ----------

def claim(limit: int = CONCURRENCY) -> List[Dict[str, Any]]:
    """领几个到点的任务。

    先查后改、并用 status='queued' 作为条件，改到了才算领到——两个 worker 同时
    抢同一个任务时，只有一个的 rowcount 是 1。
    """
    now = _now_iso()
    claimed = []
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status='queued' AND run_after<=?"
            " ORDER BY run_after LIMIT ?", (now, limit * 4)).fetchall()
        for row in rows:
            if len(claimed) >= limit:
                break
            n = conn.execute(
                "UPDATE jobs SET status='running', attempts=attempts+1, started_at=?"
                " WHERE job_id=? AND status='queued'", (now, row["job_id"])).rowcount
            if n == 1:
                claimed.append(dict(row))
    return claimed


def finish(job: Dict[str, Any], error: Optional[str] = None) -> None:
    """做完一个任务。失败且还有补跑机会的，往后排 30 分钟重来。"""
    now = _now_iso()
    attempts = int(job.get("attempts") or 0) + 1
    with db.cursor() as conn:
        if error and attempts < MAX_ATTEMPTS:
            conn.execute(
                "UPDATE jobs SET status='queued', run_after=?, error=? WHERE job_id=?",
                ((datetime.now(timezone.utc) + RETRY_AFTER).isoformat(timespec="seconds"),
                 error[:500], job["job_id"]))
            log("任务 %s 失败，30 分钟后补跑：%s" % (job["job_id"][:8], error[:120]))
            return
        conn.execute(
            "UPDATE jobs SET status=?, error=?, finished_at=? WHERE job_id=?",
            ("failed" if error else "succeeded", error[:500] if error else None,
             now, job["job_id"]))
    if error:
        log("任务 %s 最终失败：%s" % (job["job_id"][:8], error[:160]))


# ---------- 干活 ----------

def run_job(job: Dict[str, Any]) -> None:
    author, date = job["author"], job["date"]
    label = "%s %s(%s)" % (author, date, job["kind"])
    try:
        sync_issues(author)                       # 同步失败不挡着出日报
        from . import service

        r = service.end_of_day(date, author=author, force=(job["kind"] == "regenerate"))
        log("%s → %s（%d 个任务 / %d 条进展）" % (
            label, r.get("status"), r.get("task_count", 0), r.get("update_count", 0)))
        for w in r.get("warnings") or []:
            log("  ! %s" % w)
        finish(job)
    except Exception as exc:                      # noqa: BLE001 一个人失败不能带倒整轮
        log("%s 出错：%s" % (label, traceback.format_exc(limit=3)))
        finish(job, "%s: %s" % (exc.__class__.__name__, exc))


def sync_issues(author: str) -> None:
    """用服务器替这个人存的 Mobius 授权同步 issue。

    同步不上不该挡着出日报——没有最新的 issue 列表，顶多是归属判断少一点依据。
    """
    from . import mobius, mobius_login

    try:
        mobius.sync(author, assignee=author, token=mobius_login.access_token(author))
    except Exception as exc:                      # noqa: BLE001
        log("  ! %s 的 Mobius 同步失败（不影响出日报）：%s" % (author, str(exc)[:120]))


# ---------- 主循环 ----------

def tick(pool: ThreadPoolExecutor) -> int:
    enqueue_due()
    batch = claim()
    if batch:
        log("领到 %d 个任务" % len(batch))
        list(pool.map(run_job, batch))
    return len(batch)


def serve() -> None:
    if not config.CLOUD or db.backend() != "postgres":
        raise SystemExit("后台程序是云端版专用：要设 FECHO_CLOUD=1 和 FECHO_DATABASE_URL")
    if not config.llm_configured():
        raise SystemExit("没配 LLM，出不了日报：要设 FECHO_LLM_BASE_URL / KEY / MODEL")

    db.init()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: _stop.set())
    log("启动：库=%s 模型=%s 并发=%d 轮询=%ds"
        % (db.location(), config.LLM_MODEL, CONCURRENCY, POLL_SECONDS))

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        while not _stop.is_set():
            try:
                if tick(pool) == 0:
                    _stop.wait(POLL_SECONDS)
            except Exception:                     # noqa: BLE001 循环不能被单次异常打断
                log("这一轮出错：%s" % traceback.format_exc(limit=3))
                _stop.wait(POLL_SECONDS)
    log("已停止")


def run_once() -> Dict[str, Any]:
    """跑一轮就退出。装完之后拿它做一次实测，不用等定时。"""
    db.init()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        n = tick(pool)
    with db.cursor() as conn:
        pending = conn.execute("SELECT COUNT(*) n FROM jobs WHERE status='queued'").fetchone()["n"]
    return {"processed": n, "queued_left": pending}
