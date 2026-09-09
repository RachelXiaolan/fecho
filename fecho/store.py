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
from datetime import date as calendar_date, datetime, timezone
from typing import Any, Dict, List, Optional

from . import config, db, match


_UNSET = object()


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


def _get_or_create_mobius_task(author: str, issue_key: str, title: Optional[str]) -> Dict[str, Any]:
    with db.cursor() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE author=? AND issue_key=?", (author, issue_key)
        ).fetchone()
        if row:
            task = db._row(row)
            if task["status"] == "done":
                conn.execute("UPDATE tasks SET status='open' WHERE task_id=?", (task["task_id"],))
                task["status"] = "open"
            elif task["status"] == "merged":
                target_id = (task.get("meta") or {}).get("merged_into")
                target = db.get_task(target_id) if target_id else None
                if target and target["author"] == author:
                    if target["status"] == "done":
                        conn.execute("UPDATE tasks SET status='open' WHERE task_id=?", (target_id,))
                        target["status"] = "open"
                    return target
            return task
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


def _validate_issue(author: str, issue_key: str) -> str:
    key = (issue_key or "").strip().upper()
    if not match.ISSUE_RE.fullmatch(key):
        raise ValueError("issue 格式无效: %s" % issue_key)
    with db.cursor() as conn:
        known = conn.execute(
            "SELECT 1 FROM mobius_issues WHERE author=? AND issue_key=?", (author, key)
        ).fetchone()
    if not known:
        raise ValueError("issue %s 不在已同步的 Mobius issue 中；请先 sync_issues" % key)
    return key


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
    ingestion_method: str = "direct",
    completion_status: str = "unknown",
    content_kind: str = "progress",
    session_id: Optional[str] = None,
    issue: Optional[str] = None,
    task_id: Optional[str] = None,
    project: Optional[str] = None,
    freeform: bool = False,
    source_event_key: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    content_md = (content_md or "").strip()
    if not content_md:
        raise ValueError("进展内容不能为空")
    date = date or today()
    try:
        calendar_date.fromisoformat(date)
    except (TypeError, ValueError):
        raise ValueError("date 必须是 YYYY-MM-DD 格式的有效日期")
    if freeform and (issue or task_id):
        raise ValueError("freeform 不能和 issue/task_id 同时指定")
    if source_agent == "scan" and ingestion_method == "direct":
        ingestion_method = "transcript-scan"  # 兼容老调用方
    if completion_status not in ("done", "wip", "blocked", "unknown"):
        raise ValueError("completion_status 必须是 done/wip/blocked/unknown")
    if content_kind not in ("progress", "pitfall", "decision"):
        raise ValueError("content_kind 必须是 progress/pitfall/decision")

    # Transcript 重跑时模型措辞可能变化，不能只靠正文 hash 去重。稳定事件键在创建
    # task 之前判断，避免重复回放留下空任务。
    if source_event_key:
        with db.cursor() as conn:
            old = conn.execute(
                "SELECT u.*, t.title, t.source, t.issue_key FROM updates u"
                " JOIN tasks t ON t.task_id=u.task_id WHERE u.source_event_key=?",
                (source_event_key,),
            ).fetchone()
        if old:
            task = {"task_id": old["task_id"], "title": old["title"],
                    "source": old["source"], "issue_key": old["issue_key"]}
            decision = {"method": old["match_method"], "score": old["match_score"],
                        "via": "source-event-key"}
            return _result(task, old["update_id"], "duplicate", decision,
                           old["date"], old["author"],
                           note="同一 transcript 事件已处理，未重复写入。")

    tasks = db.list_tasks(author=author, status="open")
    if freeform:
        decision = {"method": "explicit-freeform", "score": 1.0,
                    "confidence": "high", "via": "caller"}
    else:
        decision = match.decide(
            content_md,
            tasks=tasks,
            session_task_id=_session_last_task(session_id, author),
            explicit_issue=issue,
            explicit_task_id=task_id,
            project=project,
            # 扫描是批量抽取，同一个 session 下的条目彼此没有对话上的先后关系，
            # 用会话惯性兜底只会把一条错误扩散成一片。
            allow_session_fallback=(source_agent or "") != "scan",
        )

    if decision.get("task_id"):
        task = db.get_task(decision["task_id"])
        if task is None or task["author"] != author:
            raise ValueError("任务不存在: %s" % decision["task_id"])
    elif decision.get("issue_key"):
        decision["issue_key"] = _validate_issue(author, decision["issue_key"])
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

        # 扫描来源额外挡近似重复。这跟「相似就删」不是一回事：
        # agent 主动记的相似内容可能是真实的不同进展（措辞碰巧像），必须全留；
        # 但扫描是把同一段对话重新总结一遍，措辞变了信息量没变，留着只是噪音。
        # 重跑（比如上次某组失败后补跑）会大量产生这种，实测一天能堆出 85 条里 158 对。
        #
        # 判为重复的**照样写进库**，只是标成 duplicate-ignored。相似度是纯字符串
        # 判断，没有后续环节能纠错——真判错了，直接不写就等于这条内容从没存在过，
        # 日报看不到、人也翻不到。留一行的成本几乎为零，丢一条真实进展的成本很高。
        dup_of = None
        if ingestion_method == "transcript-scan":
            near = conn.execute(
                "SELECT update_id, content_md FROM updates WHERE task_id=? AND date=?"
                " AND ingestion_method='transcript-scan' AND status='active'",
                (task["task_id"], date),
            ).fetchall()
            for row in near:
                if match.similarity(content_md, row["content_md"]) >= config.SCAN_DEDUPE_SIMILARITY:
                    dup_of = row["update_id"]
                    break

        update_id = str(uuid.uuid4())
        ts = now_iso()
        conn.execute(
            "INSERT INTO updates (update_id, task_id, author, date, content_md, source_agent,"
            " ingestion_method, session_id, match_method, match_score, assignment_source, assignment_locked,"
            " revision, source_event_key, completion_status, content_kind, pto_status,"
            " created_at, content_hash, status, meta)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (update_id, task["task_id"], author, date, content_md, source_agent or "manual",
             ingestion_method, session_id, decision["method"], decision.get("score"),
             _assignment_source(decision["method"]), 0, 1, source_event_key,
             completion_status, content_kind, None, ts, h,
             "duplicate-ignored" if dup_of else "active",
             json.dumps(dict(meta or {}, **({"duplicate_of": dup_of} if dup_of else {})),
                        ensure_ascii=False)),
        )
        if not dup_of:
            conn.execute("UPDATE tasks SET last_update=? WHERE task_id=?", (ts, task["task_id"]))

    if dup_of:
        return _result(task, update_id, "duplicate", decision, date, author,
                       note="内容和同任务下已有的扫描进展几乎相同，已存库但不进日报"
                            "（status=duplicate-ignored，判错了还能找回来）。")
    return _result(task, update_id, decision["method"], decision, date, author)


