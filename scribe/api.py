"""接入层 / 读取层：REST。token 即身份。"""
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from . import __version__, auth, config, db, digest, llm, mobius, store

app = FastAPI(
    title="Scribe",
    version=__version__,
    description="Agent 优先的工作日志系统。提交入口只有 MCP / API，没有人工提交 UI。",
)


@app.on_event("startup")
def _startup() -> None:
    db.init()
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)


def identity(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    ident = auth.resolve(authorization)
    if not ident:
        raise HTTPException(401, "无效或缺失的 Bearer token")
    return ident


class ProgressIn(BaseModel):
    content_md: str = Field(..., description="做成了什么、进展到哪——不是对话内容")
    date: Optional[str] = Field(None, description="YYYY-MM-DD，缺省=服务器当天")
    source_agent: str = Field("manual", description="codex / claude-code / hermes / manual")
    session_id: Optional[str] = Field(None, description="哪个对话；配对时作为上下文先验")
    issue: Optional[str] = Field(None, description="强制指定 Mobius issue，如 AI-2541")
    task_id: Optional[str] = Field(None, description="强制挂到某个已有任务")
    meta: Optional[Dict[str, Any]] = None


@app.get("/healthz")
def healthz() -> Dict[str, Any]:
    return {
        "ok": True,
        "version": __version__,
        "db": str(config.DB_PATH),
        "llm_configured": config.llm_configured(),
        "llm_model": config.LLM_MODEL,
        "mobius_configured": mobius.configured(),
    }


@app.post("/progress", status_code=201)
def post_progress(p: ProgressIn, ident: Dict[str, Any] = Depends(identity)) -> Dict[str, Any]:
    try:
        res = store.record_progress(
            author=ident["author"], content_md=p.content_md, date=p.date,
            source_agent=p.source_agent, session_id=p.session_id,
            issue=p.issue, task_id=p.task_id, meta=p.meta,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    res["today_task_count"] = len(db.day_tasks(res["author"], res["date"]))
    return res


@app.get("/progress")
def get_progress(
    date: Optional[str] = Query(None),
    author: Optional[str] = Query(None),
    mine: bool = Query(False, description="只看自己——由 token 判定"),
    ident: Dict[str, Any] = Depends(identity),
) -> Dict[str, Any]:
    date = date or store.today()
    if mine:
        author = ident["author"]
    if author:
        tasks = db.day_tasks(author, date)
    else:
        tasks = []
        for a in sorted({u["author"] for u in db.list_updates(date=date)}):
            for t in db.day_tasks(a, date):
                t["author"] = a
                tasks.append(t)
    return {
        "date": date, "author": author,
        "task_count": len(tasks),
        "update_count": sum(len(t["updates"]) for t in tasks),
        "tasks": tasks,
    }


@app.get("/tasks")
def get_tasks(
    author: Optional[str] = Query(None),
    status: Optional[str] = Query("open"),
    mine: bool = Query(False),
    ident: Dict[str, Any] = Depends(identity),
) -> Dict[str, Any]:
    if mine:
        author = ident["author"]
    tasks = db.list_tasks(author=author, status=status)
    return {"count": len(tasks), "tasks": tasks}


@app.get("/tasks/{task_id}")
def get_task(task_id: str, ident: Dict[str, Any] = Depends(identity)) -> Dict[str, Any]:
    t = db.get_task(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    t["updates"] = db.list_updates(task_id=task_id)
    return t


@app.post("/tasks/{task_id}/close")
def post_close_task(task_id: str, ident: Dict[str, Any] = Depends(identity)) -> Dict[str, Any]:
    t = db.get_task(task_id)
    if not t or t["author"] != ident["author"]:
        raise HTTPException(404, "任务不存在")
    return store.close_task(task_id, ident["author"])


@app.post("/mobius/sync")
def post_mobius_sync(
    assignee: Optional[str] = Query(None),
    ident: Dict[str, Any] = Depends(identity),
) -> Dict[str, Any]:
    try:
        return mobius.sync(ident["author"], assignee or ident.get("mobius_assignee"))
    except mobius.MobiusError as exc:
        raise HTTPException(502, str(exc))


@app.get("/mobius/issues")
def get_mobius_issues(ident: Dict[str, Any] = Depends(identity)) -> Dict[str, Any]:
    rows = mobius.cached_issues(ident["author"])
    return {"count": len(rows), "synced_at": mobius.cache_age(ident["author"]),
            "issues": [{k: r[k] for k in ("issue_key", "title", "state", "url")} for r in rows]}


@app.get("/report/{date}")
def get_report(
    date: str,
    author: Optional[str] = Query(None),
    generate: bool = Query(False),
    force: bool = Query(False),
    ident: Dict[str, Any] = Depends(identity),
) -> Dict[str, Any]:
    who = author or ident["author"]
    daily = db.get_report(who, date, "daily")
    if force or (generate and not daily):
        run = digest.generate(who, date, force=force)
        return {"author": who, "date": date, "run": run,
                "daily": db.get_report(who, date, "daily"),
                "voice": db.get_report(who, date, "voice")}
    if not daily:
        raise HTTPException(404, "该日尚无整理稿；带 ?generate=true 触发生成")
    return {"author": who, "date": date, "daily": daily,
            "voice": db.get_report(who, date, "voice")}


@app.post("/report/{date}/generate")
def post_generate(
    date: str,
    author: Optional[str] = Query(None),
    force: bool = Query(False),
    all_authors: bool = Query(False),
    ident: Dict[str, Any] = Depends(identity),
) -> Dict[str, Any]:
    if all_authors:
        return {"date": date, "results": digest.run_all(date, force=force)}
    return {"date": date, "results": [digest.generate(author or ident["author"], date, force=force)]}


@app.get("/stats")
def get_stats(
    since: Optional[str] = Query(None),
    until: Optional[str] = Query(None),
    ident: Dict[str, Any] = Depends(identity),
) -> Dict[str, Any]:
    return store.stats(since=since, until=until)


@app.get("/day/{date}/{author}.md", response_class=PlainTextResponse)
def day_page(date: str, author: str, ident: Dict[str, Any] = Depends(identity)) -> str:
    """只读日页。读给人看，写永远只走 agent 入口。"""
    return store.day_page(author, date)
