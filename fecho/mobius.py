"""Mobius 客户端 + 本地 issue 缓存。

Mobius 对外是一个 HTTP MCP 端点（JSON-RPC over HTTP），不是普通 REST，
所以这里直接说 JSON-RPC，不引任何 SDK。

缓存的意义：配对发生在每一次 log_work 上，不能每次都去问 Mobius。
定时同步一次，配对全在本地做，Mobius 挂了也不影响记日志。
"""
import json
from typing import Any, Dict, List, Optional

import httpx

from . import config, db, match, store


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


def endpoint() -> str:
    return config.MOBIUS_URL or "https://mobius.feedmob.com/api/mcp"


def _rpc(method: str, params: Optional[Dict[str, Any]] = None,
         token: Optional[str] = None) -> Dict[str, Any]:
    """token 不传就用本机配置里那一个（本机版）；云端版每个人传各自的。"""
    if token is None and not configured():
        raise MobiusError("未配置 Mobius：需要 FECHO_MOBIUS_URL 与 FECHO_MOBIUS_TOKEN")
    body: Dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    headers = {
        "Authorization": "Bearer %s" % (token or config.MOBIUS_TOKEN),
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    try:
        r = httpx.post(endpoint() if token else config.MOBIUS_URL,
                       json=body, headers=headers, timeout=60)
    except httpx.HTTPError as exc:
        raise MobiusError("Mobius 请求失败: %s" % exc) from exc
    if r.status_code >= 400:
        raise MobiusError("Mobius %d: %s" % (r.status_code, r.text[:300]))
    data = r.json()
    if "error" in data:
        raise MobiusError("Mobius: %s" % str(data["error"])[:300])
    return data.get("result", {})


# 同步范围里的状态分组。backlog 以前不同步——排进 backlog 的票照样会有人在做
_OPEN_BUCKETS = ("backlog", "unstarted", "started")
_CLOSED_BUCKETS = ("completed", "canceled")
RECENTLY_CLOSED_DAYS = 7         # 关了几天之内的还同步：收尾的进展要能归上去
TOUCHED_DAYS = 30                # Fecho 里最近这么多天归过进展的 issue 算「参与中」
TOUCHED_MAX = 40                 # 这类要一个个查，封个顶
# 查单个 issue 时只拿得到状态名，拿不到分组。分组靠同一轮列表查询学来，学不到再按名字猜
_CLOSED_NAMES = {"done", "completed", "canceled", "cancelled", "duplicate", "closed"}


def _list_issues(args: Dict[str, Any], token: Optional[str], pages: int = 5) -> List[Dict[str, Any]]:
    """list_issues 翻页拉全。一次最多 100 条，封顶几页，免得一个人名下几千张票拖死同步。"""
    out: List[Dict[str, Any]] = []
    cursor = None
    for _ in range(pages):
        a = dict(args, limit=100)
        if cursor:
            a["cursor"] = cursor
        res = _rpc("tools/call", {"name": "list_issues", "arguments": a}, token=token)
        content = res.get("content") or []
        if not content:
            break
        payload = json.loads(content[0]["text"])
        out.extend(payload.get("issues", []))
        cursor = payload.get("nextCursor")
        if not payload.get("hasMore") or not cursor:
            break
    return out


def fetch_open_issues(assignee: str, token: Optional[str] = None) -> List[Dict[str, Any]]:
    """某人名下还开着的 issue（含 backlog），每条带上它的状态分组 `_bucket`。"""
    out: Dict[str, Dict[str, Any]] = {}
    for bucket in _OPEN_BUCKETS:
        for issue in _list_issues({"assigneeEmail": assignee, "stateType": bucket}, token):
            out[issue["identifier"]] = dict(issue, _bucket=bucket)
    return list(out.values())


DESC_CHARS = 160      # 描述摘要多长：够模型认出是哪件事，又不把候选列表撑爆
DESC_FETCH_MAX = 40   # 一轮同步最多为补描述查几次


def snippet(description: Optional[str]) -> str:
    """描述压成一句摘要：去掉 HTML 注释标记、标题行、Markdown 符号，合并空白。

    标题常常是黑话（「9.18 RSI 带练项目自进化检查节点」），光看标题认不出
    Bug Hunter 就是它；描述里通常有人话。
    """
    import re
    text = re.sub(r"<!--.*?-->", " ", description or "", flags=re.S)
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    text = re.sub(r"[*_`>|\[\]]+", " ", " ".join(lines))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:DESC_CHARS]


