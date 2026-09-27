"""MCP 的两种 HTTP 传输：SSE（本机版给 ChatGPT 用）和 Streamable HTTP（/mcp）。

这里只管连接和会话；工具本身在 mcp_server。从 web.py 拆出来（Tony 的 PR #4），
顺带把认人、查库、跑工具这些同步操作挪进线程池——以前直接在异步路由里做，
一次慢查询就把所有连接都卡住。
"""
import asyncio
import dataclasses
import json
import secrets
from typing import Any, Callable, Dict, Optional

from fastapi import Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from . import config, db, mcp_server, store


def install_mcp_routes(app: Any, guard: Callable[..., str]) -> None:
    """Register SSE and Streamable HTTP routes on the application."""
    sessions: Dict[str, Dict[str, Any]] = {}
    http_contexts: Dict[str, mcp_server.MCPContext] = {}

    # ---- MCP over SSE ----
    # 老一档的 MCP 传输，但 ChatGPT 现在要的就是它，且 URL 必须以 /sse/ 结尾。
    # 握手是两条腿：GET 开一条长连接，第一个事件告诉对方「消息往哪 POST」；
    # 之后每次 POST 的响应不从 POST 返回，而是顺着那条长连接推回去。
    @app.get("/sse/")
    @app.get("/sse")
    async def sse_stream(request: Request, authorization: Optional[str] = Header(None)):
        if config.CLOUD:
            # 长连接 + 会话存内存，放到按请求运行的平台上撑不住。云端版只走 /mcp。
            raise HTTPException(404, "云端版请用 %s/mcp" % config.PUBLIC_URL)
        await run_in_threadpool(guard, request, authorization)
        sid = secrets.token_urlsafe(16)
        queue: asyncio.Queue = asyncio.Queue()
        sessions[sid] = {"queue": queue,
                         "context": mcp_server.MCPContext(session_id=sid)}

        async def gen():
            yield "event: endpoint\ndata: /sse/messages?session_id=%s\n\n" % sid
            try:
                while True:
                    try:
                        msg = await asyncio.wait_for(queue.get(), timeout=15)
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
        await run_in_threadpool(guard, request, authorization)
        session = sessions.get(session_id)
        if session is None:
            raise HTTPException(404, "会话不存在或已断开，请重新连接 /sse/")
        msg = await request.json()
        resp = await run_in_threadpool(mcp_server.handle, msg, session["context"])
        if resp is not None:
            await session["queue"].put(resp)
        return JSONResponse(status_code=202, content={"ok": True})

    # ---- MCP over Streamable HTTP ----
    def remember_session(sid: str, author: str, client_name: str) -> None:
        """agent 连上来时报的名字记进库：云端版的下一个请求可能落到别的实例上。"""
        try:
            with db.cursor() as conn:
                conn.execute(
                    "INSERT INTO mcp_sessions (session_id, author, client_name, created_at)"
                    " VALUES (?,?,?,?) ON CONFLICT (session_id) DO UPDATE SET"
                    " author=excluded.author, client_name=excluded.client_name,"
                    " created_at=excluded.created_at",
                    (sid, author, client_name, store.now_iso()))
        except Exception:  # noqa: BLE001 记不下来不该挡住连接
            pass

    def session_client(sid: str, author: str) -> Optional[str]:
        """按会话号查回 agent 名字。只认同一个人的会话：拿着别人的会话号查不到别人的 agent。"""
        try:
            with db.cursor() as conn:
                row = conn.execute("SELECT client_name FROM mcp_sessions"
                                   " WHERE session_id=? AND author=?", (sid, author)).fetchone()
            return row["client_name"] if row else None
        except Exception:  # noqa: BLE001 表还没建好时照常工作
            return None

    @app.post("/mcp")
    async def mcp_endpoint(request: Request,
                           authorization: Optional[str] = Header(None),
                           mcp_session_id: Optional[str] = Header(None,
                                                                  alias="Mcp-Session-Id")):
        me = await run_in_threadpool(guard, request, authorization)
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
                    name = await run_in_threadpool(session_client, sid, me)
                    if name:
                        context = dataclasses.replace(context, client_name=name)
        resp = await run_in_threadpool(mcp_server.handle, msg, context)
        if config.CLOUD and method == "initialize" and sid:
            await run_in_threadpool(remember_session, sid, me, context.client_name)
        headers = {"Mcp-Session-Id": sid} if sid else {}
        if resp is None:                       # 通知类消息没有响应体
            return JSONResponse(status_code=202, content=None, headers=headers)
        return JSONResponse(resp, headers=headers)
