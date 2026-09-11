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
import dataclasses
import json
import os
import secrets
from typing import Any, Dict, List, Optional

from . import __version__, accounts, config, db, mcp_server, store

# 云端版的两个 cookie：登录后的会话、跳去 Mobius 登录路上的临时状态
SESSION_COOKIE = "fecho_session"
OAUTH_COOKIE = "fecho_oauth"

TOKEN = os.getenv("FECHO_WEB_TOKEN") or config.get("web_token", "FECHO_WEB_TOKEN", "")


# ---------- dashboard 用的数据 ----------


def _days_ago(days: int) -> str:
    from datetime import timedelta
    from . import clock
    return (clock.now() - timedelta(days=days)).strftime("%Y-%m-%d")

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
    from datetime import datetime, timedelta

    end = end_date or store.today()
    start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT u.date, u.content_md, COALESCE(t.issue_key,'') k, t.title, t.source"
            " FROM updates u JOIN tasks t ON t.task_id=u.task_id"
            " WHERE u.author=? AND u.status='active'"
            " AND u.date BETWEEN ? AND ? ORDER BY t.issue_key, u.date",
            (author, start, end)).fetchall()
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
    # 起始日期在 Python 里按北京时间算好再传。原来写的 date('now', ?) 一是只有
    # SQLite 认，二是取的 UTC 日期——北京早上 8 点前算「最近 N 天」会差一天。
    since = _days_ago(days)
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT u.match_method m, COUNT(*) n FROM updates u"
            " JOIN tasks t ON t.task_id=u.task_id WHERE u.author=? AND u.status='active'"
            " AND u.date >= ? GROUP BY u.match_method",
            (author, since)).fetchall()
        free = conn.execute(
            "SELECT COUNT(*) n FROM updates u JOIN tasks t ON t.task_id=u.task_id"
            " WHERE u.author=? AND u.status='active' AND t.issue_key IS NULL"
            " AND u.date >= ?", (author, since)).fetchone()["n"]
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

    def guard(request: Request, authorization: Optional[str]) -> str:
        """认人。返回这个请求是谁的（author）。

        本机版：本机随便连，非本机必须带 FECHO_WEB_TOKEN；身份就是配置里那个名字。
        隧道一开 /mcp 就在公网上了，没设 token 时直接拒绝外部来源——宁可连不上，
        也不要默认裸奔。

        云端版：agent 带个人 token（Authorization: Bearer），浏览器带登录 cookie。
        两样都没有就是没登录。
        """
        if config.CLOUD:
            got = (authorization or "").removeprefix("Bearer ").strip()
            if got:
                author = accounts.resolve_token(got)
                if not author:
                    raise HTTPException(401, "token 无效或已过期（连续 7 天没用会失效），"
                                             "请到 %s/onboard 重新获取" % config.PUBLIC_URL)
                return author
            author = accounts.resolve_session(request.cookies.get(SESSION_COOKIE, ""))
            if not author:
                raise HTTPException(401, "请先登录")
            return author

        host = (request.client.host if request.client else "") or ""
        if host in ("127.0.0.1", "::1", "localhost"):
            return config.AUTHOR
        if not TOKEN:
            raise HTTPException(403, "非本机访问需要先设 FECHO_WEB_TOKEN")
        got = (authorization or "").removeprefix("Bearer ").strip()
        if not secrets.compare_digest(got, TOKEN):
            raise HTTPException(401, "token 不对")
        return config.AUTHOR

    def admin_only(author: str) -> None:
        if not config.CLOUD:
            raise HTTPException(404, "本机版没有 admin 面板")
        if not accounts.is_admin(author):
            raise HTTPException(403, "需要 admin 权限")

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

        if config.CLOUD:
            # 长连接 + 会话存内存，放到按请求运行的平台上撑不住。云端版只走 /mcp。
            raise HTTPException(404, "云端版请用 %s/mcp" % config.PUBLIC_URL)
        me = guard(request, authorization)
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
        me = guard(request, authorization)
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
        me = guard(request, authorization)
        msg = await request.json()
        method = msg.get("method")
        sid = mcp_session_id
        if method == "initialize" and (not sid or sid not in http_contexts):
            sid = secrets.token_urlsafe(18)
            http_contexts[sid] = mcp_server.MCPContext(session_id=sid)
        context = http_contexts.get(sid) if sid else None
        if context is None:
            # 云端版跑在多个实例上，这次请求落到的实例可能没见过这个会话号。
            # 按会话号新建一个——不能退回默认上下文，那是本机版那一个人的身份，
            # 退回去等于把这个人的进展记到别人头上。
            context = mcp_server.MCPContext(session_id=sid or secrets.token_urlsafe(18))
            if sid:
                http_contexts[sid] = context
        if config.CLOUD:
            if method == "initialize":
                context.author = me          # initialize 会往缓存里写客户端名，用原对象
            else:
                # 身份每次都按这次请求的 token 重填，不信任缓存里的
                context = dataclasses.replace(context, author=me)
        resp = mcp_server.handle(msg, context)
        headers = {"Mcp-Session-Id": sid} if sid else {}
        if resp is None:                       # 通知类消息没有响应体
            return JSONResponse(status_code=202, content=None, headers=headers)
        return JSONResponse(resp, headers=headers)

    @app.get("/healthz")
    def healthz(request: Request, authorization: Optional[str] = Header(None)):
        # 云端版不要求登录：部署平台和监控要靠它判断服务活没活。只返回版本号，
        # 不带任何身份信息，所以公开没问题。本机版照旧只让本机访问。
        if not config.CLOUD:
            guard(request, authorization)
        return {"ok": True, "version": __version__}

    # ---- dashboard 数据 ----
    @app.get("/api/overview")
    def api_overview(request: Request, date: Optional[str] = None,
                     authorization: Optional[str] = Header(None)):
        me = guard(request, authorization)
        return overview(me, date or store.today())

    @app.get("/api/dashboard")
    def api_dashboard(request: Request, date: Optional[str] = None,
                      authorization: Optional[str] = Header(None)):
        me = guard(request, authorization)
        return dashboard_payload(me, date or store.today())

    @app.get("/api/review")
    def api_review(request: Request, date: Optional[str] = None,
                   authorization: Optional[str] = Header(None)):
        me = guard(request, authorization)
        return {"items": review_queue(me, date or store.today())}

    @app.get("/api/issues")
    def api_issues(request: Request, authorization: Optional[str] = Header(None)):
        me = guard(request, authorization)
        from . import mobius
        return {"issues": mobius.cached_issues(me)}

    @app.post("/api/reassign")
    def api_reassign(request: Request, body: Dict[str, Any] = Body(...),
                     authorization: Optional[str] = Header(None)):
        me = guard(request, authorization)
        update_id = body["update_id"]
        with db.cursor() as conn:
            row = conn.execute("SELECT date FROM updates WHERE update_id=? AND author=?",
                               (update_id, me)).fetchone()
        if row is None:
            raise ValueError("进展不存在: %s" % update_id)
        kw = ({"issue_key": body["issue_key"]} if body.get("issue_key")
              else {"freeform": True})
        result = store.correct_progress(update_id, me, **kw)
        date_ = body.get("date") or row["date"]
        return {"ok": True, "changed": result["changed"],
                "message": "归属已确认", "dashboard": dashboard_payload(me, date_)}

    @app.post("/api/correct")
    def api_correct(request: Request, body: Dict[str, Any] = Body(...),
                    authorization: Optional[str] = Header(None)):
        me = guard(request, authorization)
        update_id = body["update_id"]
        with db.cursor() as conn:
            row = conn.execute("SELECT date FROM updates WHERE update_id=? AND author=?",
                               (update_id, me)).fetchone()
        if row is None:
            raise ValueError("进展不存在: %s" % update_id)
        kw: Dict[str, Any] = {}
        if "content" in body:
            kw["content_md"] = body["content"]
        if body.get("issue_key"):
            kw["issue_key"] = body["issue_key"]
        elif "issue_key" in body:
            kw["freeform"] = True
        result = store.correct_progress(update_id, me, **kw)
        date_ = body.get("date") or row["date"]
        return {"ok": True, "changed": result["changed"], "message": "进展已修订",
                "dashboard": dashboard_payload(me, date_)}

    @app.post("/api/tasks/{task_id}/complete")
    def api_complete_task(task_id: str, request: Request,
                          body: Dict[str, Any] = Body(default={}),
                          authorization: Optional[str] = Header(None)):
        me = guard(request, authorization)
        from . import service
        result = service.complete_task(task_id, author=me)
        return {"ok": True, **result,
                "dashboard": dashboard_payload(me, body.get("date") or store.today())}

    @app.post("/api/tasks/{task_id}/reopen")
    def api_reopen_task(task_id: str, request: Request,
                        body: Dict[str, Any] = Body(default={}),
                        authorization: Optional[str] = Header(None)):
        me = guard(request, authorization)
        from . import service
        result = service.reopen_task(task_id, author=me)
        return {"ok": True, **result,
                "dashboard": dashboard_payload(me, body.get("date") or store.today())}

    @app.post("/api/tasks/merge")
    def api_merge_tasks(request: Request, body: Dict[str, Any] = Body(...),
                        authorization: Optional[str] = Header(None)):
        me = guard(request, authorization)
        from . import service
        result = service.merge_tasks(body["source_task_id"], body["target_task_id"], author=me)
        return {"ok": True, **result,
                "dashboard": dashboard_payload(me, body.get("date") or store.today())}

    @app.get("/api/hidden")
    def api_hidden(request: Request, date: Optional[str] = None,
                   authorization: Optional[str] = Header(None)):
        me = guard(request, authorization)
        return {"items": hidden_entries(me, date)}

    @app.post("/api/restore")
    def api_restore(request: Request, body: Dict[str, Any] = Body(...),
                    authorization: Optional[str] = Header(None)):
        me = guard(request, authorization)
        with db.cursor() as conn:
            row = conn.execute("SELECT date FROM updates WHERE update_id=? AND author=?",
                               (body["update_id"], me)).fetchone()
            if row is None:
                raise ValueError("进展不存在: %s" % body["update_id"])
            n = conn.execute("UPDATE updates SET status='active',revision=revision+1"
                             " WHERE update_id=? AND author=?",
                             (body["update_id"], me)).rowcount
        date_ = body.get("date") or row["date"]
        return {"ok": bool(n), "message": "进展已恢复",
                "dashboard": dashboard_payload(me, date_)}

    @app.get("/api/timeline")
    def api_timeline(request: Request, days: int = Query(7, ge=1, le=90),
                     date: Optional[str] = None,
                     authorization: Optional[str] = Header(None)):
        me = guard(request, authorization)
        return {"groups": timeline(me, days, date)}

    @app.get("/api/health-stats")
    def api_health(request: Request, days: int = Query(14, ge=1, le=180),
                   authorization: Optional[str] = Header(None)):
        me = guard(request, authorization)
        return health(me, days)

    @app.get("/api/report")
    def api_report(request: Request, date: Optional[str] = None,
                   authorization: Optional[str] = Header(None)):
        me = guard(request, authorization)
        d = date or store.today()
        return {"date": d, **report_payload(me, d)}

    @app.post("/api/regenerate")
    def api_regenerate(request: Request, body: Dict[str, Any] = Body(default={}),
                       authorization: Optional[str] = Header(None)):
        me = guard(request, authorization)
        from . import service
        date_ = body.get("date") or store.today()
        if config.CLOUD:
            # 出日报要好几分钟，网页请求等不了——排队交给 lu2 上的后台程序
            from . import jobs
            job = jobs.enqueue(me, "regenerate", date_)
            return {"ok": True, "queued": True, "job": job,
                    "message": "已排队，几分钟后刷新就能看到新的日报",
                    "dashboard": dashboard_payload(me, date_)}
        result = service.end_of_day(date_, author=me, force=True)
        return {"ok": True, "result": result,
                "dashboard": dashboard_payload(me, date_)}

    # ---- 云端版：登录 ----
    from fastapi.responses import RedirectResponse

    def _page(name: str) -> str:
        from pathlib import Path
        html = (Path(__file__).resolve().parent / "presets" / name).read_text(encoding="utf-8")
        return html.replace("__DOMAIN__", config.ALLOWED_EMAIL_DOMAIN)

    def _cookie_kw() -> Dict[str, Any]:
        # 线上是 https，cookie 只走加密连接；本地调试是 http，不加这条否则浏览器不回传
        return {"httponly": True, "samesite": "lax",
                "secure": config.PUBLIC_URL.startswith("https://"), "path": "/"}

    def _signed_in(request: Request) -> Optional[str]:
        return accounts.resolve_session(request.cookies.get(SESSION_COOKIE, ""))

    def _cloud_only() -> None:
        if not config.CLOUD:
            raise HTTPException(404, "本机版没有登录")

    @app.get("/login", response_class=HTMLResponse)
    def login_page():
        _cloud_only()
        return _page("login.html")

    @app.get("/auth/login")
    def auth_login():
        from . import mobius_login
        _cloud_only()
        url, signed = mobius_login.start()
        resp = RedirectResponse(url, status_code=302)
        resp.set_cookie(OAUTH_COOKIE, signed, max_age=600, **_cookie_kw())
        return resp

    @app.get("/auth/callback")
    def auth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        from urllib.parse import quote
        from . import mobius_login
        _cloud_only()
        try:
            if error:
                raise accounts.AccessDenied("Mobius 那边取消了授权")
            author = mobius_login.finish(code, state, request.cookies.get(OAUTH_COOKIE, ""))
        except (accounts.AccessDenied, mobius_login.mobius.MobiusError) as exc:
            resp = RedirectResponse("/login?error=" + quote(str(exc)), status_code=302)
            resp.delete_cookie(OAUTH_COOKIE, path="/")
            return resp
        resp = RedirectResponse("/onboard", status_code=302)
        resp.set_cookie(SESSION_COOKIE, accounts.create_session(author),
                        max_age=int(accounts.SESSION_TTL.total_seconds()), **_cookie_kw())
        resp.delete_cookie(OAUTH_COOKIE, path="/")
        return resp

    @app.post("/auth/logout")
    def auth_logout(request: Request):
        _cloud_only()
        raw = request.cookies.get(SESSION_COOKIE, "")
        if raw:
            accounts.end_session(raw)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    @app.get("/onboard", response_class=HTMLResponse)
    def onboard_page(request: Request):
        _cloud_only()
        if not _signed_in(request):
            return RedirectResponse("/login", status_code=302)
        return _page("onboard.html")

    # ---- 云端版：我是谁、设置、token ----
    @app.get("/api/me")
    def api_me(request: Request, authorization: Optional[str] = Header(None)):
        from . import mobius_login
        me = guard(request, authorization)
        if not config.CLOUD:
            return {"author": me, "display_name": config.DISPLAY_NAME or me,
                    "is_admin": False, "cloud": False}
        user = accounts.get_user(me) or {}
        return {"author": me, "display_name": user.get("display_name", me),
                "is_admin": bool(user.get("is_admin")), "daily_time": user.get("daily_time"),
                "admin_request": accounts.pending_request(me),
                "mobius_connected": mobius_login.connected(me), "cloud": True}

    @app.post("/api/tokens")
    def api_issue_token(request: Request):
        # 只认浏览器登录，不认 agent token：拿着一个 token 不该能再生出更多 token
        _cloud_only()
        me = _signed_in(request)
        if not me:
            raise HTTPException(401, "请先登录")
        raw = accounts.issue_token(me)
        return {"token": raw, "ttl_days": accounts.TOKEN_TTL.days}

    @app.post("/api/settings")
    def api_settings(request: Request, body: Dict[str, Any] = Body(...),
                     authorization: Optional[str] = Header(None)):
        _cloud_only()
        me = guard(request, authorization)
        return {"ok": True, "daily_time": accounts.set_daily_time(me, body["daily_time"])}

    # ---- 云端版：扫描、agent、工作文件夹 ----
    @app.get("/api/scan/due")
    def api_scan_due(request: Request, authorization: Optional[str] = Header(None)):
        """本机小脚本每 15 分钟来问一次。只是个查表，不经过任何大模型。"""
        from . import cloudscan
        _cloud_only()
        me = guard(request, authorization)
        return cloudscan.due(me)

    @app.get("/api/agents")
    def api_agents(request: Request, authorization: Optional[str] = Header(None)):
        from . import cloudscan
        _cloud_only()
        me = guard(request, authorization)
        return {"items": cloudscan.agents(me)}

    @app.post("/api/agents/{agent}")
    def api_agent_toggle(agent: str, request: Request, body: Dict[str, Any] = Body(...),
                         authorization: Optional[str] = Header(None)):
        from . import cloudscan
        _cloud_only()
        me = guard(request, authorization)
        return cloudscan.set_scan_enabled(me, agent, bool(body.get("scan_enabled")))

    @app.get("/api/folders")
    def api_folders(request: Request, authorization: Optional[str] = Header(None)):
        from . import cloudscan
        _cloud_only()
        me = guard(request, authorization)
        return {"items": cloudscan.folders_of(me)}

    @app.post("/api/folders")
    def api_folders_set(request: Request, body: Dict[str, Any] = Body(...),
                        authorization: Optional[str] = Header(None)):
        from . import cloudscan
        _cloud_only()
        me = guard(request, authorization)
        return {"selected": cloudscan.set_selected(me, body.get("selected") or [])}

    @app.get("/install")
    @app.get("/install.md")
    def install_doc():
        """给 agent 看的安装说明。onboarding 那句 prompt 指向这里。"""
        from fastapi.responses import PlainTextResponse
        _cloud_only()
        text = _page("install.md").replace("__URL__", config.PUBLIC_URL)
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")

    # ---- 云端版：admin ----
    @app.post("/api/admin/request")
    def api_admin_request(request: Request, authorization: Optional[str] = Header(None)):
        _cloud_only()
        me = guard(request, authorization)
        return accounts.request_admin(me)

    @app.get("/api/admin/requests")
    def api_admin_requests(request: Request, authorization: Optional[str] = Header(None)):
        me = guard(request, authorization)
        admin_only(me)
        return {"items": accounts.admin_requests()}

    @app.post("/api/admin/requests/{request_id}")
    def api_admin_decide(request_id: str, request: Request, body: Dict[str, Any] = Body(...),
                         authorization: Optional[str] = Header(None)):
        me = guard(request, authorization)
        admin_only(me)
        return accounts.decide_admin_request(request_id, me, bool(body.get("approve")))

    @app.get("/api/admin/users")
    def api_admin_users(request: Request, authorization: Optional[str] = Header(None)):
        me = guard(request, authorization)
        admin_only(me)
        return {"items": accounts.list_users()}

    @app.get("/api/admin/dashboard")
    def api_admin_dashboard(request: Request, author: str, date: Optional[str] = None,
                            authorization: Optional[str] = Header(None)):
        """admin 看别人的日志。只读：看别人的面板不能顺手改别人的数据。"""
        me = guard(request, authorization)
        admin_only(me)
        if not accounts.get_user(author):
            raise HTTPException(404, "没有这个人: %s" % author)
        return dashboard_payload(author, date or store.today())

    @app.exception_handler(accounts.AccessDenied)
    async def access_denied_handler(_request: Request, exc: accounts.AccessDenied):
        return JSONResponse(status_code=403, content={"ok": False, "error": str(exc)})

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        from pathlib import Path
        if config.CLOUD and not _signed_in(request):
            return RedirectResponse("/login", status_code=302)
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