def _days_ago_iso(days: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _touched_keys(author: str) -> List[str]:
    """这个人在 Fecho 里最近归过进展的 issue，新的在前。

    Mobius 没有「参与中」这种查询，而人实际在做的票常常不在自己名下：
    别人的票帮着做、没人认领的票先做了、刚关掉还在收尾。归过进展就说明在参与。
    """
    since = _days_ago_iso(TOUCHED_DAYS)
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT issue_key, MAX(last_update) lu FROM tasks WHERE author=? AND issue_key IS NOT NULL"
            " AND status<>'merged' AND last_update>=? GROUP BY issue_key ORDER BY lu DESC",
            (author, since)).fetchall()
    return [r["issue_key"] for r in rows][:TOUCHED_MAX]


def token_for(author: str) -> Optional[str]:
    """云端每人用自己的 Mobius 授权；本机版返回 None，走配置里那一个。

    以前按编号查单个 issue 时没带授权，云端一律报「未配置 Mobius」再被吞掉——
    扫描里明确写了的 issue 号在云端从来没认出来过。
    """
    if not config.CLOUD:
        return None
    from . import mobius_login
    return mobius_login.access_token(author)


def fetch_issue(author: str, identifier: str, token: Optional[str] = None) -> Dict[str, Any]:
    """Resolve one historical issue and retain it in the local validation cache."""
    res = _rpc("tools/call", {
        "name": "get_issue", "arguments": {"identifier": identifier}}, token=token)
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


def ensure_issue(author: str, issue_key: str) -> Dict[str, Any]:
    """人手点名的 issue：缓存里有就用，没有就现去 Mobius 查一次。

    缓存只装同步范围里的那些，人要归的 issue 常常不在里面（别人名下的、没指派的、
    刚建的）。以前这种一律「不在已同步的 issue 中」，人连手动补救都做不到。
    """
    key = (issue_key or "").strip().upper()
    if not match.ISSUE_RE.fullmatch(key):
        raise ValueError("issue 编号格式不对：%s（应该像 AI-2660）" % issue_key)
    with db.cursor() as conn:
        row = conn.execute("SELECT * FROM mobius_issues WHERE author=? AND issue_key=?",
                           (author, key)).fetchone()
    if row:
        return dict(row)
    try:
        issue = fetch_issue(author, key, token=token_for(author))
    except MobiusError as exc:
        raise ValueError("没能在 Mobius 上确认 %s：%s" % (key, exc)) from exc
    return {"issue_key": key, "title": issue.get("title", ""), "state": issue.get("state", "")}


def sync(author: str, assignee: Optional[str] = None,
         token: Optional[str] = None) -> Dict[str, Any]:
    """把这个人「参与中」的 issue 同步进本地缓存。出日报前、或人手动同步时调。

    Mobius 没有「参与中」的查询，这里拼出来：
      1. 指派给他、还开着的（backlog / 待开始 / 进行中都算）
      2. 指派给他、最近 7 天关掉的——收尾的进展还要能归上去
      3. 他在 Fecho 里最近归过进展的，不管指派给谁、有没有人认领；关了超过 7 天的不要
    整份替换：不在这一轮里的（关了很久、早就不碰了的）就从缓存里拿掉。
    """
    assignee = assignee or config.MOBIUS_ASSIGNEE
    if not assignee:
        raise MobiusError("不知道要同步谁的 issue：设置 FECHO_MOBIUS_ASSIGNEE 或传 assignee")
    found: Dict[str, Dict[str, Any]] = {i["identifier"]: dict(i, _via="assigned")
                                        for i in fetch_open_issues(assignee, token=token)}
    since = _days_ago_iso(RECENTLY_CLOSED_DAYS)
    for bucket in _CLOSED_BUCKETS:
        for i in _list_issues({"assigneeEmail": assignee, "stateType": bucket,
                               "updatedAfter": since}, token):
            found.setdefault(i["identifier"], dict(i, _bucket=bucket, _via="assigned"))

    bucket_of = {(i.get("state") or "").lower(): i["_bucket"] for i in found.values()}
    touched = 0
    for key in _touched_keys(author):
        if key in found:
            continue
        try:
            res = _rpc("tools/call", {"name": "get_issue", "arguments": {"identifier": key}},
                       token=token)
            payload = json.loads((res.get("content") or [{}])[0].get("text") or "{}")
        except (MobiusError, ValueError, KeyError, IndexError):
            continue                      # 查不到（删了、没权限）就不算，不挡着别的
        issue = payload.get("issue", payload)
        if issue.get("identifier") != key:
            continue
        name = (issue.get("state") or "").lower()
        bucket = bucket_of.get(name) or ("completed" if name in _CLOSED_NAMES else "started")
        if bucket in _CLOSED_BUCKETS and (issue.get("updatedAt") or "") < since:
            continue
        issue["_desc"] = snippet(issue.pop("description", None))   # 原文可能很长，只存摘要
        issue.pop("comments", None)
        issue.pop("activity", None)
        found[key] = dict(issue, _bucket=bucket, _via="touched")
        touched += 1

    _fill_descriptions(author, found, token)
    now = store.now_iso()
    with db.cursor() as conn:
        conn.execute("DELETE FROM mobius_issues WHERE author=?", (author,))
        for i in found.values():
            conn.execute(
                "INSERT INTO mobius_issues"
                " (issue_key, author, title, state, url, updated_at, synced_at, raw)"
                " VALUES (?,?,?,?,?,?,?,?)" + _UPSERT_ISSUE,
                (i["identifier"], author, i.get("title", ""), i.get("state", ""),
                 i.get("url", ""), i.get("updatedAt", ""), now,
                 json.dumps(i, ensure_ascii=False)),
            )
    return {"author": author, "assignee": assignee, "count": len(found),
            "touched": touched, "synced_at": now}


