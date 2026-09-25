"""HTTP adapters for the MCP protocol transports.

This module owns only transport/session state. Tool behavior stays in
``mcp_server`` and the same adapter is used by the local SSE and cloud HTTP
entry points.
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

    @app.get("/sse/")
    @app.get("/sse")
    async def sse_stream(request: Request, authorization: Optional[str] = Header(None)):
        if config.CLOUD:
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
                        yield ": keep-alive\n\n"
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

    def remember_session(sid: str, author: str, client_name: str) -> None:
        """Persist the client name because cloud requests may reach another instance."""
        try:
            with db.cursor() as conn:
                conn.execute(
                    "INSERT INTO mcp_sessions (session_id, author, client_name, created_at)"
                    " VALUES (?,?,?,?) ON CONFLICT (session_id) DO UPDATE SET"
                    " author=excluded.author, client_name=excluded.client_name,"
                    " created_at=excluded.created_at",
                    (sid, author, client_name, store.now_iso()))
        except Exception:  # noqa: BLE001  Persistence failure must not reject initialize.
            pass

    def session_client(sid: str, author: str) -> Optional[str]:
        """Look up only a session owned by this caller."""
        try:
            with db.cursor() as conn:
                row = conn.execute("SELECT client_name FROM mcp_sessions"
                                   " WHERE session_id=? AND author=?", (sid, author)).fetchone()
            return row["client_name"] if row else None
        except Exception:  # noqa: BLE001  Older local schemas may not have this table.
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
            # A request can land on another cloud instance; never fall back to the
            # process-wide default context, which belongs to the local author.
            context = mcp_server.MCPContext(session_id=sid or secrets.token_urlsafe(18))
            if sid:
                http_contexts[sid] = context
        if config.CLOUD:
            if method == "initialize":
                context.author = me
            else:
                context = dataclasses.replace(context, author=me)
                if context.client_name == "unknown-agent" and sid:
                    name = await run_in_threadpool(session_client, sid, me)
                    if name:
                        context = dataclasses.replace(context, client_name=name)
        resp = await run_in_threadpool(mcp_server.handle, msg, context)
        if config.CLOUD and method == "initialize" and sid:
            await run_in_threadpool(remember_session, sid, me, context.client_name)
        headers = {"Mcp-Session-Id": sid} if sid else {}
        if resp is None:
            return JSONResponse(status_code=202, content=None, headers=headers)
        return JSONResponse(resp, headers=headers)
