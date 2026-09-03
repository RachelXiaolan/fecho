"""业务层：MCP 工具和 REST 接口共用的一套函数。

MCP server 直接调这里（单进程，装完就能跑）；REST 服务也调这里（团队共享部署时用）。
两条路同一套逻辑，不会跑偏。
"""
from datetime import date as _date, timedelta
from typing import Any, Dict, List, Optional

from . import config, db, digest, mobius, store


def whoami() -> str:
    return config.AUTHOR


def record(content: str, author: Optional[str] = None, **kw) -> Dict[str, Any]:
    res = store.record_progress(author or whoami(), content, **kw)
    res["today_task_count"] = len(db.day_tasks(res["author"], res["date"]))
    return res


def day(author: Optional[str] = None, date: Optional[str] = None) -> Dict[str, Any]:
    date = date or store.today()
    if author:
        tasks = db.day_tasks(author, date)
    else:
        tasks = []
        for a in sorted({u["author"] for u in db.list_updates(date=date)}):
            for t in db.day_tasks(a, date):
                t["author"] = a
                tasks.append(t)
    return {"date": date, "author": author, "task_count": len(tasks),
            "update_count": sum(len(t["updates"]) for t in tasks), "tasks": tasks}


def open_tasks(author: Optional[str] = None) -> List[Dict[str, Any]]:
    tasks = db.list_tasks(author=author or whoami(), status="open")
    for t in tasks:
        ups = db.list_updates(task_id=t["task_id"])
        t["last_progress"] = ups[-1]["content_md"] if ups else None
        t["update_count"] = len(ups)
    return tasks


def report(date: str, author: Optional[str] = None) -> Dict[str, Any]:
    who = author or whoami()
    return {"author": who, "date": date,
            "daily": db.get_report(who, date, "daily"),
            "voice": db.get_report(who, date, "voice")}


def end_of_day(date: Optional[str] = None, author: Optional[str] = None,
               force: bool = False) -> Dict[str, Any]:
    who = author or whoami()
    when = date or store.today()
    r = digest.generate(who, when, force=force)
    if r["status"] in ("generated", "skipped", "pto-exempt"):
        # 无论这次是不是重新生成，都把本地当前的成品同步给团队 collector；
        # 没配 collector 或推送失败都不影响本地产物，只是团队视图暂时看不到这份。
        from . import push

        r["team_push"] = push.push(who, when)
    return r


def team_digest(date: Optional[str] = None, author: Optional[str] = None) -> Dict[str, Any]:
    from . import push

    return push.team_digest(date or store.today(), author)


def sync_issues(assignee: Optional[str] = None, author: Optional[str] = None) -> Dict[str, Any]:
    from . import oauth

    oauth.refresh_if_needed()          # token 快过期就先续，别让用户看见一次失败
    config.reload_module()
    return mobius.sync(author or whoami(), assignee or config.MOBIUS_ASSIGNEE)


def catch_up(date: Optional[str] = None, author: Optional[str] = None) -> Dict[str, Any]:
    who = author or whoami()
    day_ = date or (_date.today() - timedelta(days=1)).isoformat()
    return {"date": day_, "report": report(day_, who), "open_tasks": open_tasks(who)}


def doctor() -> Dict[str, Any]:
    """装完之后 agent 该看的第一眼：什么配好了，什么还缺，缺的怎么补。"""
    from . import llm, oauth

    cfg = config.redacted()
    checks: List[Dict[str, Any]] = []

    checks.append({
        "name": "存储", "ok": True,
        "detail": "SQLite: %s" % cfg["db"],
    })

    mob_ok = config.mobius_configured()
    st = oauth.status()
    checks.append({
        "name": "Mobius 连接",
        "ok": mob_ok,
        "detail": ("已连接（%s）" % st.get("auth")) if mob_ok else "未连接",
        "fix": None if mob_ok else
        "调用 mobius_login 工具，会开浏览器让你授权；或 fecho login --token <你的 token>",
    })

    n_issues = len(mobius.cached_issues(cfg["author"])) if mob_ok else 0
    checks.append({
        "name": "issue 缓存", "ok": n_issues > 0,
        "detail": "%d 个在办 issue（同步于 %s）" % (n_issues, mobius.cache_age(cfg["author"]))
        if n_issues else "空",
        "fix": None if n_issues else "调用 sync_issues 工具拉一次",
    })

    llm_ok = config.llm_configured()
    checks.append({
        "name": "LLM（出日报用）", "ok": llm_ok,
        "detail": "%s / %s" % (cfg["llm"]["base_url"], cfg["llm"]["model"])
        if llm_ok else "未配置",
        "fix": None if llm_ok else
        "fecho setup --llm-url <url> --llm-key <key> --llm-model <model>；"
        "不配也能记流水，只是日终出的是兜底稿",
    })

    team_ok = bool(cfg["team"]["collector_url"])
    checks.append({
        "name": "团队协作（可选）",
        "ok": team_ok,
        "detail": "已配置 %s" % cfg["team"]["collector_url"] if team_ok
        else "未配置——日报只留在本机，不影响个人使用",
        "fix": None if team_ok else
        "团队部署了共享 collector 后：fecho setup --collector-url <url> --collector-token <token>",
    })

    return {
        "config": cfg,
        "checks": checks,
        "ready_to_log": True,
        "ready_to_match": mob_ok and n_issues > 0,
        "ready_to_report": llm_ok,
        "ready_for_team": team_ok,
    }
