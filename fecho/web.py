"""本机 HTTP 层：一个进程托两样东西。

  /mcp        —— MCP over Streamable HTTP，给连不上 stdio 的客户端（ChatGPT 等）
  /           —— dashboard，看数据、改归属

为什么两样放一起：都需要「有个 HTTP 服务能读到这份 SQLite」，而数据在本机、
扫描要读本机的对话记录，所以服务也得在本机。分成两个进程只是多一份维护。

MCP 那边直接复用 mcp_server.handle()——它本来就是纯 JSON-RPC 函数，和 stdio
无关。工具集不按客户端分：fecho 的工具就是那些，谁连上都一样，鉴权管的是
「能不能连」而不是「连上给几个」。

默认只监听 127.0.0.1。要从公网用（ChatGPT），走隧道并设 FECHO_WEB_TOKEN——
不设 token 时拒绝非本机来源，免得隧道一开就裸奔。
"""
import json
import os
import secrets
from typing import Any, Dict, List, Optional

from . import config, db, mcp_server, store

TOKEN = os.getenv("FECHO_WEB_TOKEN") or config.get("web_token", "FECHO_WEB_TOKEN", "")


# ---------- dashboard 用的数据 ----------

def overview(author: str, date: str) -> Dict[str, Any]:
    """今天到底干活了没。"""
    with db.cursor() as conn:
        row = conn.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT task_id) t FROM updates"
            " WHERE author=? AND date=? AND status='active'", (author, date)).fetchone()
        hidden = conn.execute(
            "SELECT COUNT(*) n FROM updates WHERE author=? AND date=?"
            " AND status IN ('duplicate-ignored','superseded')", (author, date)).fetchone()["n"]
        mark = conn.execute(
            "SELECT MAX(last_ts) t FROM scan_marks").fetchone()["t"]
    rep = db.get_report(author, date, "daily")
    return {"date": date, "updates": row["n"], "tasks": row["t"], "hidden": hidden,
            "scan_mark": mark, "has_report": bool(rep),
            "report_status": (rep or {}).get("generator")}


def review_queue(author: str, date: str) -> List[Dict[str, Any]]:
    """需要你看一眼的条目。

    排序按「错得起的程度」：猜的排最前，其次落自由任务的（归属没成功），
    最后是验证改过的（多半对了，但值得确认）。明确写了 issue 号又没被改过的
    不在这里——那种没什么可复核的。
    """
    rank = {"task-continue": 0, "new-task": 1, "verified": 2, "project-bound": 3}
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT u.update_id, u.content_md, u.match_method, u.created_at,"
            " t.issue_key, t.title FROM updates u JOIN tasks t ON t.task_id=u.task_id"
            " WHERE u.author=? AND u.date=? AND u.status='active'"
            " ORDER BY u.created_at", (author, date)).fetchall()
    out = []
    for r in rows:
        m = r["match_method"]
        if m == "explicit":
            continue
        out.append({"update_id": r["update_id"], "content": r["content_md"],
                    "method": m, "issue_key": r["issue_key"], "title": r["title"],
                    "rank": rank.get(m, 9)})
    out.sort(key=lambda x: x["rank"])
    return out


def hidden_entries(author: str, date: Optional[str] = None) -> List[Dict[str, Any]]:
    sql = ("SELECT u.update_id, u.date, u.content_md, u.status, COALESCE(t.issue_key,'') k"
           " FROM updates u JOIN tasks t ON t.task_id=u.task_id"
           " WHERE u.author=? AND u.status IN ('duplicate-ignored','superseded')")
    p: List[Any] = [author]
    if date:
        sql += " AND u.date=?"
        p.append(date)
    with db.cursor() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY u.date DESC", p).fetchall()]


def timeline(author: str, days: int = 7) -> List[Dict[str, Any]]:
    """按 issue 看这几天的进展。写周报的原料。"""
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT u.date, u.content_md, COALESCE(t.issue_key,'') k, t.title, t.source"
            " FROM updates u JOIN tasks t ON t.task_id=u.task_id"
            " WHERE u.author=? AND u.status='active'"
            " AND u.date >= date('now', ?) ORDER BY t.issue_key, u.date",
            (author, "-%d days" % days)).fetchall()
    grouped: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        key = r["k"] or ("freeform:" + r["title"])
        g = grouped.setdefault(key, {"issue_key": r["k"], "title": r["title"],
                                     "source": r["source"], "days": {}})
        g["days"].setdefault(r["date"], []).append(r["content_md"])
    out = list(grouped.values())
    out.sort(key=lambda g: (not g["issue_key"], -sum(len(v) for v in g["days"].values())))
    return out


def health(author: str, days: int = 14) -> Dict[str, Any]:
    """归属成功率的代理指标。

    落自由任务的比例是这套机制在**这个人身上**灵不灵的最直接信号——
    整套目前只在一个人一周的数据上验证过，这个数字是唯一的预警。
    """
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT u.match_method m, COUNT(*) n FROM updates u"
            " JOIN tasks t ON t.task_id=u.task_id WHERE u.author=? AND u.status='active'"
            " AND u.date >= date('now', ?) GROUP BY u.match_method",
            (author, "-%d days" % days)).fetchall()
        free = conn.execute(
            "SELECT COUNT(*) n FROM updates u JOIN tasks t ON t.task_id=u.task_id"
            " WHERE u.author=? AND u.status='active' AND t.issue_key IS NULL"
            " AND u.date >= date('now', ?)", (author, "-%d days" % days)).fetchone()["n"]
    by = {r["m"]: r["n"] for r in rows}
    total = sum(by.values())
    return {"days": days, "total": total, "by_method": by, "freeform": free,
            "freeform_pct": round(100.0 * free / total, 1) if total else 0.0}


