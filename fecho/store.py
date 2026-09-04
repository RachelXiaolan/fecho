"""写入层：一条进展从 agent 到落库的全部规则。

关键语义（这一版纠正过）：
  提交的是**做成了什么、进展到哪**，不是对话内容，也不是用户说过的 prompt。
  一个任务可以横跨很多天、很多个对话、很多个 agent；一个对话里也可能推进好几个任务。
  所以任务是主干，会话（session）只是审计线索和配对时的上下文先验。

同一任务下的多条进展**全部保留**——只有内容逐字相同的重复提交才会被挡下，
那是 agent 重试，不是新进展。
"""
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import config, db, match


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def content_hash(text: str) -> str:
    import hashlib

    norm = re.sub(r"\s+", "", (text or "").strip().lower())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _derive_title(content: str, limit: int = 40) -> str:
    """自由任务的名字：拿第一小句，够用就行。以后可以让 LLM 起名。"""
    first = re.split(r"[。\n；;]", content.strip())[0].strip()
    first = re.sub(r"^[-*\d.、\s]+", "", first)
    # 长句再往前切一刀：任务名要的是「帮同事查爬虫超时」，不是整句话
    if len(first) > 18:
        head = re.split(r"[，,]", first)[0].strip()
        if len(head) >= 6:
            first = head
    return (first[:limit] + "…") if len(first) > limit else (first or content[:limit])


def _session_last_task(session_id: Optional[str], author: str) -> Optional[str]:
    if not session_id:
        return None
    with db.cursor() as conn:
        row = conn.execute(
            "SELECT task_id FROM updates WHERE session_id=? AND author=? AND status='active'"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (session_id, author),
        ).fetchone()
    return row["task_id"] if row else None


def _recent_texts(author: str, limit_per_task: int = 3) -> Dict[str, str]:
    """每个任务最近几条进展拼起来，配对时和标题一起比。"""
    out: Dict[str, List[str]] = {}
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT task_id, content_md FROM updates WHERE author=? AND status='active'"
            " ORDER BY created_at DESC",
            (author,),
        ).fetchall()
    for r in rows:
        bucket = out.setdefault(r["task_id"], [])
        if len(bucket) < limit_per_task:
            bucket.append(r["content_md"])
    return {k: " ".join(v) for k, v in out.items()}


def _get_or_create_mobius_task(author: str, issue_key: str, title: Optional[str]) -> Dict[str, Any]:
    with db.cursor() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE author=? AND issue_key=?", (author, issue_key)
        ).fetchone()
        if row:
            return db._row(row)
        if not title:
            cached = conn.execute(
                "SELECT title FROM mobius_issues WHERE author=? AND issue_key=?",
                (author, issue_key),
            ).fetchone()
            title = cached["title"] if cached else issue_key
        task_id = str(uuid.uuid4())
        ts = now_iso()
        conn.execute(
            "INSERT INTO tasks (task_id, author, source, issue_key, title, status,"
            " first_seen, last_update, meta) VALUES (?,?,?,?,?,?,?,?,?)",
            (task_id, author, "mobius", issue_key, title, "open", today(), ts, "{}"),
        )
    return db.get_task(task_id)


def _create_freeform_task(author: str, content: str) -> Dict[str, Any]:
    task_id = str(uuid.uuid4())
    ts = now_iso()
    with db.cursor() as conn:
        conn.execute(
            "INSERT INTO tasks (task_id, author, source, issue_key, title, status,"
            " first_seen, last_update, meta) VALUES (?,?,?,?,?,?,?,?,?)",
            (task_id, author, "freeform", None, _derive_title(content), "open",
             today(), ts, "{}"),
        )
    return db.get_task(task_id)


