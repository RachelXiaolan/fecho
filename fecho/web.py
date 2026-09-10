"""本机 HTTP 层：一个进程托两样东西。

  /sse/       —— MCP over SSE，ChatGPT 要的就是这个（URL 必须以 /sse/ 结尾）
  /mcp        —— MCP over Streamable HTTP，新客户端用这个
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

from . import __version__, config, db, mcp_server, store

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
    rank = {"task-continue": 0, "new-task": 1, "verified": 2,
            "explicit": 3, "explicit-freeform": 3, "project-bound": 4}
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT u.update_id, u.content_md, u.match_method, u.match_score,"
            " u.assignment_source, u.assignment_locked, u.source_agent,"
            " u.ingestion_method, u.session_id, u.completion_status, u.content_kind, u.created_at,"
            " t.issue_key, t.title FROM updates u JOIN tasks t ON t.task_id=u.task_id"
            " WHERE u.author=? AND u.date=? AND u.status='active' AND u.assignment_locked=0"
            " ORDER BY u.created_at", (author, date)).fetchall()
    out = []
    for r in rows:
        m = r["match_method"]
        out.append({"update_id": r["update_id"], "content": r["content_md"],
                    "method": m, "issue_key": r["issue_key"], "title": r["title"],
                    "rank": rank.get(m, 9), "match_score": r["match_score"],
                    "assignment_source": r["assignment_source"],
                    "source_agent": r["source_agent"],
                    "ingestion_method": r["ingestion_method"],
                    "session_id": r["session_id"], "created_at": r["created_at"],
                    "completion_status": r["completion_status"],
                    "content_kind": r["content_kind"],
                    "confidence": "low" if m == "task-continue" else
                                  ("medium" if m in ("new-task", "verified") else "high")})
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


def timeline(author: str, days: int = 7, end_date: Optional[str] = None) -> List[Dict[str, Any]]:
    """按 issue 看这几天的进展。写周报的原料。"""
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT u.date, u.content_md, COALESCE(t.issue_key,'') k, t.title, t.source"
            " FROM updates u JOIN tasks t ON t.task_id=u.task_id"
            " WHERE u.author=? AND u.status='active'"
            " AND u.date BETWEEN date(?, ?) AND date(?) ORDER BY t.issue_key, u.date",
            (author, end_date or store.today(), "-%d days" % (days - 1),
             end_date or store.today())).fetchall()
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


def task_list(author: str) -> List[Dict[str, Any]]:
    tasks = db.list_tasks(author=author)
    for task in tasks:
        updates = db.list_updates(task_id=task["task_id"])
        task["updates"] = updates
        task["update_count"] = len(updates)
        task["last_progress"] = updates[-1]["content_md"] if updates else None
    return tasks


def report_payload(author: str, date: str) -> Dict[str, Any]:
    from . import auth, digest, personas, pto

    daily = db.get_report(author, date, "daily")
    voice = db.get_report(author, date, "voice")
    ident = auth.all_authors().get(author, {})
    persona = personas.load(ident.get("persona") or author)
    if not persona.get("display_name"):
        persona["display_name"] = ident.get("display_name") or author
    current_fp = digest.fingerprint(db.day_tasks(author, date), persona, pto.status(author, date))
    return {
        "daily": (daily or {}).get("content_md"),
        "voice": (voice or {}).get("content_md"),
        "generator": (daily or {}).get("generator"),
        "warnings": (daily or {}).get("warnings", []),
        "generated_at": (daily or {}).get("created_at"),
        "dirty": bool(daily and daily.get("fingerprint") != current_fp),
        "history": db.report_history(author, date),
    }


def dashboard_payload(author: str, date: str) -> Dict[str, Any]:
    from . import automation, mobius, service

    all_updates = db.list_updates(author=author)
    with db.cursor() as conn:
        scan_rows = [dict(r) for r in conn.execute(
            "SELECT producer_agent,session_id,project,date,status,chunks,entries,error,"
            "started_at,finished_at FROM scan_runs WHERE author=?"
            " ORDER BY started_at DESC LIMIT 20", (author,)).fetchall()]
    agents = sorted({u["source_agent"] for u in all_updates})
    ingestions = sorted({u.get("ingestion_method", "direct") for u in all_updates})
    return {
        "date": date,
        "overview": overview(author, date),
        "today": service.day(author, date),
        "review": {"items": review_queue(author, date)},
        "tasks": {"items": task_list(author)},
        "reports": report_payload(author, date),
        "hidden": {"items": hidden_entries(author, date)},
        "timeline": {"groups": timeline(author, 14, date)},
        "issues": {"items": mobius.cached_issues(author)},
        "system": {"doctor": service.doctor(), "scan_runs": scan_rows,
                   "automation": automation.status()},
        "filters": {"agents": agents, "ingestion_methods": ingestions,
                    "completion_statuses": ["done", "wip", "blocked", "unknown"]},
    }


# ---------- FastAPI ----------

def build_app():
    from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
    from fastapi.responses import HTMLResponse, JSONResponse

    app = FastAPI(title="fecho", docs_url=None, redoc_url=None)

    @app.exception_handler(ValueError)
    async def value_error_handler(_request: Request, exc: ValueError):
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

    @app.exception_handler(KeyError)
    async def key_error_handler(_request: Request, exc: KeyError):
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "缺少参数: %s" % str(exc).strip("'")})

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

    # ---- MCP over SSE ----
    # 老一档的 MCP 传输，但 ChatGPT 现在要的就是它，且 URL 必须以 /sse/ 结尾。
    # 握手是两条腿：GET 开一条长连接，第一个事件告诉对方「消息往哪 POST」；
    # 之后每次 POST 的响应不从 POST 返回，而是顺着那条长连接推回去。
    import asyncio

    sessions: Dict[str, Dict[str, Any]] = {}
    http_contexts: Dict[str, mcp_server.MCPContext] = {}

    @app.get("/sse/")
    @app.get("/sse")
    async def sse_stream(request: Request, authorization: Optional[str] = Header(None)):
        from fastapi.responses import StreamingResponse

        guard(request, authorization)
        sid = secrets.token_urlsafe(16)
        q: asyncio.Queue = asyncio.Queue()
        sessions[sid] = {"queue": q, "context": mcp_server.MCPContext(session_id=sid)}

        async def gen():
            yield "event: endpoint\ndata: /sse/messages?session_id=%s\n\n" % sid
            try:
                while True:
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"      # 挡住中间层的空闲超时
                        continue
                    if msg is None:
                        break
                    yield "event: message\ndata: %s\n\n" % json.dumps(msg, ensure_ascii=False)
            finally:
                sessions.pop(sid, None)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "Connection": "keep-alive",
            "X-Accel-Buffering": "no"})

    @app.post("/sse/messages")
    async def sse_messages(request: Request, session_id: str = Query(...),
                           authorization: Optional[str] = Header(None)):
        guard(request, authorization)
        session = sessions.get(session_id)
        if session is None:
            raise HTTPException(404, "会话不存在或已断开，请重新连接 /sse/")
        q = session["queue"]
        msg = await request.json()
        resp = mcp_server.handle(msg, session["context"])
        if resp is not None:
            await q.put(resp)
        return JSONResponse(status_code=202, content={"ok": True})

    # ---- MCP over Streamable HTTP ----
    @app.post("/mcp")
    async def mcp_endpoint(request: Request,
                           authorization: Optional[str] = Header(None),
                           mcp_session_id: Optional[str] = Header(None,
                                                                  alias="Mcp-Session-Id")):
        guard(request, authorization)
        msg = await request.json()
        method = msg.get("method")
        sid = mcp_session_id
        if method == "initialize" and (not sid or sid not in http_contexts):
            sid = secrets.token_urlsafe(18)
            http_contexts[sid] = mcp_server.MCPContext(session_id=sid)
        context = http_contexts.get(sid) if sid else mcp_server.MCPContext()
        resp = mcp_server.handle(msg, context)
        headers = {"Mcp-Session-Id": sid} if sid else {}
        if resp is None:                       # 通知类消息没有响应体
            return JSONResponse(status_code=202, content=None, headers=headers)
        return JSONResponse(resp, headers=headers)

    @app.get("/healthz")
    def healthz(request: Request, authorization: Optional[str] = Header(None)):
        guard(request, authorization)
        return {"ok": True, "version": __version__}

    # ---- dashboard 数据 ----
    @app.get("/api/overview")
    def api_overview(request: Request, date: Optional[str] = None,
                     authorization: Optional[str] = Header(None)):
        guard(request, authorization)
        return overview(config.AUTHOR, date or store.today())

    @app.get("/api/dashboard")
    def api_dashboard(request: Request, date: Optional[str] = None,
                      authorization: Optional[str] = Header(None)):
        guard(request, authorization)
        return dashboard_payload(config.AUTHOR, date or store.today())

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
        update_id = body["update_id"]
        with db.cursor() as conn:
            row = conn.execute("SELECT date FROM updates WHERE update_id=? AND author=?",
                               (update_id, config.AUTHOR)).fetchone()
        if row is None:
            raise ValueError("进展不存在: %s" % update_id)
        kw = ({"issue_key": body["issue_key"]} if body.get("issue_key")
              else {"freeform": True})
        result = store.correct_progress(update_id, config.AUTHOR, **kw)
        date_ = body.get("date") or row["date"]
        return {"ok": True, "changed": result["changed"],
                "message": "归属已确认", "dashboard": dashboard_payload(config.AUTHOR, date_)}

    @app.post("/api/correct")
    def api_correct(request: Request, body: Dict[str, Any] = Body(...),
                    authorization: Optional[str] = Header(None)):
        guard(request, authorization)
        update_id = body["update_id"]
        with db.cursor() as conn:
            row = conn.execute("SELECT date FROM updates WHERE update_id=? AND author=?",
                               (update_id, config.AUTHOR)).fetchone()
        if row is None:
            raise ValueError("进展不存在: %s" % update_id)
        kw: Dict[str, Any] = {}
        if "content" in body:
            kw["content_md"] = body["content"]
        if body.get("issue_key"):
            kw["issue_key"] = body["issue_key"]
        elif "issue_key" in body:
            kw["freeform"] = True
        result = store.correct_progress(update_id, config.AUTHOR, **kw)
        date_ = body.get("date") or row["date"]
        return {"ok": True, "changed": result["changed"], "message": "进展已修订",
                "dashboard": dashboard_payload(config.AUTHOR, date_)}

    @app.post("/api/tasks/{task_id}/complete")
    def api_complete_task(task_id: str, request: Request,
                          body: Dict[str, Any] = Body(default={}),
                          authorization: Optional[str] = Header(None)):
        guard(request, authorization)
        from . import service
        result = service.complete_task(task_id)
        return {"ok": True, **result,
                "dashboard": dashboard_payload(config.AUTHOR, body.get("date") or store.today())}

    @app.post("/api/tasks/{task_id}/reopen")
    def api_reopen_task(task_id: str, request: Request,
                        body: Dict[str, Any] = Body(default={}),
                        authorization: Optional[str] = Header(None)):
        guard(request, authorization)
        from . import service
        result = service.reopen_task(task_id)
        return {"ok": True, **result,
                "dashboard": dashboard_payload(config.AUTHOR, body.get("date") or store.today())}

    @app.post("/api/tasks/merge")
    def api_merge_tasks(request: Request, body: Dict[str, Any] = Body(...),
                        authorization: Optional[str] = Header(None)):
        guard(request, authorization)
        from . import service
        result = service.merge_tasks(body["source_task_id"], body["target_task_id"])
        return {"ok": True, **result,
                "dashboard": dashboard_payload(config.AUTHOR, body.get("date") or store.today())}

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
            row = conn.execute("SELECT date FROM updates WHERE update_id=? AND author=?",
                               (body["update_id"], config.AUTHOR)).fetchone()
            if row is None:
                raise ValueError("进展不存在: %s" % body["update_id"])
            n = conn.execute("UPDATE updates SET status='active',revision=revision+1"
                             " WHERE update_id=? AND author=?",
                             (body["update_id"], config.AUTHOR)).rowcount
        date_ = body.get("date") or row["date"]
        return {"ok": bool(n), "message": "进展已恢复",
                "dashboard": dashboard_payload(config.AUTHOR, date_)}

    @app.get("/api/timeline")
    def api_timeline(request: Request, days: int = Query(7, ge=1, le=90),
                     date: Optional[str] = None,
                     authorization: Optional[str] = Header(None)):
        guard(request, authorization)
        return {"groups": timeline(config.AUTHOR, days, date)}

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
        return {"date": d, **report_payload(config.AUTHOR, d)}

    @app.post("/api/regenerate")
    def api_regenerate(request: Request, body: Dict[str, Any] = Body(default={}),
                       authorization: Optional[str] = Header(None)):
        guard(request, authorization)
        from . import service
        date_ = body.get("date") or store.today()
        result = service.end_of_day(date_, force=True)
        return {"ok": True, "result": result,
                "dashboard": dashboard_payload(config.AUTHOR, date_)}

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
