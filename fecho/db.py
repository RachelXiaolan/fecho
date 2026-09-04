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
    session_id    TEXT,                 -- 哪个对话（审计用；任务与对话是多对多）
    match_method  TEXT NOT NULL,        -- explicit / mobius-auto / task-continue / new-task
    match_score   REAL,
    pto_status    TEXT,
    created_at    TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',  -- active / duplicate-ignored
    meta          TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_updates_task ON updates(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_updates_author_date ON updates(author, date);

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


def get_report(author: str, date: str, kind: str) -> Optional[Dict[str, Any]]:
    with cursor() as conn:
        r = conn.execute(
            "SELECT * FROM reports WHERE author=? AND date=? AND kind=?", (author, date, kind)
        ).fetchone()
    return dict(r) if r else None
