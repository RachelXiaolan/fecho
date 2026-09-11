"""存元数据：本机版用 SQLite，云端版用 Postgres（Supabase）。

核心是**任务**，不是记录。一件事一个 task，进展一条条挂在它下面：
一个任务可以横跨很多天、很多个对话、很多个 agent。

两种数据库共用同一套 SQL：写法只挑两边都认的（ON CONFLICT、日期在 Python 里算好
再传），不做方言翻译。唯一的适配是占位符——上层一律写 ?，Postgres 那边换成 %s。
"""
import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from . import config


def backend() -> str:
    return "postgres" if config.DATABASE_URL else "sqlite"

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

CREATE TABLE IF NOT EXISTS task_events (
    event_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    author        TEXT NOT NULL,
    actor         TEXT NOT NULL,
    event_type    TEXT NOT NULL,       -- complete / reopen / merge
    from_task_id  TEXT NOT NULL,
    to_task_id    TEXT,
    details       TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(from_task_id, created_at);

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
    warnings      TEXT NOT NULL DEFAULT '[]',
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


# 云端版多出来的表：人、登录凭证、权限、后台任务。本机版建了也不用，不碍事。
SCHEMA_CLOUD = """
-- 用户。author 字段全库通用：本机版是配置里的名字，云端版是邮箱。
CREATE TABLE IF NOT EXISTS users (
    author        TEXT PRIMARY KEY,     -- 邮箱，全小写
    display_name  TEXT NOT NULL,
    mobius_user_id TEXT,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    daily_time    TEXT NOT NULL DEFAULT '21:00',   -- 北京时间，服务器在这个点出日报
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT
);

-- agent 用的个人 token。只存哈希：库被读走也拿不到能用的 token。
-- 有效期是滑动的：每用一次顺延 7 天，连续 7 天不用才失效。
CREATE TABLE IF NOT EXISTS api_tokens (
    token_hash    TEXT PRIMARY KEY,
    author        TEXT NOT NULL,
    label         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    last_used_at  TEXT,
    expires_at    TEXT NOT NULL,
    revoked_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_tokens_author ON api_tokens(author);

-- 网页登录会话，同样只存哈希。
CREATE TABLE IF NOT EXISTS web_sessions (
    session_hash  TEXT PRIMARY KEY,
    author        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL
);

-- 每个人的 Mobius 授权，服务器替他去同步 issue。refresh_token 加密后存。
CREATE TABLE IF NOT EXISTS mobius_credentials (
    author            TEXT PRIMARY KEY,
    access_token_enc  TEXT,
    access_expires_at INTEGER,
    refresh_token_enc TEXT,
    client_id         TEXT,
    token_endpoint    TEXT,
    resource          TEXT,
    scope             TEXT,
    updated_at        TEXT NOT NULL
);

-- admin 申请：非 admin 点按钮提交，由现有 admin 在面板里批。
CREATE TABLE IF NOT EXISTS admin_requests (
    request_id    TEXT PRIMARY KEY,
    author        TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',   -- pending / approved / denied
    created_at    TEXT NOT NULL,
    decided_by    TEXT,
    decided_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_admin_requests_status ON admin_requests(status, created_at);

-- 后台任务：网页上点「重新生成」、到点出日报，都变成一行排队，由 lu2 上的程序去跑。
-- 出日报要好几分钟，放在网页请求里做会超时。
CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    author        TEXT NOT NULL,
    kind          TEXT NOT NULL,        -- daily / regenerate
    date          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'queued',    -- queued / running / succeeded / failed
    attempts      INTEGER NOT NULL DEFAULT 0,
    run_after     TEXT NOT NULL,        -- 失败补跑时往后排
    error         TEXT,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(status, run_after);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_daily ON jobs(author, kind, date) WHERE kind = 'daily';

-- 工作文件夹：扫描的隐私边界。安装时 agent 从聊天记录里找出「你用 agent 干过活的
-- 文件夹」传上来（只有路径，没有内容），网页上勾选。勾中的就是白名单，所有 agent 共用。
CREATE TABLE IF NOT EXISTS work_folders (
    author        TEXT NOT NULL,
    path          TEXT NOT NULL,
    selected      INTEGER NOT NULL DEFAULT 0,
    last_used     TEXT,                 -- 最近一次在这里用 agent 的时间
    reported_at   TEXT NOT NULL,
    PRIMARY KEY (author, path)
);

-- 每个人的 agent：连没连上、要不要扫它的聊天记录。
-- 两件事是独立的：没接 MCP 的 agent，本机照样可以扫它的聊天记录。
CREATE TABLE IF NOT EXISTS agent_connections (
    author        TEXT NOT NULL,
    agent         TEXT NOT NULL,        -- claude-code / codex / hermes
    scan_enabled  INTEGER NOT NULL DEFAULT 1,
    first_seen    TEXT,
    last_seen     TEXT,
    PRIMARY KEY (author, agent)
);

-- 每天的本机扫描做完没有。本机每 15 分钟来问一次「该扫了吗」，靠这张表回答。
CREATE TABLE IF NOT EXISTS scan_checkins (
    author        TEXT NOT NULL,
    date          TEXT NOT NULL,
    status        TEXT NOT NULL,        -- done / failed
    uploaded      INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    finished_at   TEXT NOT NULL,
    PRIMARY KEY (author, date)
);

-- 服务器级的小配置。现在只放一样：在 Mobius 注册过的 OAuth 客户端。
-- 不缓存的话每登录一次就去 Mobius 注册一个新客户端，那边会越堆越多。
CREATE TABLE IF NOT EXISTS app_settings (
    key           TEXT PRIMARY KEY,
    value         TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
"""


def _pg_sql(sql: str) -> str:
    # 上层 SQL 一律用 ? 占位。换成 %s 之前先把字面的 % 转义，免得 LIKE '%x%'
    # 被 psycopg 当成占位符。
    return sql.replace("%", "%%").replace("?", "%s")


class _PgConn:
    """让 psycopg 连接用起来像 sqlite3 连接：? 占位、按列名取行、executescript。

    上层代码几十处 conn.execute(...)，靠这一层就不用逐个改。
    """

    def __init__(self, conn: Any) -> None:
        self._c = conn

    def execute(self, sql: str, params: Any = ()) -> Any:
        params = tuple(params) if params else ()
        return self._c.execute(_pg_sql(sql) if params else sql, params or None)

    def executemany(self, sql: str, seq: Any) -> Any:
        cur = self._c.cursor()
        cur.executemany(_pg_sql(sql), [tuple(x) for x in seq])
        return cur

    def executescript(self, script: str) -> None:
        self._c.execute(script)

    def commit(self) -> None:
        self._c.commit()

    def rollback(self) -> None:
        self._c.rollback()


_pool: Any = None


def _pg_pool() -> Any:
    """一个进程一个连接池。

    每次操作都新建连接的话，每次都要和云上的数据库握手一遍；而一个页面会连着查
    好几次（按任务取进展就是一任务一查），全是新连接会慢得明显。
    prepare_threshold=None：Supabase 的连接池是事务模式，不支持预编译语句。
    """
    global _pool
    if _pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        _pool = ConnectionPool(
            config.DATABASE_URL, min_size=1, max_size=5, open=True,
            kwargs={"row_factory": dict_row, "prepare_threshold": None, "autocommit": False},
        )
    return _pool


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def location() -> str:
    if backend() == "postgres":
        # 地址后面可能跟着 ?host=/一串/路径，按最后一个 / 切会切到路径尾巴上
        from urllib.parse import urlparse
        return "postgres:" + urlparse(config.DATABASE_URL).path.lstrip("/")
    return str(config.DB_PATH)


def is_disposable() -> bool:
    """这个库能不能随便清空——只有测试专用的库才行。

    真实翻车过：一个临时脚本先导入了 fecho、后导入测试文件，测试里「指向临时目录」
    的设置没生效，每个测试开头的清表就在用户真实的 ~/.fecho/fecho.db 上执行了，
    几百条进展被清掉，只救回一部分。所以清表之前必须过这一关。
    """
    import tempfile
    if backend() == "postgres":
        return "test" in location().split(":", 1)[1]
    path = config.DB_PATH.resolve()
    tmp = __import__("pathlib").Path(tempfile.gettempdir()).resolve()
    return tmp in path.parents


def require_disposable() -> None:
    if not is_disposable():
        raise RuntimeError("拒绝清空数据库：%s 不是测试用的临时库。"
                           "测试必须在导入 fecho 之前把 FECHO_HOME/FECHO_DB 指到临时目录。"
                           % location())


def _schema_for(kind: str) -> str:
    if kind == "postgres":
        # 两边唯一不兼容的建表写法：自增主键
        return SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    return SCHEMA


def init() -> None:
    if backend() == "postgres":
        # 云端版是全新的库，建表语句本身就带全了所有列，不需要下面那些老库补列。
        with cursor() as conn:
            conn.executescript(_schema_for("postgres"))
            conn.executescript(SCHEMA_CLOUD)
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_updates_source_event"
                         " ON updates(source_event_key) WHERE source_event_key IS NOT NULL")
        return

    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.executescript(SCHEMA_CLOUD)
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
        report_columns = {r["name"] for r in conn.execute("PRAGMA table_info(reports)").fetchall()}
        if "warnings" not in report_columns:
            conn.execute("ALTER TABLE reports ADD COLUMN warnings TEXT NOT NULL DEFAULT '[]'")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_updates_source_event"
                     " ON updates(source_event_key) WHERE source_event_key IS NOT NULL")


@contextmanager
def cursor() -> Iterator[Any]:
    if backend() == "postgres":
        with _pg_pool().connection() as raw:
            conn = _PgConn(raw)
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return

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
    # 同一毫秒写入的两条，用 update_id 定个稳定顺序（rowid 只有 SQLite 有）
    sql += " ORDER BY created_at ASC, update_id ASC"
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
    if not r:
        return None
    out = dict(r)
    try:
        out["warnings"] = json.loads(out.get("warnings") or "[]")
    except ValueError:
        out["warnings"] = []
    return out


def report_history(author: str, date: str) -> List[Dict[str, Any]]:
    with cursor() as conn:
        rows = conn.execute(
            "SELECT * FROM report_history WHERE author=? AND date=?"
            " ORDER BY created_at DESC, id DESC", (author, date)
        ).fetchall()
    return [dict(r) for r in rows]