def _fill_descriptions(author: str, found: Dict[str, Dict[str, Any]], token: Optional[str]) -> None:
    """列表接口不带描述。上一轮存过、issue 之后没改过的直接复用，其余逐个补查。"""
    with db.cursor() as conn:
        old = {r["issue_key"]: r for r in conn.execute(
            "SELECT issue_key, updated_at, raw FROM mobius_issues WHERE author=?", (author,)).fetchall()}
    fetched = 0
    for key, issue in found.items():
        if "_desc" in issue:
            continue
        prev = old.get(key)
        if prev and prev["updated_at"] == issue.get("updatedAt"):
            try:
                desc = json.loads(prev["raw"] or "{}").get("_desc")
            except ValueError:
                desc = None
            if desc is not None:
                issue["_desc"] = desc
                continue
        if fetched >= DESC_FETCH_MAX:
            continue
        fetched += 1
        try:
            res = _rpc("tools/call", {"name": "get_issue", "arguments": {"identifier": key}},
                       token=token)
            payload = json.loads((res.get("content") or [{}])[0].get("text") or "{}")
            issue["_desc"] = snippet(payload.get("issue", payload).get("description"))
        except (MobiusError, ValueError, KeyError, IndexError, AttributeError):
            continue                      # 补不上就没有摘要，不挡着同步


PRIORITY_NAMES = {1: "紧急", 2: "高", 3: "中", 4: "低"}


def priority_rank(priority: Any) -> int:
    """排序用：紧急 1 → 低 4，没设优先级（0 / 空）排最后。"""
    try:
        p = int(priority or 0)
    except (TypeError, ValueError):
        p = 0
    return p if 1 <= p <= 4 else 5


def _decorate(row: Dict[str, Any]) -> Dict[str, Any]:
    """把 raw 里的优先级、状态分组、来源摊开。不加列：省一次线上改表。"""
    try:
        raw = json.loads(row.get("raw") or "{}") or {}
    except ValueError:
        raw = {}
    row["priority"] = raw.get("priority") or 0
    row["state_type"] = raw.get("_bucket") or ""
    row["closed"] = row["state_type"] in _CLOSED_BUCKETS
    row["via"] = raw.get("_via") or "assigned"
    row["desc"] = raw.get("_desc") or ""
    return row


def cached_issues(author: str) -> List[Dict[str, Any]]:
    """开着的在前，其次按优先级，同级新动过的在前。"""
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT * FROM mobius_issues WHERE author=? ORDER BY updated_at DESC", (author,)
        ).fetchall()
    out = [_decorate(dict(r)) for r in rows]
    out.sort(key=lambda i: (i["closed"], priority_rank(i["priority"])))   # 稳定排序，保留时间先后
    return out


def priorities(author: str) -> Dict[str, int]:
    """issue 号 → 优先级，出日报排序用。"""
    return {i["issue_key"]: i["priority"] for i in cached_issues(author)}


def cache_age(author: str) -> Optional[str]:
    rows = cached_issues(author)
    return rows[0]["synced_at"] if rows else None