def _assignment_source(method: str) -> str:
    if method in ("explicit", "explicit-freeform"):
        return "agent"
    if method == "project-bound":
        return "project"
    if method == "task-continue":
        return "session"
    return "system"


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


def reassign(update_id: str, author: str, issue_key: Optional[str]) -> bool:
    """把一条进展挪到另一个 issue（或挪回自由任务）。交叉验证纠错用。

    原来那个任务可能因此变空——不删，留着；它可能还挂着别的日期的进展，
    而且留着比悄悄消失更容易看出发生过什么。
    """
    with db.cursor() as conn:
        row = conn.execute(
            "SELECT u.*, t.issue_key FROM updates u JOIN tasks t ON t.task_id=u.task_id"
            " WHERE u.update_id=? AND u.author=?", (update_id, author)).fetchone()
    if row is None or row["assignment_locked"] or row["issue_key"] == issue_key:
        return False

    if issue_key:
        issue_key = _validate_issue(author, issue_key)
        task = _get_or_create_mobius_task(author, issue_key, None)
    else:
        task = _create_freeform_task(author, row["content_md"])

    ts = now_iso()
    with db.cursor() as conn:
        conn.execute("UPDATE updates SET task_id=?, match_method=?, assignment_source=?,"
                     " revision=revision+1 WHERE update_id=?",
                     (task["task_id"], "verified", "model", update_id))
        conn.execute("UPDATE tasks SET last_update=? WHERE task_id=?", (ts, task["task_id"]))
    return True


def correct_progress(
    update_id: str,
    author: str,
    issue_key: Any = _UNSET,
    task_id: Optional[str] = None,
    content_md: Optional[str] = None,
    freeform: bool = False,
    actor: str = "human",
) -> Dict[str, Any]:
    """原地修订一条进展，并锁住人工确认的归属，保留完整审计记录。"""
    if task_id and (issue_key is not _UNSET or freeform):
        raise ValueError("task_id、issue_key、freeform 只能指定一个")
    if freeform and issue_key is not _UNSET:
        raise ValueError("freeform 和 issue_key 不能同时指定")

    with db.cursor() as conn:
        row = conn.execute(
            "SELECT u.*, t.issue_key FROM updates u JOIN tasks t ON t.task_id=u.task_id"
            " WHERE u.update_id=? AND u.author=?", (update_id, author)
        ).fetchone()
    if row is None:
        raise ValueError("进展不存在: %s" % update_id)

    new_content = row["content_md"] if content_md is None else (content_md or "").strip()
    if not new_content:
        raise ValueError("进展内容不能为空")

    target = db.get_task(row["task_id"])
    if task_id:
        target = db.get_task(task_id)
        if target is None or target["author"] != author:
            raise ValueError("任务不存在: %s" % task_id)
    elif freeform or issue_key is None:
        target = _create_freeform_task(author, new_content)
    elif issue_key is not _UNSET:
        key = _validate_issue(author, issue_key)
        target = _get_or_create_mobius_task(author, key, None)

    assignment_changed = target["task_id"] != row["task_id"]
    content_changed = new_content != row["content_md"]
    already_confirmed = bool(row["assignment_locked"] and row["assignment_source"] == "human")
    if not assignment_changed and not content_changed and already_confirmed:
        return {"changed": False, "update_id": update_id, "task": target}

    ts = now_iso()
    with db.cursor() as conn:
        conn.execute(
            "UPDATE updates SET task_id=?, content_md=?, content_hash=?, match_method=?,"
            " assignment_source='human', assignment_locked=1, revision=revision+1"
            " WHERE update_id=?",
            (target["task_id"], new_content, content_hash(new_content),
             "human-corrected", update_id),
        )
        conn.execute(
            "INSERT INTO assignment_events (update_id, author, actor, from_task_id, to_task_id,"
            " from_issue_key, to_issue_key, from_content_md, to_content_md, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (update_id, author, actor, row["task_id"], target["task_id"], row["issue_key"],
             target["issue_key"], row["content_md"], new_content, ts),
        )
        conn.execute("UPDATE tasks SET last_update=? WHERE task_id=?", (ts, target["task_id"]))
    return {"changed": True, "update_id": update_id, "task": db.get_task(target["task_id"])}


