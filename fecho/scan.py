"""从 agent 会话记录里自动抽出进展——「你不说，它也记」的那条路。

MCP server 看不见上下文（协议上就不可能），能观察对话的只有两样东西：agent
自己，和它写在磁盘上的会话记录。这个模块走后者。

流程：
    扫 transcript → 工作范围白名单过滤 → 水位线切出增量 → 按(项目,日期)分组
    → 各自喂给便宜模型 → 抽出的每条走正常的 record_progress（配对/绑定/去重全生效）

三个关键设计：

**水位线**。存 (session_id → 已处理到的时间戳)。没有它，能开好几周的会话每天
都会被重新总结一遍，库里堆重复。它同时解决三件事：同一天补跑、cron 漏跑后补齐、
跨零点归属。

**只靠路径过滤，且在调 LLM 之前**。一旦想「让模型看一眼再判断这是不是私事」，
内容已经发到公司端点了。所以隐私边界必须是确定性的，见 scope.py。

**按 (项目, 本地日期) 分组**。项目决定绑定到哪个 issue（确定性已知，不让模型
猜）；本地日期决定进展归到哪天（transcript 时间戳是 UTC，直接按 UTC 切会把
一天从早上切断）。
"""
import json
import glob
import hashlib
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import config, db, scope, store
from .match import ISSUE_RE

# 单次请求的输入上限（字符）。实测 24K tokens 打推理模型会超时。
CHUNK_CHARS = int(config.get("scan_chunk_chars", "FECHO_SCAN_CHUNK_CHARS", 18000))

# 宿主注入的样板：slash command 展开、skill 说明文档、系统提醒。
# 它们以 user 身份出现在记录里，但不是用户说的话——实测占了一天内容的 40%+，
# 喂进去纯烧钱还带偏总结。
_BOILERPLATE = re.compile(
    r"<command-(message|name|args)>|<system-reminder>|<local-command-|"
    r"Base directory for this skill:|<user-prompt-submit-hook>",
    re.MULTILINE,
)

PROMPT = """你在读一段「人和 coding agent 一起干活」的对话记录，任务是抽出这段时间**做成了什么**。

规则：
- 抽的是**成果和进展**，不是对话内容，也不是用户说过的话。
- 一件事一条。同一件事反复出现，合并成一条。
- **踩的坑、得出的负面结论也算进展**——「试了 X 发现不行，因为 Y」是有价值的记录。
- 只写对话里确实发生的事，不许推断、不许补充没做的事。
- 忽略纯粹的来回确认、纯提问、没有结论的讨论。
- **不许把「还在讨论/倾向于」写成「已决定」**。只有明确拍板的才用「定为/改成/确定」，
  还在比较的要写「在评估 X 和 Y」。
- 每条一到两句话，让人三个月后还看得懂。
- **必须用中文写**，无论对话本身是什么语言。技术名词（MCP、OAuth、SQLite 等）保留原文。

还要判断每条进展属于下面哪个 issue：
- 看的是**说的是不是同一件事**，不是字面有没有重合的词。
- 真的都不属于就写 `-`，系统会归到自由任务。**宁可写 `-` 也不要硬凑**——
  归错了下游的日报全跟着错，归不上只是多一个自由任务。
- 只能从下面给的列表里选，不许自己编 issue 号。
- 如果这段没有任何已经完成、推进或明确踩坑的内容，只输出一行 `NONE`。

{issues}

输出格式：一行一条，`类型 | issue号或-| 内容`，类型是 done / pitfall / decision 三者之一。
不要 JSON、不要代码块、不要编号、不要解释。内容里随便用什么标点都行。

示例：
done | AI-2541 | 配对引擎写完了，拿真实 issue 测下来 11/12 命中
pitfall | AI-2541 | 让模型自己数中文字数会把推理预算烧穿，改成给结构性目标才出得来
decision | - | 闲鱼选品定了强推三个品类，盗版资料类全部淘汰

对话记录如下：

"""


def _prompt(issues: List[Dict[str, Any]]) -> str:
    """把候选 issue 填进提示词。

    归属交给读得懂意思的模型判断，不再靠字面相似度打分——后者在
    「做这个项目本身」的场景下天然失效（标题和干活时说的话一个词都不重合），
    又会因为两句话都出现 agent 这种通用词而误判。
    """
    if issues:
        lines = "\n".join("- %s：%s" % (i["issue_key"], i["title"]) for i in issues)
        block = "候选 issue（只能从这里选）：\n%s" % lines
    else:
        block = "（当前没有在办的 issue，所有条目的 issue 号都写 `-`）"
    return PROMPT.replace("{issues}", block)


