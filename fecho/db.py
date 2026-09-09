"""SQLite 存元数据。

核心是**任务**，不是记录。一件事一个 task，进展一条条挂在它下面：
一个任务可以横跨很多天、很多个对话、很多个 agent。
"""
import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from . import config

SCHEMA = """
-- 任务：一件要做的事。可能对应 Mobius 上的 issue，也可能不对应。
CREATE TABLE IF NOT EXISTS tasks (
    task_id       TEXT PRIMARY KEY,
    author        TEXT NOT NULL,
    source        TEXT NOT NULL,        -- mobius / freeform
    issue_key     TEXT,                 -- AI-2541；freeform 为 null
    title         TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open',   -- open / done（本地状态，不回写 Mobius）
    first_seen    TEXT NOT NULL,        -- 第一次有进展的日期
    last_update   TEXT NOT NULL,        -- 最近一次进展时间
    meta          TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_tasks_author ON tasks(author, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_issue ON tasks(author, issue_key)
    WHERE issue_key IS NOT NULL;

-- 进展：某个任务在某个时刻推进了什么。同一任务下的多条进展全部保留。
CREATE TABLE IF NOT EXISTS updates (
    update_id     TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL REFERENCES tasks(task_id),
    author        TEXT NOT NULL,
    date          TEXT NOT NULL,        -- YYYY-MM-DD
    content_md    TEXT NOT NULL,        -- 做成了什么、进展到哪
    source_agent  TEXT NOT NULL DEFAULT 'manual',
    ingestion_method TEXT NOT NULL DEFAULT 'direct', -- direct / transcript-scan
    session_id    TEXT,                 -- 哪个对话（审计用；任务与对话是多对多）
    match_method  TEXT NOT NULL,        -- explicit / mobius-auto / task-continue / new-task
    match_score   REAL,
    assignment_source TEXT NOT NULL DEFAULT 'system', -- agent / project / session / model / human
    assignment_locked INTEGER NOT NULL DEFAULT 0,     -- 人工确认后模型不得覆盖
    revision      INTEGER NOT NULL DEFAULT 1,
    source_event_key TEXT,              -- transcript chunk/item 的稳定幂等键
    completion_status TEXT NOT NULL DEFAULT 'unknown', -- done / wip / blocked / unknown
    content_kind  TEXT NOT NULL DEFAULT 'progress',    -- progress / pitfall / decision
    pto_status    TEXT,
    created_at    TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',  -- active / duplicate-ignored
    meta          TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_updates_task ON updates(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_updates_author_date ON updates(author, date);

-- 归属和正文纠错审计。update 本身原地修订，避免“重记一次”制造两条互相冲突的进展。
CREATE TABLE IF NOT EXISTS assignment_events (
    event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    update_id      TEXT NOT NULL REFERENCES updates(update_id),
    author         TEXT NOT NULL,
    actor          TEXT NOT NULL,
    from_task_id   TEXT,
    to_task_id     TEXT,
    from_issue_key TEXT,
    to_issue_key   TEXT,
    from_content_md TEXT NOT NULL,
    to_content_md   TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assignment_events_update
    ON assignment_events(update_id, created_at);

-- Mobius issue 本地缓存：配对在本地做，不每次去问 Mobius
CREATE TABLE IF NOT EXISTS mobius_issues (
    issue_key   TEXT NOT NULL,
    author      TEXT NOT NULL,
    title       TEXT NOT NULL,
    state       TEXT,
    url         TEXT,
    updated_at  TEXT,
    synced_at   TEXT NOT NULL,
    raw         TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (issue_key, author)
);

-- 扫描水位线：记住每个会话已经处理到哪个时刻。
-- 没有它，能开好几周的会话每天都会被重新总结一遍，库里堆重复。
CREATE TABLE IF NOT EXISTS scan_marks (
    session_id    TEXT PRIMARY KEY,
    last_ts       TEXT NOT NULL,     -- 已处理到的最后一条消息时间戳（ISO，UTC）
    last_scan_at  TEXT NOT NULL,
    entries       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS scan_runs (
    run_id          TEXT PRIMARY KEY,
    author          TEXT NOT NULL,
    producer_agent  TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    project         TEXT NOT NULL,
    date            TEXT NOT NULL,
    group_start_ts  TEXT NOT NULL,
    group_end_ts    TEXT NOT NULL,
    status          TEXT NOT NULL,       -- running / succeeded / failed
    chunks          INTEGER NOT NULL DEFAULT 0,
    entries         INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_scan_runs_session ON scan_runs(session_id, started_at);

CREATE TABLE IF NOT EXISTS assignment_verifications (
    author       TEXT NOT NULL,
    date         TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    model        TEXT,
    prompt_version TEXT NOT NULL,
    verified_at  TEXT NOT NULL,
    PRIMARY KEY (author, date)
);

CREATE TABLE IF NOT EXISTS reports (
    report_id     TEXT PRIMARY KEY,
    author        TEXT NOT NULL,
    date          TEXT NOT NULL,
    kind          TEXT NOT NULL,        -- daily / voice
    content_md    TEXT NOT NULL,
    fingerprint   TEXT NOT NULL,
    generator     TEXT NOT NULL,
    model         TEXT,
    entry_count   INTEGER NOT NULL DEFAULT 0,
    char_count    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    UNIQUE(author, date, kind)
);

CREATE TABLE IF NOT EXISTS report_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    author      TEXT NOT NULL,
    date        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    content_md  TEXT NOT NULL,
    generator   TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        # 老库增量升级。CREATE TABLE IF NOT EXISTS 不会替已有表补列，必须显式迁移。
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(updates)").fetchall()}
        if "assignment_source" not in columns:
            conn.execute("ALTER TABLE updates ADD COLUMN assignment_source TEXT")
            conn.execute("UPDATE updates SET assignment_source=match_method"
                         " WHERE assignment_source IS NULL")
        if "assignment_locked" not in columns:
            conn.execute("ALTER TABLE updates ADD COLUMN assignment_locked INTEGER NOT NULL DEFAULT 0")
        if "revision" not in columns:
            conn.execute("ALTER TABLE updates ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
        if "source_event_key" not in columns:
            conn.execute("ALTER TABLE updates ADD COLUMN source_event_key TEXT")
        if "ingestion_method" not in columns:
            conn.execute("ALTER TABLE updates ADD COLUMN ingestion_method TEXT NOT NULL DEFAULT 'direct'")
            conn.execute("UPDATE updates SET ingestion_method='transcript-scan'"
                         " WHERE source_agent='scan'")
        if "completion_status" not in columns:
            conn.execute("ALTER TABLE updates ADD COLUMN completion_status TEXT NOT NULL DEFAULT 'unknown'")
        if "content_kind" not in columns:
            conn.execute("ALTER TABLE updates ADD COLUMN content_kind TEXT NOT NULL DEFAULT 'progress'")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_updates_source_event"
                     " ON updates(source_event_key) WHERE source_event_key IS NOT NULL")


@contextmanager
def cursor() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _row(r: sqlite3.Row) -> Dict[str, Any]:
    d = dict(r)
    if "meta" in d:
        d["meta"] = json.loads(d.get("meta") or "{}")
    return d


def list_updates(
    date: Optional[str] = None,
    author: Optional[str] = None,
    task_id: Optional[str] = None,
    include_ignored: bool = False,
) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM updates WHERE 1=1"
    args: List[Any] = []
    for col, val in (("date", date), ("author", author), ("task_id", task_id)):
        if val:
            sql += " AND %s = ?" % col
            args.append(val)
    if not include_ignored:
        sql += " AND status = 'active'"
    sql += " ORDER BY created_at ASC, rowid ASC"
    with cursor() as conn:
        return [_row(r) for r in conn.execute(sql, args).fetchall()]


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    with cursor() as conn:
        r = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    return _row(r) if r else None


def list_tasks(author: Optional[str] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM tasks WHERE 1=1"
    args: List[Any] = []
    if author:
        sql += " AND author = ?"
        args.append(author)
    if status:
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY last_update DESC"
    with cursor() as conn:
        return [_row(r) for r in conn.execute(sql, args).fetchall()]


def day_tasks(author: str, date: str) -> List[Dict[str, Any]]:
    """当天有进展的任务，每个任务带上它当天的进展列表。"""
    ups = list_updates(date=date, author=author)
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for u in ups:
        grouped.setdefault(u["task_id"], []).append(u)
    out = []
    for tid, items in grouped.items():
        t = get_task(tid)
        if not t:
            continue
        t["updates"] = items
        out.append(t)
    # Mobius 任务排前面（有 ticket 的通常是正事），其次按当天进展条数
    out.sort(key=lambda t: (t["source"] != "mobius", -len(t["updates"])))
    return out


def day_updates(author: str, date: str) -> List[Dict[str, Any]]:
    """当天的进展平铺一列，带上各自现在归到哪个 issue。给交叉验证用。"""
    with cursor() as conn:
        rows = conn.execute(
            "SELECT u.update_id, u.content_md, u.match_method, u.assignment_source,"
            " u.assignment_locked, u.revision, u.task_id, t.issue_key"
            " FROM updates u JOIN tasks t ON t.task_id = u.task_id"
            " WHERE u.author=? AND u.date=? AND u.status='active'"
            " ORDER BY u.created_at", (author, date)).fetchall()
    return [dict(r) for r in rows]


def get_report(author: str, date: str, kind: str) -> Optional[Dict[str, Any]]:
    with cursor() as conn:
        r = conn.execute(
            "SELECT * FROM reports WHERE author=? AND date=? AND kind=?", (author, date, kind)
        ).fetchone()
    return dict(r) if r else None