def set_task_status(task_id: str, author: str, status: str,
                    actor: str = "human") -> Dict[str, Any]:
    if status not in ("open", "done"):
        raise ValueError("任务状态必须是 open/done")
    with db.cursor() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE task_id=? AND author=?",
                           (task_id, author)).fetchone()
        if row is None:
            raise ValueError("任务不存在: %s" % task_id)
        if row["status"] == "merged":
            raise ValueError("已合并任务不能直接修改状态")
        changed = row["status"] != status
        if changed:
            conn.execute("UPDATE tasks SET status=? WHERE task_id=?", (status, task_id))
            conn.execute(
                "INSERT INTO task_events (author,actor,event_type,from_task_id,details,created_at)"
                " VALUES (?,?,?,?,?,?)",
                (author, actor, "complete" if status == "done" else "reopen", task_id,
                 json.dumps({"from": row["status"], "to": status}), now_iso()),
            )
    return {"changed": changed, "task": db.get_task(task_id)}


def close_task(task_id: str, author: str) -> Dict[str, Any]:
    return set_task_status(task_id, author, "done")["task"]


def merge_tasks(source_task_id: str, target_task_id: str, author: str,
                actor: str = "human") -> Dict[str, Any]:
    if source_task_id == target_task_id:
        raise ValueError("不能把任务合并到它自己")
    source, target = db.get_task(source_task_id), db.get_task(target_task_id)
    if not source or source["author"] != author:
        raise ValueError("源任务不存在: %s" % source_task_id)
    if not target or target["author"] != author:
        raise ValueError("目标任务不存在: %s" % target_task_id)
    if source["status"] == "merged":
        raise ValueError("源任务已经合并")

    ts = now_iso()
    with db.cursor() as conn:
        updates = conn.execute("SELECT * FROM updates WHERE task_id=?", (source_task_id,)).fetchall()
        for row in updates:
            conn.execute(
                "UPDATE updates SET task_id=?,match_method='human-merged',"
                "assignment_source='human',assignment_locked=1,revision=revision+1"
                " WHERE update_id=?", (target_task_id, row["update_id"]),
            )
            conn.execute(
                "INSERT INTO assignment_events (update_id,author,actor,from_task_id,to_task_id,"
                "from_issue_key,to_issue_key,from_content_md,to_content_md,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (row["update_id"], author, actor, source_task_id, target_task_id,
                 source.get("issue_key"), target.get("issue_key"), row["content_md"],
                 row["content_md"], ts),
            )
        source_meta = dict(source.get("meta") or {})
        source_meta["merged_into"] = target_task_id
        conn.execute("UPDATE tasks SET status='merged',meta=? WHERE task_id=?",
                     (json.dumps(source_meta, ensure_ascii=False), source_task_id))
        conn.execute("UPDATE tasks SET status='open',last_update=? WHERE task_id=?",
                     (ts, target_task_id))
        conn.execute(
            "INSERT INTO task_events (author,actor,event_type,from_task_id,to_task_id,details,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (author, actor, "merge", source_task_id, target_task_id,
             json.dumps({"moved_updates": len(updates)}, ensure_ascii=False), ts),
        )
    return {"source_task_id": source_task_id, "target_task_id": target_task_id,
            "moved_updates": len(updates), "task": db.get_task(target_task_id)}


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
    by_ingestion: Dict[str, int] = {}
    by_method: Dict[str, int] = {}
    task_ids = set()
    for u in ups:
        by_author[u["author"]] = by_author.get(u["author"], 0) + 1
        by_agent[u["source_agent"]] = by_agent.get(u["source_agent"], 0) + 1
        by_ingestion[u["ingestion_method"]] = by_ingestion.get(u["ingestion_method"], 0) + 1
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
        "by_ingestion_method": by_ingestion,
        "by_match_method": by_method,
        "by_task": by_task,
    }
