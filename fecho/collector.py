"""团队联邦收集端：只收「日报 + 口播稿」成品，不碰任何人的原始进展。

隐私边界是结构性的，不是靠权限管出来的：这个文件里从头到尾没有 entries / tasks /
updates 表，collector 自己的库（team_reports.db）物理上就存不下一条原始进展。
每个人各自本机跑 fecho，日终整理完之后把成品 POST 过来；collector 只负责存和
给团队读，从不主动去问任何人「你今天做了什么」。

部署：在一台团队都能访问到的内部机器上跑 `fecho serve`。认证复用现有的
Bearer token（config.TOKENS_FILE），一人一个 token，和本机单进程模式的
auth.py 是同一套代码、同一个文件格式。
"""
import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from . import __version__, auth, config

SCHEMA = """
CREATE TABLE IF NOT EXISTS team_reports (
    author        TEXT NOT NULL,
    date          TEXT NOT NULL,
    kind          TEXT NOT NULL,        -- daily / voice
    content_md    TEXT NOT NULL,
    meta          TEXT NOT NULL DEFAULT '{}',
    received_at   TEXT NOT NULL,
    PRIMARY KEY (author, date, kind)
);
"""


def _db_path():
    config.HOME.mkdir(parents=True, exist_ok=True)
    return config.HOME / "collector.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init() -> None:
    with _conn() as conn:
        conn.executescript(SCHEMA)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    init()
    yield


app = FastAPI(
    title="Fecho Collector",
    version=__version__,
    description="团队联邦收集端：只收日报/口播稿成品，物理上不存在能读到原始进展的表。",
    lifespan=_lifespan,
)


def identity(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    ident = auth.resolve(authorization)
    if not ident:
        raise HTTPException(401, "无效或缺失的 Bearer token")
    return ident


class ReportPush(BaseModel):
    date: str = Field(..., description="YYYY-MM-DD")
    daily_md: Optional[str] = None
    voice_md: Optional[str] = None
    meta: Dict[str, Any] = Field(default_factory=dict)


@app.get("/healthz")
def healthz() -> Dict[str, Any]:
    return {"ok": True, "version": __version__, "mode": "collector"}


@app.post("/reports", status_code=201)
def push_reports(p: ReportPush, ident: Dict[str, Any] = Depends(identity)) -> Dict[str, Any]:
    """author 永远从 token 反查，绝不信任请求体——没人能冒充别人推报告。"""
    if not p.daily_md and not p.voice_md:
        raise HTTPException(400, "daily_md 和 voice_md 至少要有一个")
    author = ident["author"]
    ts = now_iso()
    meta = json.dumps(p.meta, ensure_ascii=False)
    kinds_written = []
    with _conn() as conn:
        for kind, content in (("daily", p.daily_md), ("voice", p.voice_md)):
            if not content:
                continue
            conn.execute(
                "INSERT INTO team_reports (author,date,kind,content_md,meta,received_at)"
                " VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(author,date,kind) DO UPDATE SET"
                " content_md=excluded.content_md, meta=excluded.meta,"
                " received_at=excluded.received_at",
                (author, p.date, kind, content, meta, ts),
            )
            kinds_written.append(kind)
    return {"ok": True, "author": author, "date": p.date, "received": kinds_written,
            "received_at": ts}


@app.get("/reports")
def list_reports(
    date: str = Query(..., description="YYYY-MM-DD"),
    author: Optional[str] = Query(None, description="只看某人，缺省=全员"),
    ident: Dict[str, Any] = Depends(identity),
) -> Dict[str, Any]:
    sql = "SELECT * FROM team_reports WHERE date=?"
    args: List[Any] = [date]
    if author:
        sql += " AND author=?"
        args.append(author)
    sql += " ORDER BY author, kind"
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    by_author: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        entry = by_author.setdefault(r["author"], {"author": r["author"], "date": date})
        entry[r["kind"]] = {"content_md": r["content_md"], "received_at": r["received_at"],
                            "meta": json.loads(r["meta"] or "{}")}
    return {"date": date, "count": len(by_author), "authors": list(by_author.values())}


@app.get("/reports/{date}/{author}.md", response_class=PlainTextResponse)
def get_report_md(date: str, author: str, kind: str = Query("daily"),
                  ident: Dict[str, Any] = Depends(identity)) -> str:
    with _conn() as conn:
        row = conn.execute(
            "SELECT content_md FROM team_reports WHERE author=? AND date=? AND kind=?",
            (author, date, kind),
        ).fetchone()
    if not row:
        raise HTTPException(404, "没有这份报告")
    return row["content_md"]
