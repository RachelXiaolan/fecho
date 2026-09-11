"""云端版的扫描：服务器这一侧。

扫描本身在每个人自己的电脑上跑（读的是本机的聊天记录，原始记录不出本机）。
服务器管三件事：

1. **什么时候该扫**。本机的小脚本每 15 分钟来问一次「该扫了吗」。出日报的时间设在
   服务器上，扫描要比它早 15 分钟——时间以服务器上的设置为准，所以网页上改了、或者
   跟 agent 说了要改，本机都能自动跟上，不用重装。这个问答不经过任何大模型，不花 token。
2. **扫哪里**。白名单（工作文件夹）和要扫哪些 agent 都存在服务器上，所有 agent 共用
   一份，随「该扫了吗」的回答一起下发。
3. **收结果**。本机 agent 提取出候选进展后交上来，每条走和随手记同一套归属、去重规则。
   晚到的结果（比如出日报时电脑没联网，事后补扫）会让那天的日报自动重新生成。
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from . import accounts, clock, db, jobs, store

# 第一版支持的 agent。每加一个都要会读它的聊天记录，所以不是改个列表就能加。
AGENTS: Dict[str, Dict[str, str]] = {
    "claude-code": {"label": "Claude Code", "transcripts": "~/.claude/projects/*/*.jsonl"},
    "codex": {"label": "Codex", "transcripts": "~/.codex/sessions/*/*/*/*.jsonl"},
    "hermes": {"label": "Hermes", "transcripts": "~/.hermes/sessions/**/*.jsonl"},
}
CHECK_EVERY_MINUTES = 15
SCAN_LEAD_MINUTES = accounts.SCAN_LEAD_MINUTES
RETRY_AFTER_FAILURE = timedelta(minutes=30)
_KIND = {"done": "progress", "progress": "progress", "pitfall": "pitfall", "decision": "decision"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- agent ----------

def agent_id(client_name: Optional[str]) -> Optional[str]:
    """MCP 客户端报上来的名字 → 我们认的 agent。认不出来的不记。"""
    name = (client_name or "").lower()
    for key, needle in (("claude-code", "claude"), ("codex", "codex"), ("hermes", "hermes")):
        if needle in name:
            return key
    return None


def touch_agent(author: str, client_name: Optional[str]) -> Optional[str]:
    """某个 agent 连上来了，记一笔「最近一次连接」。网页上的点亮状态靠这个。"""
    agent = agent_id(client_name)
    if not agent:
        return None
    now = _now_iso()
    with db.cursor() as conn:
        conn.execute(
            "INSERT INTO agent_connections (author, agent, scan_enabled, first_seen, last_seen)"
            " VALUES (?,?,1,?,?) ON CONFLICT (author, agent) DO UPDATE SET"
            " last_seen=excluded.last_seen,"
            " first_seen=COALESCE(agent_connections.first_seen, excluded.first_seen)",
            (author, agent, now, now))
    return agent


def agents(author: str) -> List[Dict[str, Any]]:
    """所有支持的 agent，带上这个人的连接状态和扫不扫。"""
    with db.cursor() as conn:
        rows = {r["agent"]: dict(r) for r in conn.execute(
            "SELECT * FROM agent_connections WHERE author=?", (author,)).fetchall()}
    out = []
    for key, spec in AGENTS.items():
        row = rows.get(key, {})
        out.append({"agent": key, "label": spec["label"],
                    "connected": bool(row.get("last_seen")),
                    "last_seen": row.get("last_seen"),
                    # 从没配置过的 agent 默认不扫：没用过的 agent 没有聊天记录可扫
                    "scan_enabled": bool(row.get("scan_enabled")) if row else False})
    return out


def set_scan_enabled(author: str, agent: str, enabled: bool) -> Dict[str, Any]:
    if agent not in AGENTS:
        raise ValueError("不支持的 agent: %s（目前支持 %s）" % (agent, "、".join(AGENTS)))
    with db.cursor() as conn:
        conn.execute(
            "INSERT INTO agent_connections (author, agent, scan_enabled) VALUES (?,?,?)"
            " ON CONFLICT (author, agent) DO UPDATE SET scan_enabled=excluded.scan_enabled",
            (author, agent, 1 if enabled else 0))
    return next(a for a in agents(author) if a["agent"] == agent)


# ---------- 工作文件夹（白名单） ----------

def _norm_path(path: str) -> str:
    path = (path or "").strip()
    if len(path) > 1:
        path = path.rstrip("/")
    return path


def report_folders(author: str, folders: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """agent 上报「用 agent 干过活的文件夹」。只有路径，没有内容。

    已有的保留勾选状态，只更新最近使用时间；新出现的默认不勾——白名单只能由人主动加。
    """
    now = _now_iso()
    n = 0
    with db.cursor() as conn:
        for item in folders:
            path = _norm_path(item.get("path") if isinstance(item, dict) else item)
            if not path.startswith("/"):
                continue                       # 只收绝对路径，否则没法和聊天记录里的目录对上
            last_used = item.get("last_used") if isinstance(item, dict) else None
            conn.execute(
                "INSERT INTO work_folders (author, path, selected, last_used, reported_at)"
                " VALUES (?,?,0,?,?) ON CONFLICT (author, path) DO UPDATE SET"
                " last_used=COALESCE(excluded.last_used, work_folders.last_used),"
                " reported_at=excluded.reported_at",
                (author, path, last_used, now))
            n += 1
    return {"reported": n, "folders": folders_of(author)}


def folders_of(author: str) -> List[Dict[str, Any]]:
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT path, selected, last_used FROM work_folders WHERE author=?"
            " ORDER BY selected DESC, last_used DESC, path", (author,)).fetchall()
    return [{"path": r["path"], "selected": bool(r["selected"]), "last_used": r["last_used"]}
            for r in rows]


def selected_folders(author: str) -> List[str]:
    return [f["path"] for f in folders_of(author) if f["selected"]]


def set_selected(author: str, paths: Iterable[str]) -> List[str]:
    """把白名单设成这一组。清单里没有的路径（比如手动输入的）也会加进来。"""
    want = {_norm_path(p) for p in paths if _norm_path(p).startswith("/")}
    now = _now_iso()
    with db.cursor() as conn:
        conn.execute("UPDATE work_folders SET selected=0 WHERE author=?", (author,))
        for path in want:
            conn.execute(
                "INSERT INTO work_folders (author, path, selected, reported_at) VALUES (?,?,1,?)"
                " ON CONFLICT (author, path) DO UPDATE SET selected=1", (author, path, now))
    return selected_folders(author)


def in_scope(path: Optional[str], allowed: Iterable[str]) -> bool:
    """这个目录在白名单里吗（等于某个白名单文件夹，或在它下面）。"""
    path = _norm_path(path or "")
    if not path:
        return False
    for folder in allowed:
        if path == folder or path.startswith(folder.rstrip("/") + "/"):
            return True
    return False


# ---------- 该扫了吗 ----------

def _minutes(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def _checkin(author: str, date: str) -> Optional[Dict[str, Any]]:
    with db.cursor() as conn:
        row = conn.execute("SELECT * FROM scan_checkins WHERE author=? AND date=?",
                           (author, date)).fetchone()
    return dict(row) if row else None


def _last_done(author: str) -> Optional[str]:
    with db.cursor() as conn:
        row = conn.execute("SELECT MAX(finished_at) AS t FROM scan_checkins"
                           " WHERE author=? AND status='done'", (author,)).fetchone()
    return row["t"] if row else None


def notices(author: str) -> List[str]:
    """需要本机弹通知告诉人的事。服务器没法直接在用户的 Mac 上弹通知，
    但本机每 15 分钟会来问一次，就顺带把要提醒的事告诉它。"""
    today = clock.today()
    yesterday = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT date, error FROM jobs WHERE author=? AND status='failed' AND date IN (?,?)"
            " ORDER BY date", (author, yesterday, today)).fetchall()
    return ["%s 的日报没生成成功：%s" % (r["date"], (r["error"] or "未知原因")[:80]) for r in rows]


def due(author: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    """本机每 15 分钟来问一次。回答「要不要扫、扫哪天、扫哪里」。

    - 到了（出日报时间 − 15 分钟）且今天还没扫 → 扫今天
    - 还没到今天的点，但昨天没扫成（比如昨晚电脑没开）→ 先补昨天
    - 刚失败过 → 等半小时再试，别每 15 分钟狂试
    - 没选任何工作文件夹 → 不扫。没有白名单就什么都不读，这是隐私的默认值
    """
    user = accounts.get_user(author) or {}
    daily_time = user.get("daily_time") or "21:00"
    scan_at = _minutes(daily_time) - SCAN_LEAD_MINUTES
    local = clock.now(now)
    today = local.strftime("%Y-%m-%d")
    yesterday = (local - timedelta(days=1)).strftime("%Y-%m-%d")
    # 账号是北京时间哪天建的。created_at 存的是 UTC，直接截前 10 位会差一天：
    # 北京凌晨 2 点建的号，UTC 还是前一天——实测它就被要求去补扫建号之前那天了。
    created = (clock.now(datetime.fromisoformat(user["created_at"])).strftime("%Y-%m-%d")
               if user.get("created_at") else "")

    base: Dict[str, Any] = {
        "daily_time": daily_time,
        "scan_at": "%02d:%02d" % divmod(scan_at, 60),
        "check_every_minutes": CHECK_EVERY_MINUTES,
        "folders": selected_folders(author),
        "agents": [{"agent": a["agent"], "transcripts": AGENTS[a["agent"]]["transcripts"]}
                   for a in agents(author) if a["scan_enabled"]],
        "since": _last_done(author),
        "notices": notices(author),
    }

    def answer(is_due: bool, date: Optional[str], reason: str) -> Dict[str, Any]:
        return dict(base, due=is_due, date=date, reason=reason)

    if not base["folders"]:
        return answer(False, None, "还没选工作文件夹，什么都不扫")
    if not base["agents"]:
        return answer(False, None, "没有开启扫描的 agent")

    if local.hour * 60 + local.minute >= scan_at:
        target = today
    elif yesterday >= created and not _checkin(author, yesterday):
        target = yesterday              # 昨晚没扫成，先补上
    else:
        return answer(False, None, "还没到 %s" % base["scan_at"])

    done = _checkin(author, target)
    if done and done["status"] == "done":
        return answer(False, target, "%s 已经扫过了" % target)
    if done and done["status"] == "failed":
        last = datetime.fromisoformat(done["finished_at"])
        if clock.now(now) - last.astimezone(clock.BEIJING) < RETRY_AFTER_FAILURE:
            return answer(False, target, "上次扫描失败，半小时后再试")
    return answer(True, target, "该扫 %s 了" % target)


# ---------- 收结果 ----------

def submit(author: str, entries: Iterable[Dict[str, Any]], date: Optional[str] = None,
           finished: bool = False, error: Optional[str] = None,
           producer: str = "scan") -> Dict[str, Any]:
    """收本机扫描提取出的候选进展。

    - 每条都走 record_progress：归属、去重规则和随手记完全一样
    - 服务器再按白名单过一遍：本机那边漏过了，这里也不收（第二道隐私防线）
    - 同一条带同一个 source_event_key 重复上传，只记一次
    - finished=True 表示这一轮扫完了，服务器记下「这天已扫」
    - 某天已经出过日报、又来了新进展，自动排队重出那天的日报
    """
    allowed = selected_folders(author)
    recorded, duplicate, out_of_scope, rejected = 0, 0, 0, []
    touched_dates = set()
    for e in entries:
        content = (e.get("content") or "").strip()
        if not content:
            continue
        if not in_scope(e.get("project"), allowed):
            out_of_scope += 1
            continue
        try:
            kind = (e.get("kind") or "done").lower()
            # 和本机版扫描（scan.py）的调用保持一致：没带 issue 号不强制归自由任务，
            # 让目录绑定、同会话这些确定信号照常起作用
            res = store.record_progress(
                author, content,
                date=e.get("date") or date,
                source_agent=e.get("agent") or producer,
                ingestion_method="transcript-scan",
                completion_status=e.get("completion_status")
                or ("done" if kind in ("done", "decision") else "unknown"),
                content_kind=_KIND.get(kind, "progress"),
                session_id=e.get("session_id"),
                issue=e.get("issue") or None,
                project=e.get("project"),
                source_event_key=e.get("source_event_key"),
                unknown_issue_policy="freeform",   # 认不出的 issue 号先放自由任务等人复核
                meta={"kind": kind, "source": "transcript",
                      "ingestion_method": "transcript-scan", "uploaded_by": "local-agent"},
            )
        except ValueError as exc:
            rejected.append({"content": content[:60], "error": str(exc)[:120]})
            continue
        if res.get("verdict") == "duplicate":
            duplicate += 1
        else:
            recorded += 1
            touched_dates.add(res["date"])

    if finished and date:
        with db.cursor() as conn:
            conn.execute(
                "INSERT INTO scan_checkins (author, date, status, uploaded, error, finished_at)"
                " VALUES (?,?,?,?,?,?) ON CONFLICT (author, date) DO UPDATE SET"
                " status=excluded.status, uploaded=scan_checkins.uploaded+excluded.uploaded,"
                " error=excluded.error, finished_at=excluded.finished_at",
                (author, date, "failed" if error else "done", recorded, error, _now_iso()))

    requeued = []
    for d in sorted(touched_dates):
        if db.get_report(author, d, "daily"):
            jobs.enqueue(author, "regenerate", d)
            requeued.append(d)

    return {"recorded": recorded, "duplicate": duplicate, "out_of_scope": out_of_scope,
            "rejected": rejected, "regenerate_queued": requeued}
