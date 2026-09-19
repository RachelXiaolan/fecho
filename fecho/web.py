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

from . import __version__, accounts, config, db, mcp_server, quick_api, store

# 云端版的两个 cookie：登录后的会话、跳去 Mobius 登录路上的临时状态
SESSION_COOKIE = "fecho_session"
_EDITOR_TAG = ""   # 编辑器产物的缓存键，算一次就记住
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
    rank = {"task-continue": 0, "new-task": 1, "session-group": 1, "verified": 2,
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
                                  ("medium" if m in ("new-task", "session-group", "verified")
                                   else "high")})
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


def with_aliases(author: str, tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """给任务带上模型起的短名。网页上优先显示短名：自由任务的标题就是第一条进展的原文，
    像「已将 SSH 远程机器连接到 ChatGPT」这种，当任务名根本认不出是哪件事。"""
    from . import digest

    with db.cursor() as conn:
        known = {r["task_key"]: r["alias"] for r in conn.execute(
            "SELECT task_key, alias FROM task_aliases WHERE author=?", (author,)).fetchall()}
    for task in tasks:
        task["alias"] = known.get(digest.task_key(task))
    return tasks


def task_list(author: str) -> List[Dict[str, Any]]:
    # 被挪空收起来的自由任务（status=empty）不列出来：它们只是重判来回翻留下的空壳
    tasks = [t for t in db.list_tasks(author=author) if t["status"] != "empty"]
    with_aliases(author, tasks)
    for task in tasks:
        updates = db.list_updates(task_id=task["task_id"])
        task["updates"] = updates
        task["update_count"] = len(updates)
        task["last_progress"] = updates[-1]["content_md"] if updates else None
    return tasks


def _day_with_aliases(author: str, day: Dict[str, Any]) -> Dict[str, Any]:
    with_aliases(author, day.get("tasks") or [])
    return day


def report_payload(author: str, date: str) -> Dict[str, Any]:
    from . import digest, pto

    daily = db.get_report(author, date, "daily")
    voice = db.get_report(author, date, "voice")
    persona = digest.persona_for(author)      # 必须和出日报时同一份，否则指纹永远对不上
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
        "today": _day_with_aliases(author, service.day(author, date)),
        "review": {"items": review_queue(author, date)},
        "tasks": {"items": task_list(author)},
        "reports": report_payload(author, date),
        "hidden": {"items": hidden_entries(author, date)},
        "timeline": {"groups": timeline(author, 14, date)},
        "issues": {"items": mobius.cached_issues(author)},
        # 云端版没有「你电脑上的定时任务」这回事——出日报的是服务器上那个常驻程序。
        # 这里还会去连本机 8900 端口，放在云上是白等。
        "system": {"doctor": service.doctor(author), "scan_runs": scan_rows,
                   "automation": None if config.CLOUD else automation.status()},
        "filters": {"agents": agents, "ingestion_methods": ingestions,
                    "completion_statuses": ["done", "wip", "blocked", "unknown"]},
    }


# ---------- FastAPI ----------

def _editor_tag() -> str:
    """编辑器产物的缓存键。

    产物的文件名是固定的，不挂个会变的东西，浏览器就一直用缓存里的旧编辑器——
    改完代码看不到效果，同事升级了也还在跑老版本。用内容算：
    重新构建过就必定变，没变就让浏览器接着用缓存。
    """
    global _EDITOR_TAG
    if _EDITOR_TAG:
        return _EDITOR_TAG
    from pathlib import Path

    bundle = Path(__file__).resolve().parent / "presets" / "vendor" / "cm6" / "editor.min.js"
    tag = __version__
    try:
        import hashlib

        tag += "-" + hashlib.sha256(bundle.read_bytes()).hexdigest()[:8]
    except OSError:
        pass          # 产物不在就只用版本号，不值得为这个把页面弄挂
    _EDITOR_TAG = tag
    return tag