def record_progress(
    author: str,
    content_md: str,
    date: Optional[str] = None,
    source_agent: str = "manual",
    session_id: Optional[str] = None,
    issue: Optional[str] = None,
    task_id: Optional[str] = None,
    project: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    content_md = (content_md or "").strip()
    if not content_md:
        raise ValueError("进展内容不能为空")
    date = date or today()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise ValueError("date 必须是 YYYY-MM-DD")

    from . import mobius

    issues = mobius.cached_issues(author)
    tasks = db.list_tasks(author=author, status="open")
    decision = match.decide(
        content_md,
        issues=issues,
        tasks=tasks,
        recent_texts=_recent_texts(author),
        session_task_id=_session_last_task(session_id, author),
        explicit_issue=issue,
        explicit_task_id=task_id,
        project=project,
    )

    if decision.get("task_id"):
        task = db.get_task(decision["task_id"])
        if task is None:
            raise ValueError("任务不存在: %s" % decision["task_id"])
    elif decision.get("issue_key"):
        task = _get_or_create_mobius_task(author, decision["issue_key"], decision.get("title"))
    else:
        task = _create_freeform_task(author, content_md)

    h = content_hash(content_md)
    with db.cursor() as conn:
        dup = conn.execute(
            "SELECT update_id FROM updates WHERE task_id=? AND date=? AND content_hash=?"
            " AND status='active'",
            (task["task_id"], date, h),
        ).fetchone()
        if dup:
            # 逐字相同 —— 这是 agent 重试，不是新进展。相似但不相同的一律保留。
            return _result(task, dup["update_id"], "duplicate", decision, date, author,
                           note="同一任务下已有逐字相同的进展，未重复写入。")

        update_id = str(uuid.uuid4())
        ts = now_iso()
        conn.execute(
            "INSERT INTO updates (update_id, task_id, author, date, content_md, source_agent,"
            " session_id, match_method, match_score, pto_status, created_at, content_hash,"
            " status, meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (update_id, task["task_id"], author, date, content_md, source_agent or "manual",
             session_id, decision["method"], decision.get("score"), None, ts, h, "active",
             json.dumps(meta or {}, ensure_ascii=False)),
        )
        conn.execute("UPDATE tasks SET last_update=? WHERE task_id=?", (ts, task["task_id"]))

    return _result(task, update_id, decision["method"], decision, date, author)


def _result(task, update_id, verdict, decision, date, author, note=None) -> Dict[str, Any]:
    out = {
        "update_id": update_id,
        "verdict": verdict,
        "task": {
            "task_id": task["task_id"],
            "title": task["title"],
            "source": task["source"],
            "issue_key": task["issue_key"],
        },
        "match": {
            "method": decision["method"],
            "score": decision.get("score"),
            "via": decision.get("via"),
            "confidence": decision.get("confidence", "high"),
        },
        "date": date,
        "author": author,
    }
    if note:
        out["note"] = note
    return out


def close_task(task_id: str, author: str) -> Dict[str, Any]:
    with db.cursor() as conn:
        conn.execute(
            "UPDATE tasks SET status='done' WHERE task_id=? AND author=?", (task_id, author)
        )
    return db.get_task(task_id)


def day_page(author: str, date: str) -> str:
    """文件即日页：当天按任务分组的进展原文。"""
    tasks = db.day_tasks(author, date)
    lines = ["# %s 工作流水 · %s" % (date, author), ""]
    if not tasks:
        lines.append("_（当日无进展）_")
    for t in tasks:
        head = "## %s" % t["title"]
        if t["issue_key"]:
            head = "## %s %s" % (t["issue_key"], t["title"])
        lines += [head, ""]
        for u in t["updates"]:
            ts = u["created_at"][11:16] if len(u["created_at"]) > 16 else u["created_at"]
            lines.append("- `%s` [%s] %s" % (ts, u["source_agent"], u["content_md"].strip()))
        lines.append("")
    lines += ["", "<!-- 由 Fecho 自动生成，勿手改；提交入口只有 MCP / API -->"]
    return "\n".join(lines)


def stats(since: Optional[str] = None, until: Optional[str] = None) -> Dict[str, Any]:
    sql = "SELECT * FROM updates WHERE status='active'"
    args: List[Any] = []
    if since:
        sql += " AND date >= ?"
        args.append(since)
    if until:
        sql += " AND date <= ?"
        args.append(until)
    with db.cursor() as conn:
        ups = [dict(r) for r in conn.execute(sql, args).fetchall()]
    by_author: Dict[str, int] = {}
    by_agent: Dict[str, int] = {}
    by_method: Dict[str, int] = {}
    task_ids = set()
    for u in ups:
        by_author[u["author"]] = by_author.get(u["author"], 0) + 1
        by_agent[u["source_agent"]] = by_agent.get(u["source_agent"], 0) + 1
        by_method[u["match_method"]] = by_method.get(u["match_method"], 0) + 1
        task_ids.add(u["task_id"])
    by_task = []
    for tid in task_ids:
        t = db.get_task(tid)
        if t:
            n = sum(1 for u in ups if u["task_id"] == tid)
            by_task.append({"issue_key": t["issue_key"], "title": t["title"],
                            "source": t["source"], "updates": n})
    by_task.sort(key=lambda x: -x["updates"])
    return {
        "range": {"since": since, "until": until},
        "updates": len(ups),
        "tasks_touched": len(task_ids),
        "by_author": by_author,
        "by_source_agent": by_agent,
        "by_match_method": by_method,
        "by_task": by_task,
    }
