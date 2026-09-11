"""Mobius 客户端 + 本地 issue 缓存。

Mobius 对外是一个 HTTP MCP 端点（JSON-RPC over HTTP），不是普通 REST，
所以这里直接说 JSON-RPC，不引任何 SDK。

缓存的意义：配对发生在每一次 log_work 上，不能每次都去问 Mobius。
定时同步一次，配对全在本地做，Mobius 挂了也不影响记日志。
"""
import json
from typing import Any, Dict, List, Optional

import httpx

from . import config, db, store


class MobiusError(RuntimeError):
    pass



# 已有同一条就覆盖。写成 ON CONFLICT 而不是 INSERT OR REPLACE：
# 后者只有 SQLite 认，前者 SQLite 和 Postgres 都认。
_UPSERT_ISSUE = (
    " ON CONFLICT (issue_key, author) DO UPDATE SET"
    " title=excluded.title, state=excluded.state, url=excluded.url,"
    " updated_at=excluded.updated_at, synced_at=excluded.synced_at, raw=excluded.raw")

def configured() -> bool:
    return bool(config.MOBIUS_URL and config.MOBIUS_TOKEN)


def _rpc(method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not configured():
        raise MobiusError("未配置 Mobius：需要 FECHO_MOBIUS_URL 与 FECHO_MOBIUS_TOKEN")
    body: Dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    headers = {
        "Authorization": "Bearer %s" % config.MOBIUS_TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    try:
        r = httpx.post(config.MOBIUS_URL, json=body, headers=headers, timeout=60)
    except httpx.HTTPError as exc:
        raise MobiusError("Mobius 请求失败: %s" % exc) from exc
    if r.status_code >= 400:
        raise MobiusError("Mobius %d: %s" % (r.status_code, r.text[:300]))
    data = r.json()
    if "error" in data:
        raise MobiusError("Mobius: %s" % str(data["error"])[:300])
    return data.get("result", {})


def fetch_open_issues(assignee: str) -> List[Dict[str, Any]]:
    """拉某人名下"在办 + 待办"的 issue —— 配对只可能配到这些上面。"""
    out: Dict[str, Dict[str, Any]] = {}
    for state in ("started", "unstarted"):
        res = _rpc("tools/call", {
            "name": "list_issues",
            "arguments": {"assigneeEmail": assignee, "stateType": state, "limit": 100},
        })
        content = res.get("content") or []
        if not content:
            continue
        payload = json.loads(content[0]["text"])
        for issue in payload.get("issues", []):
            out[issue["identifier"]] = issue
    return list(out.values())


def fetch_issue(author: str, identifier: str) -> Dict[str, Any]:
    """Resolve one historical issue and retain it in the local validation cache."""
    res = _rpc("tools/call", {
        "name": "get_issue", "arguments": {"identifier": identifier}})
    content = res.get("content") or []
    if not content:
        raise MobiusError("Mobius 没有返回 issue %s" % identifier)
    try:
        payload = json.loads(content[0]["text"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MobiusError("Mobius 返回的 issue 数据无法解析") from exc
    issue = payload.get("issue", payload)
    if issue.get("identifier") != identifier:
        raise MobiusError("Mobius 没有找到 issue %s" % identifier)
    now = store.now_iso()
    with db.cursor() as conn:
        conn.execute(
            "INSERT INTO mobius_issues"
            " (issue_key, author, title, state, url, updated_at, synced_at, raw)"
            " VALUES (?,?,?,?,?,?,?,?)" + _UPSERT_ISSUE,
            (identifier, author, issue.get("title", ""), issue.get("state", ""),
             issue.get("url", ""), issue.get("updatedAt", ""), now,
             json.dumps(issue, ensure_ascii=False)),
        )
    return issue


def sync(author: str, assignee: Optional[str] = None) -> Dict[str, Any]:
    """把某人的 issue 同步进本地缓存。cron 或开工时调。"""
    assignee = assignee or config.MOBIUS_ASSIGNEE
    if not assignee:
        raise MobiusError("不知道要同步谁的 issue：设置 FECHO_MOBIUS_ASSIGNEE 或传 assignee")
    issues = fetch_open_issues(assignee)
    now = store.now_iso()
    with db.cursor() as conn:
        conn.execute("DELETE FROM mobius_issues WHERE author=?", (author,))
        for i in issues:
            conn.execute(
                "INSERT INTO mobius_issues"
                " (issue_key, author, title, state, url, updated_at, synced_at, raw)"
                " VALUES (?,?,?,?,?,?,?,?)" + _UPSERT_ISSUE,
                (i["identifier"], author, i.get("title", ""), i.get("state", ""),
                 i.get("url", ""), i.get("updatedAt", ""), now,
                 json.dumps(i, ensure_ascii=False)),
            )
    return {"author": author, "assignee": assignee, "count": len(issues), "synced_at": now}


def cached_issues(author: str) -> List[Dict[str, Any]]:
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT * FROM mobius_issues WHERE author=? ORDER BY updated_at DESC", (author,)
        ).fetchall()
    return [dict(r) for r in rows]


def cache_age(author: str) -> Optional[str]:
    rows = cached_issues(author)
    return rows[0]["synced_at"] if rows else None