# ---------- FastAPI ----------

def build_app():
    from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
    from fastapi.responses import HTMLResponse, JSONResponse

    app = FastAPI(title="fecho", docs_url=None, redoc_url=None)

    def guard(request: Request, authorization: Optional[str]) -> None:
        """本机随便连；非本机必须带 token。

        隧道一开，/mcp 就在公网上了。没设 token 时直接拒绝外部来源——
        宁可连不上，也不要默认裸奔。
        """
        host = (request.client.host if request.client else "") or ""
        if host in ("127.0.0.1", "::1", "localhost"):
            return
        if not TOKEN:
            raise HTTPException(403, "非本机访问需要先设 FECHO_WEB_TOKEN")
        got = (authorization or "").removeprefix("Bearer ").strip()
        if not secrets.compare_digest(got, TOKEN):
            raise HTTPException(401, "token 不对")

    # ---- MCP over Streamable HTTP ----
    @app.post("/mcp")
    async def mcp_endpoint(request: Request,
                           authorization: Optional[str] = Header(None)):
        guard(request, authorization)
        msg = await request.json()
        resp = mcp_server.handle(msg)
        if resp is None:                       # 通知类消息没有响应体
            return JSONResponse(status_code=202, content=None)
        return JSONResponse(resp)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "author": config.AUTHOR}

    # ---- dashboard 数据 ----
    @app.get("/api/overview")
    def api_overview(request: Request, date: Optional[str] = None,
                     authorization: Optional[str] = Header(None)):
        guard(request, authorization)
        return overview(config.AUTHOR, date or store.today())

    @app.get("/api/review")
    def api_review(request: Request, date: Optional[str] = None,
                   authorization: Optional[str] = Header(None)):
        guard(request, authorization)
        return {"items": review_queue(config.AUTHOR, date or store.today())}

    @app.get("/api/issues")
    def api_issues(request: Request, authorization: Optional[str] = Header(None)):
        guard(request, authorization)
        from . import mobius
        return {"issues": mobius.cached_issues(config.AUTHOR)}

    @app.post("/api/reassign")
    def api_reassign(request: Request, body: Dict[str, Any] = Body(...),
                     authorization: Optional[str] = Header(None)):
        guard(request, authorization)
        ok = store.reassign(body["update_id"], config.AUTHOR, body.get("issue_key") or None)
        return {"ok": ok}

    @app.get("/api/hidden")
    def api_hidden(request: Request, date: Optional[str] = None,
                   authorization: Optional[str] = Header(None)):
        guard(request, authorization)
        return {"items": hidden_entries(config.AUTHOR, date)}

    @app.post("/api/restore")
    def api_restore(request: Request, body: Dict[str, Any] = Body(...),
                    authorization: Optional[str] = Header(None)):
        guard(request, authorization)
        with db.cursor() as conn:
            n = conn.execute("UPDATE updates SET status='active' WHERE update_id=? AND author=?",
                             (body["update_id"], config.AUTHOR)).rowcount
        return {"ok": bool(n)}

    @app.get("/api/timeline")
    def api_timeline(request: Request, days: int = Query(7, ge=1, le=90),
                     authorization: Optional[str] = Header(None)):
        guard(request, authorization)
        return {"groups": timeline(config.AUTHOR, days)}

    @app.get("/api/health-stats")
    def api_health(request: Request, days: int = Query(14, ge=1, le=180),
                   authorization: Optional[str] = Header(None)):
        guard(request, authorization)
        return health(config.AUTHOR, days)

    @app.get("/api/report")
    def api_report(request: Request, date: Optional[str] = None,
                   authorization: Optional[str] = Header(None)):
        guard(request, authorization)
        d = date or store.today()
        daily = db.get_report(config.AUTHOR, d, "daily")
        voice = db.get_report(config.AUTHOR, d, "voice")
        return {"date": d, "daily": (daily or {}).get("content_md"),
                "voice": (voice or {}).get("content_md")}

    @app.post("/api/regenerate")
    def api_regenerate(request: Request, body: Dict[str, Any] = Body(default={}),
                       authorization: Optional[str] = Header(None)):
        guard(request, authorization)
        from . import service
        return service.end_of_day(body.get("date"), force=True)

    @app.get("/", response_class=HTMLResponse)
    def index():
        from pathlib import Path
        return (Path(__file__).resolve().parent / "presets" / "dashboard.html").read_text(encoding="utf-8")

    return app


def serve(host: str = "127.0.0.1", port: int = 8900, open_browser: bool = True) -> None:
    import uvicorn

    db.init()
    if host not in ("127.0.0.1", "localhost") and not TOKEN:
        raise SystemExit("监听非本机地址必须先设 FECHO_WEB_TOKEN，否则数据就裸奔了。")
    if open_browser:
        import threading
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open("http://%s:%d/" % (host, port))).start()
    uvicorn.run(build_app(), host=host, port=port, log_level="warning")