# ---------- 水位线 ----------

def get_mark(session_id: str) -> Optional[str]:
    with db.cursor() as conn:
        row = conn.execute(
            "SELECT last_ts FROM scan_marks WHERE session_id=?", (session_id,)
        ).fetchone()
    return row["last_ts"] if row else None


def set_mark(session_id: str, last_ts: str, entries: int) -> None:
    with db.cursor() as conn:
        conn.execute(
            "INSERT INTO scan_marks (session_id, last_ts, last_scan_at, entries)"
            " VALUES (?,?,?,?)"
            " ON CONFLICT(session_id) DO UPDATE SET last_ts=excluded.last_ts,"
            " last_scan_at=excluded.last_scan_at, entries=entries+excluded.entries",
            (session_id, last_ts, store.now_iso(), entries),
        )


# ---------- 读会话记录 ----------

def _iter_records(path: Path):
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def _text_of(rec: Dict[str, Any]) -> str:
    msg = rec.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        body = content
    elif isinstance(content, list):
        body = "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    else:
        return ""
    return "" if _BOILERPLATE.search(body) else body


def discover_transcripts() -> List[Dict[str, Any]]:
    """按配置发现各宿主会话文件；路径只决定适配器，不读取内容。"""
    found: List[Dict[str, Any]] = []
    seen = set()
    for producer, patterns in config.SCAN_SOURCES.items():
        for pattern in patterns:
            for value in glob.glob(str(Path(pattern).expanduser()), recursive=True):
                path = Path(value)
                key = (producer, str(path))
                if path.is_file() and key not in seen:
                    seen.add(key)
                    found.append({"producer_agent": producer, "path": path})
    return sorted(found, key=lambda x: (x["producer_agent"], str(x["path"])))


def _parts_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(p.get("text", "")) for p in content
        if isinstance(p, dict) and p.get("type") in ("text", "input_text", "output_text")
    )


def _normalized_records(source: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]]]:
    """把 Claude/Codex/Hermes 的 JSONL 收敛成同一种最小事件结构。"""
    producer, path = source["producer_agent"], source["path"]
    raw_rows = list(_iter_records(path))
    if producer == "claude-code":
        session_id = path.stem
        out = []
        for rec in raw_rows:
            if rec.get("type") not in ("user", "assistant"):
                continue
            out.append({"timestamp": rec.get("timestamp"), "role": rec.get("type"),
                        "cwd": rec.get("cwd") or path.parent.name, "text": _text_of(rec)})
        return session_id, out

    cwd, sid = "", path.stem
    out = []
    for rec in raw_rows:
        payload = rec.get("payload") or {}
        if rec.get("type") == "session_meta":
            cwd = payload.get("cwd") or cwd
            sid = payload.get("id") or payload.get("session_id") or sid
            continue
        if rec.get("type") == "turn_context":
            cwd = payload.get("cwd") or cwd
            continue

        role, content = None, None
        if rec.get("type") == "response_item" and payload.get("type") == "message":
            role, content = payload.get("role"), payload.get("content")
        elif isinstance(rec.get("message"), dict):
            role = rec["message"].get("role") or rec.get("type")
            content = rec["message"].get("content")
        else:  # Hermes/OpenAI 风格：顶层 role + content
            role, content = rec.get("role"), rec.get("content")
        if role not in ("user", "assistant"):
            continue
        body = _parts_text(content)
        if _BOILERPLATE.search(body):
            body = ""
        out.append({"timestamp": rec.get("timestamp") or rec.get("created_at"),
                    "role": role, "cwd": rec.get("cwd") or payload.get("cwd") or cwd,
                    "text": body})
    return "%s:%s" % (producer, sid), out