def build_app():
    from pathlib import Path

    from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles

    app = FastAPI(title="fecho", docs_url=None, redoc_url=None)

    # 日报编辑器的打包产物。源码在仓库的 editor/，产物已经提交，部署时不用装 node
    vendor_dir = Path(__file__).resolve().parent / "presets" / "vendor"
    if vendor_dir.is_dir():
        app.mount("/vendor", StaticFiles(directory=vendor_dir), name="vendor")

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


    # ---- 快速 API ----
    # 给「能发 HTTP、能读 OpenAPI，但装不了 MCP」的 agent 用：grok bot 那种跑在服务器上的
    # application、网页版 ChatGPT 的 Actions 之类。两条路，共用一把受限钥匙：
    #   updates   交原料——一条条进展，进库、归属，到点跟别的进展一起整理成日报
    #   reports   交成品——整篇写好的日报，等同于在网页上手写，自动版转入「历史版本」
    def quick_identity(authorization: Optional[str], api_key: Optional[str]) -> str:
        author = quick_api.resolve(quick_api.from_headers(authorization, api_key) or "")
        if not author:
            raise HTTPException(401, "快速 API key 无效或已被换掉。到网页上重新生成一把")
        return author

    def public_base(request: Request) -> str:
        return (config.PUBLIC_URL or str(request.base_url)).rstrip("/")

    def quick_date(value: Optional[str]) -> str:
        from datetime import datetime
        date_ = value or store.today()
        try:
            datetime.strptime(date_, "%Y-%m-%d")
        except (TypeError, ValueError):
            raise ValueError("date 必须是 YYYY-MM-DD")
        return date_


    # 给大模型自己读的接口清单。两条路的语义差别一定要写明白，不然它不知道该用哪个
    def quick_openapi_document(request: Request) -> Dict[str, Any]:
        auth = [{"bearerAuth": []}, {"apiKeyAuth": []}]
        str_ = {"type": "string"}
        return {
            "openapi": "3.0.3",
            "info": {
                "title": "Fecho Quick API",
                "version": __version__,
                "description": (
                    "把工作记录交到 Fecho。两条路，按你手上有什么选：\n\n"
                    "- **有零散进展** → `POST /api/quick-api/updates`。一条条交上来，"
                    "Fecho 会归属到任务，到点跟别的进展一起整理成日报。**追加，不会覆盖任何东西。**\n"
                    "- **已经写好整篇日报** → `POST /api/quick-api/reports`。"
                    "**会覆盖这一天显示的日报**，并把它标成「人写的」——"
                    "之后自动生成的版本不再覆盖它，只进「历史版本」。\n\n"
                    "拿不准就用 updates。\n\n"
                    "**钥匙是谁的，记录就进谁的账号。**多人共用一个 bot 时，每人要填自己那把。"
                    "重新生成凭据后旧钥匙立刻失效。"
                ),
            },
            "servers": [{"url": public_base(request)}],
            "security": auth,
            "paths": {
                "/api/quick-api/updates": {
                    "post": {
                        "operationId": "logProgress",
                        "summary": "交一条或几条工作进展（推荐）",
                        "description": (
                            "记的是「做成了什么、进展到哪」，一句能让人看懂结果的话就够，"
                            "不是对话内容。知道是哪个 issue 就填 issue，不确定就留空——"
                            "**归错了当天日报会跟着错，归不上只是多一个自由任务**。\n\n"
                            "重试安全：带上 event_key，同一个 key 重复交只记一次。"
                        ),
                        "security": auth,
                        "requestBody": {"required": True, "content": {"application/json": {"schema": {
                            "type": "object",
                            "properties": {
                                "date": dict(str_, format="date",
                                             description="这批进展算哪天，缺省为今天"),
                                "entries": {"type": "array", "items": {
                                    "type": "object", "required": ["content"],
                                    "properties": {
                                        "content": dict(str_, description="做成了什么、进展到哪"),
                                        "issue": dict(str_, description="确定是哪个 issue 就写，如 AI-2541；不确定留空"),
                                        "date": dict(str_, format="date", description="这条发生在哪天"),
                                        "completion_status": {"type": "string",
                                                              "enum": ["done", "wip", "blocked", "unknown"]},
                                        "kind": {"type": "string", "enum": ["progress", "pitfall", "decision"]},
                                        "agent": dict(str_, description="哪个 agent 交的，会显示在「今天」页上"),
                                        "event_key": dict(str_, description="去重用：同一个 key 重复交只记一次"),
                                        "freeform": {"type": "boolean",
                                                     "description": "明确不属于任何 issue"},
                                    }}},
                            },
                        }}}},
                        "responses": {"200": {"description": "已记下，返回每条归到了哪个任务"},
                                      "401": {"description": "钥匙无效或已被换掉"}},
                    },
                },
                "/api/quick-api/reports": {
                    "post": {
                        "operationId": "uploadDailyReport",
                        "summary": "上传整篇写好的日报（会覆盖当天显示的那篇）",
                        "description": (
                            "标准 Markdown。JSON 请求可以带 date，直接发 Markdown 正文时用 date 查询参数。"
                            "\n\n**注意：存完这一天的日报就算「人写的」**，到点自动生成的版本不再覆盖它，"
                            "只会放进「历史版本」。想换回自动版要人去网页上点「重新生成」。"
                        ),
                        "security": auth,
                        "parameters": [{"name": "date", "in": "query", "required": False,
                                        "description": "日报日期，缺省为今天",
                                        "schema": dict(str_, format="date")}],
                        "requestBody": {"required": True, "content": {
                            "text/markdown": {"schema": str_},
                            "application/json": {"schema": {
                                "type": "object", "required": ["content_md"],
                                "properties": {"date": dict(str_, format="date"),
                                               "content_md": dict(str_, description="标准 Markdown 原文")}}},
                        }},
                        "responses": {"200": {"description": "日报已保存"},
                                      "401": {"description": "钥匙无效或已被换掉"},
                                      "413": {"description": "超过 2 MB"}},
                    },
                },
                "/api/quick-api/resources": {
                    "post": {
                        "operationId": "uploadImage",
                        "summary": "上传图片，拿到能直接贴进正文的 Markdown",
                        "description": ("请求体直接放 PNG / JPEG / WebP / GIF 二进制。"
                                        "返回的 markdown 字段可以原样写进日报或进展正文。"),
                        "security": auth,
                        "parameters": [
                            {"name": "alt", "in": "query", "required": False,
                             "schema": dict(str_, default="图片")},
                            {"name": "width", "in": "query", "required": False,
                             "schema": {"type": "integer", "minimum": 1, "maximum": 4000, "default": 480}},
                        ],
                        "requestBody": {"required": True, "content": {
                            m: {"schema": dict(str_, format="binary")}
                            for m in ("image/png", "image/jpeg", "image/webp", "image/gif")}},
                        "responses": {"200": {"description": "已保存，返回 url 和 markdown"},
                                      "413": {"description": "超过 2 MB"}},
                    },
                },
                "/api/reports/images/{image_id}": {
                    "get": {
                        "operationId": "getImage",
                        "summary": "读回已上传的图片",
                        "description": "只有图片的主人和 admin 能打开。",
                        "security": auth,
                        "parameters": [{"name": "image_id", "in": "path", "required": True,
                                        "schema": str_}],
                        "responses": {"200": {"description": "图片文件"}},
                    },
                },
            },
            "components": {"securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "API key"},
                "apiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-API-Key"},
            }},
        }

    @app.get("/api/quick-api/openapi.json", include_in_schema=False)
    @app.get("/api/quick-api/open.json", include_in_schema=False)
    def quick_openapi(request: Request):
        return quick_openapi_document(request)

    @app.post("/api/quick-api/credentials", include_in_schema=False)
    def quick_credentials(request: Request, authorization: Optional[str] = Header(None)):
        """网页上点「快速 API」：签发一把新钥匙，上一把立刻失效。原文只在这个响应里出现一次。"""
        me = guard(request, authorization)
        raw = quick_api.issue(me)
        base = public_base(request)
        return {"ok": True, "base_url": base, "api_key": raw,
                "openapi_url": base + "/api/quick-api/openapi.json"}

    @app.post("/api/quick-api/updates", include_in_schema=False)
    async def quick_update_upload(request: Request,
                                  authorization: Optional[str] = Header(None),
                                  x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
        """交原料：一条条进展，走和随手记完全一样的归属、去重规则。"""
        me = quick_identity(authorization, x_api_key)
        try:
            payload = await request.json()
        except ValueError:
            raise ValueError("请求体要是 JSON")
        if isinstance(payload, dict) and "entries" in payload:
            entries, default_date = payload.get("entries"), payload.get("date")
        elif isinstance(payload, dict):
            entries, default_date = [payload], payload.get("date")   # 一条也能直接发对象
        else:
            raise ValueError("请求体要是对象，或者 {\"entries\": [...]}")
        if not isinstance(entries, list) or not entries:
            raise ValueError("entries 不能为空")
        if len(entries) > 200:
            raise HTTPException(413, "一次最多交 200 条")

        saved, skipped = [], 0
        for item in entries:
            if not isinstance(item, dict):
                raise ValueError("entries 里每一项都要是对象")
            content = (item.get("content") or "").strip()
            if not content:
                raise ValueError("每条进展都要有 content")
            if len(content) > 4000:
                raise ValueError("单条进展最多 4000 字")
            r = store.record_progress(
                me, content,
                date=quick_date(item.get("date") or default_date),
                source_agent=(item.get("agent") or "quick-api"),
                ingestion_method="quick-api",
                completion_status=item.get("completion_status") or "unknown",
                content_kind=item.get("kind") or "progress",
                issue=item.get("issue") or None,
                freeform=bool(item.get("freeform")),
                # 模型调 HTTP 会超时重试，同一个 key 重复交只记一次
                source_event_key=item.get("event_key") or None,
                unknown_issue_policy="freeform",
            )
            # 同一个 event_key 交过了：record_progress 判成 duplicate，不会重复写
            if r.get("verdict") == "duplicate":
                skipped += 1
            else:
                saved.append({"update_id": r["update_id"], "task": r["task"]["title"],
                              "issue": r["task"].get("issue_key")})
        return {"ok": True, "saved": len(saved), "skipped_duplicates": skipped, "updates": saved}

    @app.post("/api/quick-api/reports", include_in_schema=False)
    async def quick_report_upload(request: Request, date: Optional[str] = None,
                                  authorization: Optional[str] = Header(None),
                                  x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
        """交成品：整篇 Markdown 日报。和在网页上手写是同一件事——存完就算「人写的」，
        到点自动生成的版本不再覆盖它，只会进「历史版本」。"""
        me = quick_identity(authorization, x_api_key)
        media = (request.headers.get("content-type") or "").split(";", 1)[0].lower()
        if media == "application/json":
            try:
                payload = await request.json()
            except ValueError:
                raise ValueError("JSON 请求体格式不正确")
            if not isinstance(payload, dict):
                raise ValueError("JSON 请求体必须是对象")
            content = payload.get("content_md", payload.get("markdown"))
            date = payload.get("date") or date
        else:
            raw = await request.body()
            if len(raw) > 2_000_000:
                raise HTTPException(413, "日报太大了，最多 2 MB")
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError("日报必须是 UTF-8 Markdown")
        if not isinstance(content, str):
            raise ValueError("请提供 content_md Markdown 原文")
        if len(content.encode("utf-8")) > 2_000_000:
            raise HTTPException(413, "日报太大了，最多 2 MB")
        date_ = quick_date(date)
        from . import digest, service
        saved = digest.save_human_edit(me, date_, content)
        follow = service.after_human_edit(me, date_) if saved["status"] == "saved" else {}
        return {"ok": True, "date": date_, "saved": saved, "follow_up": follow}

    @app.post("/api/quick-api/resources", include_in_schema=False)
    async def quick_resource_upload(request: Request, alt: str = Query("图片", max_length=100),
                                    width: int = Query(480, ge=1, le=4000),
                                    authorization: Optional[str] = Header(None),
                                    x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
        """传图，返回一段能直接贴进日报或进展正文的 Markdown。"""
        import base64
        from . import report_images
        me = quick_identity(authorization, x_api_key)
        media = (request.headers.get("content-type") or "").split(";", 1)[0].lower()
        if media == "application/json":
            try:
                payload = await request.json()
            except ValueError:
                raise ValueError("JSON 请求体格式不正确")
            if not isinstance(payload, dict):
                raise ValueError("JSON 请求体必须是对象")
            saved = report_images.save(me, payload.get("data"))
        else:
            raw = await request.body()
            if len(raw) > report_images.MAX_IMAGE_BYTES:
                raise HTTPException(413, "图片太大了（最多 2 MB）")
            saved = report_images.save(me, base64.b64encode(raw).decode("ascii"))
        url = public_base(request) + "/" + saved["url"].lstrip("/")
        safe_alt = alt.replace("]", "\\]").replace("\n", " ").strip() or "图片"
        return {"ok": True, "image_id": saved["image_id"], "url": url,
                "markdown": "![%s|%d](%s)" % (safe_alt, width, url)}

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
    def _remember_session(sid: str, author: str, client_name: str) -> None:
        """agent 连上来时报的名字记进库：云端版的下一个请求可能落到别的实例上。"""
        try:
            with db.cursor() as conn:
                conn.execute(
                    "INSERT INTO mcp_sessions (session_id, author, client_name, created_at)"
                    " VALUES (?,?,?,?) ON CONFLICT (session_id) DO UPDATE SET"
                    " author=excluded.author, client_name=excluded.client_name,"
                    " created_at=excluded.created_at",
                    (sid, author, client_name, store.now_iso()))
        except Exception:                           # noqa: BLE001 记不下来不该挡住连接
            pass

    def _session_client(sid: str, author: str) -> Optional[str]:
        """按会话号查回 agent 名字。只认同一个人的会话：拿着别人的会话号查不到别人的 agent。"""
        try:
            with db.cursor() as conn:
                row = conn.execute("SELECT client_name FROM mcp_sessions"
                                   " WHERE session_id=? AND author=?", (sid, author)).fetchone()
            return row["client_name"] if row else None
        except Exception:                           # noqa: BLE001 表还没建好时照常工作
            return None

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
                if context.client_name == "unknown-agent" and sid:
                    # 这个实例没见过这个会话的 initialize：去库里找回是哪个 agent，
                    # 否则记下的进展来源会是 unknown-agent
                    name = _session_client(sid, me)
                    if name:
                        context = dataclasses.replace(context, client_name=name)
        resp = mcp_server.handle(msg, context)
        if config.CLOUD and method == "initialize" and sid:
            _remember_session(sid, me, context.client_name)
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
        # 这天一条记录都没有，排队也生成不出东西——直接说清楚，别让人以为几分钟后会有日报
        if not overview(me, date_)["updates"]:
            return {"ok": True, "queued": False, "empty": True,
                    "message": "这天没有任何记录，生成不出日报",
                    "dashboard": dashboard_payload(me, date_)}
        if config.CLOUD:
            # 出日报要好几分钟，网页请求等不了——排队交给 lu2 上的后台程序
            from . import jobs
            job = jobs.enqueue(me, "regenerate", date_)
            return {"ok": True, "queued": True, "job": job,
                    "message": "已排队，几分钟后刷新就能看到新的日报",
                    "dashboard": dashboard_payload(me, date_)}
        # 人点了「重新生成」：明确要覆盖，改过的版本会留在历史版本里
        result = service.end_of_day(date_, author=me, force=True, keep_human=False)
        return {"ok": True, "result": result,
                "dashboard": dashboard_payload(me, date_)}

    @app.post("/api/reports/daily")
    def api_edit_daily(request: Request, body: Dict[str, Any] = Body(...),
                       authorization: Optional[str] = Header(None)):
        """人亲手改日报。只能改自己的：admin 看别人的面板是只读的，这里也只认 me。"""
        me = guard(request, authorization)
        from . import digest, service
        date_ = body.get("date") or store.today()
        saved = digest.save_human_edit(me, date_, body.get("content_md") or "")
        follow = service.after_human_edit(me, date_) if saved["status"] == "saved" else {}
        return {"ok": True, "saved": saved, "follow_up": follow,
                "dashboard": dashboard_payload(me, date_)}

    @app.post("/api/reports/images")
    def api_report_image_upload(request: Request, body: Dict[str, Any] = Body(...),
                                authorization: Optional[str] = Header(None)):
        """日报里贴图：先传图拿地址，网页再把地址写进日报。一次一张。"""
        from . import report_images
        me = guard(request, authorization)
        return {"ok": True, **report_images.save(me, body.get("data"))}

    @app.get("/api/reports/images/{image_id}")
    def api_report_image(image_id: str, request: Request,
                         authorization: Optional[str] = Header(None),
                         x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
        from fastapi.responses import Response
        from . import report_images
        # 快速 API 传上来的图，模型自己也要能读回去验一眼
        bearer = (authorization or "").strip()
        if bearer.lower().startswith("bearer "):
            bearer = bearer[7:].strip()
        if x_api_key or bearer.startswith(quick_api.TOKEN_PREFIX):
            me = quick_identity(authorization, x_api_key)
        else:
            me = guard(request, authorization)
        found = report_images.image_for(image_id, me)
        if not found:
            raise HTTPException(404, "没有这张图")
        mime, raw = found
        return Response(raw, media_type=mime, headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "private, max-age=86400"})

    @app.get("/api/style")
    def api_style(request: Request, authorization: Optional[str] = Header(None)):
        """我的写作偏好。每个人只能看自己的。"""
        me = guard(request, authorization)
        from . import style
        return style.get(me)

    @app.post("/api/style")
    def api_style_save(request: Request, body: Dict[str, Any] = Body(...),
                       authorization: Optional[str] = Header(None)):
        me = guard(request, authorization)
        from . import style
        return style.save(me, body.get("content_md") or "")

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

    @app.post("/api/scan/submit")
    def api_scan_submit(request: Request, body: Dict[str, Any] = Body(...),
                        authorization: Optional[str] = Header(None)):
        """本机脚本交扫描结果。和 MCP 的 submit_scan 进的是同一个函数，只是走普通 HTTP：
        脚本自己就能把结果和失败原因交上来，不必经过 agent——提炼到一半 agent 崩了，
        服务器照样知道这天没扫成、半小时后要重试。"""
        from . import cloudscan
        _cloud_only()
        me = guard(request, authorization)
        entries = body.get("entries") or []
        if not isinstance(entries, list):
            raise ValueError("entries 必须是列表")
        return cloudscan.submit(me, entries, date=body.get("date"),
                                finished=bool(body.get("finished")), error=body.get("error"),
                                producer="local-script")

    @app.post("/api/folders/report")
    def api_folders_report(request: Request, body: Dict[str, Any] = Body(...),
                           authorization: Optional[str] = Header(None)):
        """本机脚本上报「用 agent 干过活的文件夹」。只有路径，新上报的默认不勾。"""
        from . import cloudscan
        _cloud_only()
        me = guard(request, authorization)
        return cloudscan.report_folders(me, body.get("folders") or [])

    # 本机要下载的文件。里面没有任何密钥，不用登录也能下——装的时候 token 还在命令行参数里。
    _LOCAL_FILES = {
        "fecho_local.py": ("local/fecho_local.py", "text/x-python"),
        "SKILL.md": ("skill/SKILL.md", "text/markdown"),
    }

    @app.get("/local/{name}")
    def local_file(name: str):
        from fastapi.responses import PlainTextResponse
        _cloud_only()
        if name not in _LOCAL_FILES:
            raise HTTPException(404, "没有这个文件")
        rel, kind = _LOCAL_FILES[name]
        text = _page(rel).replace("__URL__", config.PUBLIC_URL)
        return PlainTextResponse(text, media_type=kind + "; charset=utf-8")

    @app.get("/guide", response_class=HTMLResponse)
    def guide_page():
        """给同事的接入与验收指南。不用登录就能看：还没装的人也得先读到它。"""
        return _page("guide.html")

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

    # ---- 云端版：反馈 ----
    @app.post("/api/feedback")
    def api_feedback_submit(request: Request, body: Dict[str, Any] = Body(...),
                            authorization: Optional[str] = Header(None)):
        from . import feedback
        _cloud_only()
        me = guard(request, authorization)
        images = body.get("images") or []
        if not isinstance(images, list):
            raise ValueError("images 必须是列表")
        return {"ok": True, "item": feedback.submit(me, body.get("body") or "", images,
                                                    body.get("page"))}

    @app.get("/api/feedback")
    def api_feedback_mine(request: Request, authorization: Optional[str] = Header(None)):
        from . import feedback
        _cloud_only()
        me = guard(request, authorization)
        return {"items": feedback.list_mine(me)}

    @app.get("/api/feedback/images/{image_id}")
    def api_feedback_image(image_id: str, request: Request,
                           authorization: Optional[str] = Header(None)):
        from fastapi.responses import Response
        from . import feedback
        _cloud_only()
        me = guard(request, authorization)
        found = feedback.image_for(image_id, me)
        if not found:
            raise HTTPException(404, "没有这张图")
        mime, raw = found
        # nosniff + 不允许图片里跑任何东西：就算混进了奇怪的文件，浏览器也只当图片看
        return Response(raw, media_type=mime, headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "private, max-age=86400"})

    @app.get("/api/admin/feedback")
    def api_admin_feedback(request: Request, authorization: Optional[str] = Header(None)):
        from . import feedback
        me = guard(request, authorization)
        admin_only(me)
        return {"items": feedback.list_all()}

    @app.post("/api/admin/feedback/{feedback_id}")
    def api_admin_feedback_status(feedback_id: str, request: Request,
                                  body: Dict[str, Any] = Body(...),
                                  authorization: Optional[str] = Header(None)):
        from . import feedback
        me = guard(request, authorization)
        admin_only(me)
        return {"ok": True, "item": feedback.set_status(feedback_id, body.get("status") or "", me)}

    @app.exception_handler(accounts.AccessDenied)
    async def access_denied_handler(_request: Request, exc: accounts.AccessDenied):
        return JSONResponse(status_code=403, content={"ok": False, "error": str(exc)})

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        from pathlib import Path
        if config.CLOUD and not _signed_in(request):
            return RedirectResponse("/login", status_code=302)
        html = (Path(__file__).resolve().parent / "presets" / "dashboard.html").read_text(encoding="utf-8")
        return html.replace("/vendor/cm6/editor.min.js",
                            "/vendor/cm6/editor.min.js?v=" + _editor_tag())

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