def collect(days: int = 1) -> Tuple[Dict[Tuple[str, str, str, str], List[Dict]],
                                    Dict[str, set], Dict[str, List[str]]]:
    """返回 (分组, 被跳过的目录, 每个会话的最新时间戳)。

    分组的键是 (session_id, project, 本地日期)——三者决定了这批进展归谁、
    归哪个 issue、归哪天。
    """
    tz = datetime.now().astimezone().tzinfo
    floor = (datetime.now(tz) - timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0)

    groups: Dict[Tuple[str, str, str], List[Dict]] = {}
    skipped: Dict[str, set] = {}
    stamps: Dict[str, List[str]] = {}   # session -> 本轮看到的所有时间戳
    verdicts: Dict[str, str] = {}

    for source in discover_transcripts():
        producer = source["producer_agent"]
        session_id, records = _normalized_records(source)
        mark = get_mark(session_id)
        for rec in records:
            ts = rec.get("timestamp")
            if not ts:
                continue
            if mark and ts <= mark:            # 水位线之前的，已经处理过
                continue
            when = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(tz)
            if when < floor:                   # 太老的不追，避免首次扫描炸开
                continue

            cwd = rec.get("cwd") or source["path"].parent.name
            v = verdicts.get(cwd)
            if v is None:
                v = verdicts[cwd] = scope.classify(cwd)[0]
            if v != "work":
                # 未登记/已忽略：连内容都不取，更不会进 LLM 请求。
                skipped.setdefault(v, set()).add(cwd)
                stamps.setdefault(session_id, []).append(ts)
                continue

            body = (rec.get("text") or "").strip()
            if not body:
                stamps.setdefault(session_id, []).append(ts)
                continue

            key = (producer, session_id, cwd, when.strftime("%Y-%m-%d"))
            groups.setdefault(key, []).append(
                {"at": when, "ts": ts, "role": rec["role"], "text": body})
            stamps.setdefault(session_id, []).append(ts)

    return groups, skipped, {k: sorted(v) for k, v in stamps.items()}


def _split(rows: List[Dict[str, Any]], limit: int) -> List[List[Dict[str, Any]]]:
    """按时间顺序切块，每块不超过 limit 个字符。"""
    out, cur, size = [], [], 0
    for r in rows:
        n = len(r["text"]) + 24
        if cur and size + n > limit:
            out.append(cur)
            cur, size = [], 0
        cur.append(r)
        size += n
    if cur:
        out.append(cur)
    return out


def render(rows: List[Dict[str, Any]], cap: int = 2000) -> str:
    out = []
    for r in rows:
        body = r["text"]
        if len(body) > cap:
            body = body[: cap // 2] + "\n…(略)…\n" + body[-cap // 2:]
        out.append("[%s %s] %s" % (r["at"].strftime("%H:%M"),
                                   "用户" if r["role"] == "user" else "agent", body))
    return "\n\n".join(out)


def parse_entries(raw: str, valid_keys: Optional[set] = None) -> List[Dict[str, str]]:
    """一行一条、竖线分隔。

    刻意不用 JSON：中文内容里的引号会把它打断（实测两个项目全炸在这），
    而这里根本不需要嵌套结构。
    """
    raw = re.sub(r"^```\w*\s*|\s*```$", "", raw.strip())
    out = []
    for line in raw.splitlines():
        line = line.strip().lstrip("-*0123456789. ")
        parts = [x.strip() for x in line.split("|")]
        if len(parts) < 2:
            continue
        kind = parts[0].lower()
        if kind not in ("done", "pitfall", "decision"):
            continue
        if len(parts) >= 3:
            issue, content = parts[1], "|".join(parts[2:]).strip()
        else:                                   # 老格式：没有 issue 段
            issue, content = "", parts[1]
        if not content:
            continue
        # 只认真实存在的 issue。光校验形状不够——模型可以吐出一个格式完全正确
        # 但根本不存在的号，那样就成了凭空造归属。
        issue = issue.upper()
        ok = bool(ISSUE_RE.fullmatch(issue)) and (valid_keys is None or issue in valid_keys)
        out.append({"kind": kind, "content": content, "issue": issue if ok else None})
    return out


def _parse_response(raw: str, valid_keys: set) -> List[Dict[str, str]]:
    cleaned = re.sub(r"^```\w*\s*|\s*```$", "", (raw or "").strip())
    if cleaned.upper() == "NONE":
        return []
    entries = parse_entries(cleaned, valid_keys)
    if not entries:
        raise ValueError("模型输出不符合约定格式，未推进扫描水位线")
    return entries


def _scan_run_start(author: str, producer: str, session_id: str, project: str,
                    date: str, rows: List[Dict[str, Any]], chunks: int) -> str:
    run_id = str(uuid.uuid4())
    with db.cursor() as conn:
        conn.execute(
            "INSERT INTO scan_runs (run_id,author,producer_agent,session_id,project,date,"
            "group_start_ts,group_end_ts,status,chunks,started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, author, producer, session_id, project, date,
             min(r["ts"] for r in rows), max(r["ts"] for r in rows), "running", chunks,
             store.now_iso()),
        )
    return run_id


def _scan_run_finish(run_id: str, status: str, entries: int = 0,
                     error: Optional[str] = None) -> None:
    with db.cursor() as conn:
        conn.execute("UPDATE scan_runs SET status=?, entries=?, error=?, finished_at=?"
                     " WHERE run_id=?", (status, entries, error, store.now_iso(), run_id))


def _event_key(producer: str, session_id: str, part: List[Dict[str, Any]],
               item_index: int) -> str:
    seed = "|".join((producer, session_id, part[0]["ts"], part[-1]["ts"], str(item_index)))
    return "scan:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


# ---------- 主流程 ----------

def scan(days: int = 1, dry_run: bool = False, author: Optional[str] = None) -> Dict[str, Any]:
    from . import llm

    author = author or config.AUTHOR
    db.init()

    if not config.llm_configured():
        return {"ok": False, "error": "没配 LLM，扫描没法总结。先跑 fecho setup --llm-url …"}

    groups, skipped, stamps = collect(days=days)
    result: Dict[str, Any] = {
        "ok": True, "dry_run": dry_run, "groups": [], "skipped": {k: sorted(v) for k, v in skipped.items()},
        "recorded": 0, "tokens_in": 0,
    }
    if not groups:
        result["note"] = "没有新内容（水位线之后没有属于工作范围的对话）"
        return result

    from . import match

    # 候选 issue 取一次，所有组共用
    from . import mobius
    open_issues = mobius.cached_issues(author)
    valid_keys = {i["issue_key"] for i in open_issues}

    blocked: Dict[str, str] = {}     # session_id -> 失败组里最早的时间戳
    for (producer, session_id, project, date), rows in sorted(groups.items()):
        convo = render(rows)
        bound = match.project_binding(project)
        g = {"session": session_id[:8], "session_id": session_id,
             "producer_agent": producer, "ingestion_method": "transcript-scan",
             "project": project, "date": date,
             "messages": len(rows), "tokens_in": int(len(convo) / 1.5),
             "bound": bound, "entries": []}
        result["tokens_in"] += g["tokens_in"]

        if dry_run:
            result["groups"].append(g)
            continue

        # 实测单组 24K tokens 打推理模型会超时。超限就按时间顺序切块分几次问——
        # 一天的活本来就是分段发生的，切开反而更贴近事实。
        chunks = _split(rows, CHUNK_CHARS)
        g["chunks"] = len(chunks)
        run_id = _scan_run_start(author, producer, session_id, project, date, rows, len(chunks))
        extracted: List[Tuple[Dict[str, str], str]] = []
        failed = False
        for part in chunks:
            try:
                raw = llm.chat(
                    [{"role": "user", "content": _prompt(open_issues) + render(part)}],
                    temperature=0.1, max_tokens=4000)
                parsed = _parse_response(raw, valid_keys)
                for item_index, entry in enumerate(parsed, 1):
                    extracted.append((entry, _event_key(producer, session_id, part, item_index)))
            except (llm.LLMError, ValueError) as exc:
                g["error"] = str(exc)[:200]
                failed = True
                break

        if failed:
            result["groups"].append(g)
            # 这组没处理成，水位线不能越过它——否则这段对话永远不会被重试。
            # 记下这组最早的时间戳，稍后把该会话的水位线卡在它之前。
            first = min(r["ts"] for r in rows)
            blocked[session_id] = min(blocked.get(session_id, first), first)
            _scan_run_finish(run_id, "failed", error=g["error"])
            result["ok"] = False
            continue

        for e, source_event_key in extracted:
            rec = store.record_progress(
                author, e["content"], date=date, source_agent=producer,
                ingestion_method="transcript-scan",
                session_id=session_id, project=project,
                issue=e.get("issue"),        # 模型判的归属，当确定信号用
                source_event_key=source_event_key,
                meta={"kind": e["kind"], "source": "transcript",
                      "ingestion_method": "transcript-scan"},
            )
            g["entries"].append({
                "content": e["content"], "kind": e["kind"],
                "verdict": rec["verdict"], "task": rec["task"]["title"],
                "issue": rec["task"]["issue_key"], "method": rec["match"]["method"],
            })
            if rec["verdict"] != "duplicate":
                result["recorded"] += 1
        _scan_run_finish(run_id, "succeeded", entries=len(g["entries"]))
        result["groups"].append(g)

    # 水位线只在真写入之后推进——dry-run 不该让下次扫描漏掉这段。
    #
    # 有组失败时，水位线必须停在那组**之前**：之前成功处理的保持已处理，
    # 失败的那段和它之后的下次重来。推过去就等于那段对话永远丢了。
    if not dry_run:
        for session_id, all_ts in stamps.items():
            floor_ts = blocked.get(session_id)
            usable = [t for t in all_ts if floor_ts is None or t < floor_ts]
            if not usable:
                continue                     # 整个会话都卡在失败组里，水位线不动
            n = sum(len(g["entries"]) for g in result["groups"]
                    if g.get("session_id") == session_id)
            set_mark(session_id, usable[-1], n)
        if blocked:
            result["retry_next_time"] = sorted(blocked)

    return result
