"""整理层：当天推进的任务 -> 日报 + 口播稿。

日报按**任务**分组，不是按记录平铺：每个任务一段，讲清这件事今天推到哪了。

三条不变量：
  1. 幂等——以当天全部进展为唯一输入；输入指纹没变就不重复烧 LLM token。
  2. 永远有产物——LLM 挂了走确定性兜底稿，日报文件不会缺。
  3. 口播稿字数硬校验——生成后数字数，超界调结构重写，仍超界按句号裁。
"""
import argparse
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import config, db, llm, personas, pto, store


REPORT_PROMPT_VERSION = "daily-v2-status-fields"
VERIFY_PROMPT_VERSION = "assignment-v2-cache-safe"


def _cjk_len(text: str) -> int:
    return len(re.sub(r"[\s#*`>_\-\[\]()]", "", text or ""))


def speaking_seconds(text: str) -> int:
    return int(round(_cjk_len(text) / 3.6))


def fingerprint(tasks: List[Dict[str, Any]], persona: Dict[str, Any], pto_status: str) -> str:
    blob = json.dumps(
        {
            "tasks": [[t["task_id"], t.get("issue_key"), t.get("title"), t.get("status"),
                       [[u["update_id"], u.get("revision", 1), u.get("content_hash"),
                         u.get("task_id"), u.get("assignment_source"),
                         u.get("assignment_locked", 0), u.get("completion_status", "unknown"),
                         u.get("content_kind", "progress")] for u in t["updates"]]]
                      for t in tasks],
            "persona": personas.fingerprint(persona),
            "pto": pto_status,
            "prompt_version": REPORT_PROMPT_VERSION,
        },
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _tasks_block(tasks: List[Dict[str, Any]]) -> str:
    out = []
    for i, t in enumerate(tasks, 1):
        label = "%s %s" % (t["issue_key"], t["title"]) if t["issue_key"] else t["title"]
        agents = sorted({u["source_agent"] for u in t["updates"]})
        head = "[任务 %d] %s（%s，%d 条进展，来自 %s）" % (
            i, label, "Mobius" if t["source"] == "mobius" else "无对应 issue",
            len(t["updates"]), "/".join(agents))
        body = "\n".join("  - [status=%s kind=%s] %s" % (
                             u.get("completion_status", "unknown"),
                             u.get("content_kind", "progress"),
                             u["content_md"].strip().replace("\n", " "))
                         for u in t["updates"])
        out.append(head + "\n" + body)
    return "\n\n".join(out)


STATUS_ICON = {"done": "✅", "wip": "⭕️", "blocked": "❌", "unknown": "•"}

# 标题里这些后缀对短名没信息量，砍掉
_TITLE_TRIM = re.compile(r"[（(【\[].*?[）)】\]]|[:：].*$")


def short_name(task: Dict[str, Any], persona: Dict[str, Any], limit: int = 14) -> str:
    """任务短名。优先用人配的别名，否则从标题派生——不让模型起名。

    模型给任务起短名时会把当天还在讨论的候选名当成已确定（把 fecho 写成 fmjot）。
    标题和 issue 号在库里是确定的，没理由让它猜。
    """
    aliases = persona.get("task_aliases") or {}
    if task.get("issue_key") and task["issue_key"] in aliases:
        return aliases[task["issue_key"]]

    title = (task.get("title") or "").strip()
    trimmed = _TITLE_TRIM.sub("", title).strip(" -—、,，。")
    name = trimmed or title
    if len(name) > limit:
        cut = max((name.rfind(c, 0, limit + 1) for c in "，,、 ；;"), default=-1)
        name = name[:cut] if cut >= 6 else name[:limit]
        name = name.rstrip(" ，,、；;") + "…"
    return name or (task.get("issue_key") or "未命名")


def _daily_prompt(author, date, tasks, persona) -> List[dict]:
    """模型只出语义，不出符号也不起名。

    实测它复述精确字符串会出错（把 RachelXiaolan 写成 RachelXiaelan、把已经改掉的
    项目名写回去）。所以链接、短名、状态图标全部由代码渲染，模型只负责说人话和
    判断每条是「做完了 / 还在做 / 卡住了」。
    """
    sys = (
        "你是 %s 的日志助手。把当天推进的几个任务整理成工作日志的内容。\n"
        "硬规则：\n"
        "- 只用给到的进展事实，不许推断、不许补充没写的进展、不许夸大。\n"
        "- **不要写 URL、不要写 issue 号、不要给任务起名、不要写 emoji**——\n"
        "  这些由系统自动加，你写了反而会错。\n"
        "- 每条都要判一个状态：done=做完了 / wip=还在做或部分完成 / blocked=卡住了。\n"
        "  出现「先不做」「待优化」「还没」「下一步」「明天」「暂缓」这类说法的是 wip；\n"
        "  「卡住」「没装成功」「失败了还没解决」是 blocked；其余才是 done。\n"
        "  别偷懒全标 done——一天里总有没做完的事。\n"
        "- 每个任务一句话总结，再列 1-4 条子弹点写具体做了什么、踩了什么坑。\n"
        "- To do 只写进展里明确提到还没做完的事；没有就整段不写。\n"
        "风格要求：%s\n\n"
        "输出格式（严格照此，不要 JSON、不要代码块、不要别的解释）：\n"
        "[1] done | 一句话总结\n"
        "- done | 子弹点\n"
        "- wip | 子弹点\n"
        "[2] wip | 一句话总结\n"
        "- blocked | 子弹点\n"
        "TODO\n"
        "- [1] 跟任务 1 有关的待办\n"
        "- 跟具体任务无关的待办\n"
    ) % (persona.get("display_name") or author, persona.get("daily_style", ""))

    # 把扫描时判好的 kind 一起给它。之前不给，模型只能瞎猜，实测把所有条目
    # 都标成了 done——包括「只读 UI 先不做」「准确率待优化」这种明显没完成的。
    hint = {"pitfall": "[坑]", "decision": "[决定]"}
    blocks = []
    for i, t in enumerate(tasks, 1):
        lines = []
        for u in t["updates"]:
            k = (u.get("meta") or {}).get("kind")
            kind = u.get("content_kind") or k or "progress"
            status = u.get("completion_status") or "unknown"
            lines.append("  - [status=%s kind=%s] %s%s" % (
                status, kind, hint.get(kind, ""),
                u["content_md"].strip().replace("\n", " ")))
        blocks.append("[%d]\n%s" % (i, "\n".join(lines)))
    user = "日期：%s\n\n今天推进了 %d 个任务：\n\n%s" % (date, len(tasks), "\n\n".join(blocks))
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


def _split_status(text: str) -> Tuple[str, str]:
    """从 `status | 正文` 里取出状态；没写时保持中性，不冒充已完成。"""
    head, sep, rest = text.partition("|")
    key = head.strip().lower()
    if sep and key in STATUS_ICON:
        return key, rest.strip()
    return "unknown", text.strip()


def _parse_daily(raw: str, n_tasks: int) -> Tuple[Dict[int, Dict[str, Any]], List[Tuple[Optional[int], str]]]:
    """解析成 {任务序号: {status, summary, bullets}} 和 [(任务序号|None, 待办)]。"""
    raw = re.sub(r"^```\w*\s*|\s*```$", "", raw.strip())
    items: Dict[int, Dict[str, Any]] = {}
    todos: List[Tuple[Optional[int], str]] = []
    cur, in_todo = None, False
    for line in raw.splitlines():
        if not line.strip():
            continue
        if re.match(r"^\s*todo\s*[:：]?\s*$", line, re.I):
            in_todo, cur = True, None
            continue
        m = re.match(r"^\s*\[(\d+)\]\s*(.*)$", line)
        if m and not in_todo:
            idx = int(m.group(1))
            if 1 <= idx <= n_tasks:
                status, summary = _split_status(m.group(2))
                cur = idx
                items[idx] = {"status": status, "summary": summary, "bullets": []}
            continue
        b = re.match(r"^\s*[-*•]\s*(.+)$", line)
        if not b:
            continue
        text = b.group(1).strip()
        if in_todo:
            m2 = re.match(r"^\[(\d+)\]\s*(.+)$", text)
            todos.append((int(m2.group(1)), m2.group(2).strip()) if m2 else (None, text))
        elif cur is not None:
            status, body = _split_status(text)
            items[cur]["bullets"].append((status, body))
    return items, todos


def _link(task: Dict[str, Any], label: str, persona: Dict[str, Any]) -> str:
    """有 issue 就带链接，自由任务不带——链接在这里拼，不经过模型。"""
    if not task.get("issue_key"):
        return "**%s**" % label
    url = persona.get("issue_url_template",
                      "https://mobius.feedmob.com/issue/{issue_key}").format(
        issue_key=task["issue_key"])
    return "[**%s**](%s)" % (label, url)


def _assemble_daily(date, tasks, items, todos, persona) -> str:
    header = persona.get("daily_header", "{date_slash} 工作日志").format(
        date_slash=date.replace("-", "/"), date=date,
        display_name=persona.get("display_name", ""))
    out = ["# %s" % header]
    sections = (("done", "Done"), ("wip", "In Progress"),
                ("blocked", "Blocked"), ("unknown", "Updates"))
    for status_key, label in sections:
        indexes = [i for i in range(1, len(tasks) + 1)
                   if (items.get(i) or {}).get("status", "unknown") == status_key]
        if not indexes:
            continue
        out += ["", "## %s" % label, ""]
        for i in indexes:
            t = tasks[i - 1]
            it = items.get(i) or {}
            icon = STATUS_ICON[status_key]
            head = _link(t, short_name(t, persona), persona)
            summary = it.get("summary", "")
            out.append("%d. %s %s%s" % (i, icon, head, "：" + summary if summary else ""))
            for bullet_status, text in it.get("bullets", []):
                out.append("    * %s %s" % (
                    STATUS_ICON.get(bullet_status, STATUS_ICON["unknown"]), text))
    if todos:
        out += ["", "## To do", ""]
        for n, (idx, text) in enumerate(todos, 1):
            if idx and 1 <= idx <= len(tasks):
                head = _link(tasks[idx - 1], short_name(tasks[idx - 1], persona), persona)
                out.append("%d. %s：%s" % (n, head, text))
            else:
                out.append("%d. %s" % (n, text))
    return "\n".join(out) + "\n"


def _voice_prompt(author, date, daily_md, persona, target_items, feedback=None) -> List[dict]:
    """注意：这里**不**让模型数字数。

    推理模型拿到「必须 200-280 字」会在思维链里反复数中文字符，烧穿 token 预算也吐不出正文。
    字数是确定性问题，交给 _cjk_len 数、_clip 裁；留给模型的是它擅长的结构性目标。
    """
    sys = (
        "你把工作日志改写成一段口播稿，供本人照着念、录成语音发 Slack。\n"
        "要求：\n"
        "- 讲 %d 件事，每件事一到两句话，按日志里的顺序。\n"
        "- 结尾一句明天打算；日志里没写就说一句收尾的话。\n"
        "- 纯口语连续文本：不要标题、不要序号、不要 markdown 符号、不要念 issue 号。\n"
        "- 只讲日志里有的事，不要编。\n"
        "- 直接输出口播稿正文，不要任何解释。\n"
        "风格要求：%s"
    ) % (target_items, persona.get("voice_style", ""))
    user = "日期：%s\n\n今天的工作日志：\n\n%s" % (date, daily_md)
    if feedback:
        user += "\n\n上一版需要调整：%s\n请重写。" % feedback
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


# ---------- fallback（LLM 不可用时的确定性产物） ----------

def _fallback_daily(author, date, tasks, persona) -> str:
    """LLM 挂了也要出同样结构的稿——链接、短名、图标照拼，内容用进展原文顶上。"""
    items = {}
    for i, t in enumerate(tasks, 1):
        updates = t["updates"]
        ups = [u["content_md"].strip().splitlines()[0] for u in updates]
        statuses = [u.get("completion_status", "unknown") for u in updates]
        if "blocked" in statuses:
            task_status = "blocked"
        elif "wip" in statuses:
            task_status = "wip"
        elif statuses and all(s == "done" for s in statuses):
            task_status = "done"
        else:
            task_status = "unknown"
        items[i] = {"status": task_status, "summary": ups[0] if ups else "",
                    "bullets": [(statuses[n], u) for n, u in enumerate(ups[1:], 1)]}
    return (_assemble_daily(date, tasks, items, [], persona)
            + "\n> 本篇为兜底稿（LLM 不可用），内容取自进展原文，未经整理。\n")


def _fallback_voice(author, date, tasks, persona, hi: int) -> str:
    items = [t["title"].rstrip("。")[:36] for t in tasks[:4]]
    text = "今天主要推进了这么几件事：%s。以上就是今天的进展。" % "；".join(items)
    return _clip(text, hi) if _cjk_len(text) > hi else text


def _clip(text: str, hi: int) -> str:
    parts = re.split(r"(?<=[。！？])", text)
    out = ""
    for p in parts:
        if _cjk_len(out + p) > hi:
            break
        out += p
    return (out or text[:hi]).strip()


# ---------- 主流程 ----------

_VERIFY_PROMPT = """下面是同一个人一天里记下的工作进展。请判断每一条属于哪个 issue。

规则：
- 看的是**说的是不是同一件事**，不是字面有没有重合的词。
  比如「闲鱼选品调研」和「有关 Linux.do 积分、开小店」字面零重合，但很可能是同一件事。
- 每条独立判断。**不要因为相邻的几条归了同一个 issue 就跟着归**。
- 真的都不属于就写 `-`。**宁可写 `-` 也不要硬凑**——归错了日报全跟着错。
- 只能从下面给的列表里选，不许自己编 issue 号。

{issues}

进展列表：
{entries}

输出格式：一行一条，`序号 | issue号或-`。不要 JSON、不要代码块、不要解释。
必须每条都给，序号和上面一一对应。
"""


def _verification_fingerprint(rows: List[Dict[str, Any]],
                              issues: List[Dict[str, Any]]) -> str:
    payload = {
        "prompt": VERIFY_PROMPT_VERSION,
        "model": config.LLM_MODEL,
        "rows": [[r["update_id"], r.get("revision", 1), r["content_md"],
                  r.get("issue_key"), r.get("assignment_locked", 0)] for r in rows],
        "issues": [[i["issue_key"], i.get("title"), i.get("state"), i.get("updated_at")]
                   for i in issues],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _issue_cache_error(issues: List[Dict[str, Any]]) -> Optional[str]:
    if not issues:
        return "Mobius issue 缓存为空，跳过交叉验证；请先 sync_issues"
    try:
        latest = max(datetime.fromisoformat(i["synced_at"].replace("Z", "+00:00"))
                     for i in issues if i.get("synced_at"))
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - latest.astimezone(timezone.utc)).total_seconds() / 3600
    except (ValueError, TypeError):
        return "Mobius issue 缓存时间无效，视为已过期；请先 sync_issues"
    if age_hours > config.MOBIUS_CACHE_MAX_AGE_HOURS:
        return "Mobius issue 缓存已过期（%.1f 小时），跳过交叉验证；请先 sync_issues" % age_hours
    return None


def _verification_cached(author: str, date: str, fp: str) -> bool:
    with db.cursor() as conn:
        row = conn.execute(
            "SELECT fingerprint FROM assignment_verifications WHERE author=? AND date=?",
            (author, date),
        ).fetchone()
    return bool(row and row["fingerprint"] == fp)


def _cache_verification(author: str, date: str, fp: str) -> None:
    with db.cursor() as conn:
        conn.execute(
            "INSERT INTO assignment_verifications"
            " (author,date,fingerprint,model,prompt_version,verified_at) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(author,date) DO UPDATE SET fingerprint=excluded.fingerprint,"
            " model=excluded.model,prompt_version=excluded.prompt_version,"
            " verified_at=excluded.verified_at",
            (author, date, fp, config.LLM_MODEL, VERIFY_PROMPT_VERSION, store.now_iso()),
        )


def verify_assignments(author: str, date: str) -> Dict[str, Any]:
    """把当天所有进展的归属重判一次，不管它当初是怎么归的。

    为什么连 explicit 也要重判：那个 issue 号是调用方 agent 填的，**它也是模型的
    判断**，一样会错——记的时候手上只有当前这一条的上下文，这里能看到一整天。
    两边不一致时以这里为准，但把改动如实报出来，不闷声改。
    """
    from . import mobius

    out: Dict[str, Any] = {"checked": 0, "changed": [], "error": None, "cached": False}
    if not config.llm_configured():
        out["error"] = "没配 LLM，跳过交叉验证"
        return out

    # 人工在 Review 页确认/改过的归属已经是最终判断，模型不得覆盖。
    rows = [r for r in db.day_updates(author, date) if not r.get("assignment_locked")]
    if not rows:
        return out
    issues = mobius.cached_issues(author)
    cache_error = _issue_cache_error(issues)
    if cache_error:
        out["error"] = cache_error
        return out
    valid = {i["issue_key"] for i in issues}
    before_fp = _verification_fingerprint(rows, issues)
    if _verification_cached(author, date, before_fp):
        out["cached"] = True
        return out

    listing = ("候选 issue（只能从这里选）：\n"
               + "\n".join("- %s：%s" % (i["issue_key"], i["title"]) for i in issues)
               ) if issues else "（当前没有在办的 issue，全部写 `-`）"
    body = "\n".join("%d | %s" % (n, r["content_md"].strip().replace("\n", " "))
                      for n, r in enumerate(rows, 1))
    prompt = _VERIFY_PROMPT.replace("{issues}", listing).replace("{entries}", body)

    try:
        raw = llm.chat([{"role": "user", "content": prompt}],
                       temperature=0.1, max_tokens=max(2000, 60 * len(rows)))
    except llm.LLMError as exc:
        out["error"] = str(exc)[:160]
        return out

    verdicts: Dict[int, Optional[str]] = {}
    for line in re.sub(r"^```\w*\s*|\s*```$", "", raw.strip()).splitlines():
        m = re.match(r"^\s*(\d+)\s*\|\s*(\S+)", line)
        if not m:
            continue
        key = m.group(2).strip().upper()
        verdicts[int(m.group(1))] = key if key in valid else None

    out["checked"] = len(verdicts)
    for n, row in enumerate(rows, 1):
        if n not in verdicts:
            continue                      # 模型没给这条，保持原样
        want, have = verdicts[n], row["issue_key"]
        if want == have:
            continue
        moved = store.reassign(row["update_id"], author, want)
        if moved:
            out["changed"].append({
                "content": row["content_md"][:40],
                "from": have or "自由任务", "to": want or "自由任务",
                "was": row["match_method"],
            })
    # reassign 会递增 revision/改变 task，缓存最终状态的指纹，下一次相同输入零调用。
    if verdicts:
        final_rows = [r for r in db.day_updates(author, date) if not r.get("assignment_locked")]
        _cache_verification(author, date, _verification_fingerprint(final_rows, issues))
    return out


def generate(author: str, date: str, force: bool = False,
             persona_name: Optional[str] = None) -> Dict[str, Any]:
    from . import auth

    ident = auth.all_authors().get(author, {})
    persona = personas.load(persona_name or ident.get("persona") or author)
    if not persona.get("display_name"):
        persona["display_name"] = ident.get("display_name") or author

    # 出稿前先把归属重判一次。记的时候手上只有当前那一条的上下文，这里能看到
    # 一整天——连 agent 明确填的 issue 号也重判，那同样是模型的判断，一样会错。
    verified = verify_assignments(author, date)

    tasks = db.day_tasks(author, date)
    n_updates = sum(len(t["updates"]) for t in tasks)
    pto_status = pto.status(author, date)
    fp = fingerprint(tasks, persona, pto_status)

    existing = db.get_report(author, date, "daily")
    if existing and existing["fingerprint"] == fp and not force:
        return _result(author, date, "skipped", pto_status, len(tasks), n_updates,
                       reason="输入未变，跳过重生成")

    if pto_status == "pto" and not tasks:
        daily = "# %s 工作日志 · %s\n\n休假（PTO），当日无任务进展，日志豁免。\n" % (
            date, persona["display_name"])
        voice = "今天请假，没有工作内容，跳过日志。"
        _persist(author, date, "daily", daily, fp, "pto-exempt", None, 0)
        _persist(author, date, "voice", voice, fp, "pto-exempt", None, 0)
        _write_files(author, date, daily, voice, persona)
        _stamp_pto(author, date, "pto")
        return _result(author, date, "pto-exempt", pto_status, 0, 0)

    if not tasks:
        return _result(author, date, "empty", pto_status, 0, 0, reason="当日无进展，未生成")

    lo, hi = persona.get("voice_target_chars", [config.VOICE_MIN_CHARS, config.VOICE_MAX_CHARS])
    warnings: List[str] = []
    # 改了归属就说出来，不闷声改
    for c in verified["changed"]:
        warnings.append("归属订正：%s → %s（原为 %s）「%s」"
                        % (c["from"], c["to"], c["was"], c["content"]))
    if verified.get("error"):
        warnings.append("交叉验证未执行：%s" % verified["error"])
    model = config.LLM_MODEL

    # 日报和口播稿各自独立降级：一个挂了不该把另一个也拖成兜底稿。
    try:
        # 预算按任务数给。上一版固定 4000，28 条进展的日报被截断在半句话上，
        # 后两个任务只剩标题。宁可给多，llm.chat 那边本来就有截断重试。
        budget = max(4000, 1200 * len(tasks) + 2000)
        raw = llm.chat(_daily_prompt(author, date, tasks, persona), max_tokens=budget)
        items, todos = _parse_daily(raw, len(tasks))
        if len(items) < len(tasks):
            # 少解析出任务块，多半是被 max_tokens 截断了——翻倍再来一次。
            raw = llm.chat(_daily_prompt(author, date, tasks, persona),
                           max_tokens=min(budget * 2, llm.MAX_TOKEN_CEILING))
            items, todos = _parse_daily(raw, len(tasks))
        if not items:
            raise llm.LLMError("没解析出任何任务块，原样片段：%s" % raw[:200])
        if len(items) < len(tasks):
            warnings.append("只整理出 %d/%d 个任务，其余可能因长度被截断"
                            % (len(items), len(tasks)))
        # 链接和结构在这里拼死，模型碰不到——它写错 URL 的账已经吃过一次了。
        daily = _assemble_daily(date, tasks, items, todos, persona)
        daily_gen = "llm"
    except llm.LLMError as exc:
        warnings.append("日报 LLM 失败（%s），已输出兜底稿" % str(exc)[:160])
        daily = _fallback_daily(author, date, tasks, persona)
        daily_gen = "fallback"

    try:
        items = max(2, min(4, len(tasks)))
        voice = llm.chat(_voice_prompt(author, date, daily, persona, items),
                         temperature=0.6, max_tokens=4000)
        n = _cjk_len(voice)
        if not (lo <= n <= hi):
            warnings.append("首版口播稿 %d 字，%s目标区间 %d-%d，已重试"
                            % (n, "长于" if n > hi else "短于", lo, hi))
            if n > hi:
                items = max(2, items - 1)
                fb = "太长了，讲 %d 件事就够，每件压到一句话。" % items
            else:
                items = min(4, items + 1)
                fb = "太短了，多讲一件事，每件事展开到两句话。"
            voice = llm.chat(_voice_prompt(author, date, daily, persona, items, feedback=fb),
                             temperature=0.6, max_tokens=4000)
            n = _cjk_len(voice)
            if n > hi:
                voice = _clip(voice, hi)
                warnings.append("重试后仍 %d 字，已按句号边界裁剪至 %d 字" % (n, _cjk_len(voice)))
            elif n < lo:
                # 真实模型复验里第二版仍只有 173 字。再给一次“结构性展开”机会，
                # 仍不要求模型数字数，避免推理模型把预算耗在计数上。
                voice = llm.chat(_voice_prompt(
                    author, date, daily, persona, min(4, items + 1),
                    feedback="仍然太短。把每件事的结果、关键做法和影响各讲一句，"
                             "结尾补充下一步，保持自然口语。"),
                    temperature=0.4, max_tokens=4000)
                n = _cjk_len(voice)
                if n > hi:
                    voice = _clip(voice, hi)
                    warnings.append("二次重试后 %d 字，已按句号边界裁剪至 %d 字"
                                    % (n, _cjk_len(voice)))
                elif n < lo:
                    warnings.append("二次重试后仍 %d 字，短于目标下限，按原样输出" % n)
        voice_gen = "llm"
    except llm.LLMError as exc:
        warnings.append("口播稿 LLM 失败（%s），已输出兜底稿" % str(exc)[:160])
        voice = _fallback_voice(author, date, tasks, persona, hi)
        voice_gen = "fallback"

    generator = daily_gen if daily_gen == voice_gen else "%s+%s" % (daily_gen, voice_gen)

    if pto_status == "pto":
        daily = "> 当日为 PTO（请假），以下为期间仍产生的进展。\n\n" + daily
        warnings.append("当日为 PTO，日报已降级标注")

    _persist(author, date, "daily", daily, fp, daily_gen,
             model if daily_gen == "llm" else None, n_updates, warnings)
    _persist(author, date, "voice", voice, fp, voice_gen,
             model if voice_gen == "llm" else None, n_updates, warnings)
    paths = _write_files(author, date, daily, voice, persona)
    _stamp_pto(author, date, pto_status)

    res = _result(author, date, "generated", pto_status, len(tasks), n_updates)
    res.update({
        "generator": generator,
        "model": model if "llm" in generator else None,
        "warnings": warnings,
        "files": paths,
        "voice_chars": _cjk_len(voice),
        "voice_seconds_est": speaking_seconds(voice),
        "daily_md": daily,
        "voice_md": voice,
    })
    return res


def _stamp_pto(author: str, date: str, status: str) -> None:
    with db.cursor() as conn:
        conn.execute("UPDATE updates SET pto_status=? WHERE author=? AND date=?",
                     (status, author, date))


def _persist(author, date, kind, content, fp, generator, model, n,
             warnings: Optional[List[str]] = None) -> None:
    prev = db.get_report(author, date, kind)
    with db.cursor() as conn:
        if prev:
            conn.execute(
                "INSERT INTO report_history (author,date,kind,content_md,generator,created_at)"
                " VALUES (?,?,?,?,?,?)",
                (author, date, kind, prev["content_md"], prev["generator"], prev["created_at"]))
        conn.execute(
            "INSERT INTO reports (report_id,author,date,kind,content_md,fingerprint,"
            "generator,model,entry_count,char_count,warnings,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(author,date,kind) DO UPDATE SET content_md=excluded.content_md,"
            " fingerprint=excluded.fingerprint, generator=excluded.generator,"
            " model=excluded.model, entry_count=excluded.entry_count,"
            " char_count=excluded.char_count, warnings=excluded.warnings,"
            " created_at=excluded.created_at",
            (str(uuid.uuid4()), author, date, kind, content, fp, generator, model,
             n, _cjk_len(content), json.dumps(warnings or [], ensure_ascii=False), store.now_iso()))


def _write_files(author, date, daily, voice, persona) -> Dict[str, str]:
    out_dir = config.LOGS_DIR / date
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "day_page": out_dir / ("%s.md" % author),
        "daily": out_dir / ("%s-日报.md" % author),
        "voice": out_dir / ("%s-口播稿.md" % author),
    }
    files["day_page"].write_text(store.day_page(author, date), encoding="utf-8")
    files["daily"].write_text(daily.rstrip() + "\n", encoding="utf-8")
    header = "# %s 口播稿 · %s\n\n> 约 %d 字 / %d 秒 · 照读即可，语音请本人录\n\n" % (
        date, persona.get("display_name") or author, _cjk_len(voice), speaking_seconds(voice))
    files["voice"].write_text(header + voice.strip() + "\n", encoding="utf-8")
    return {k: str(v) for k, v in files.items()}


def _result(author, date, status, pto_status, n_tasks, n_updates, reason=None) -> Dict[str, Any]:
    out = {"author": author, "date": date, "status": status, "pto_status": pto_status,
           "task_count": n_tasks, "update_count": n_updates}
    if reason:
        out["reason"] = reason
    return out


def run_all(date: str, force: bool = False) -> List[Dict[str, Any]]:
    from . import auth

    authors = sorted(set(list(auth.all_authors().keys()) +
                         [u["author"] for u in db.list_updates(date=date)]))
    return [generate(a, date, force=force) for a in authors]


def main() -> None:
    ap = argparse.ArgumentParser(description="Fecho 日终整理（cron 入口）")
    ap.add_argument("--date", default=store.today())
    ap.add_argument("--author", default=None)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    db.init()
    results = [generate(args.author, args.date, force=args.force)] if args.author \
        else run_all(args.date, force=args.force)
    for r in results:
        line = "[%s] %s %s · %d 个任务 / %d 条进展 · pto=%s" % (
            r["status"], r["date"], r["author"], r["task_count"], r["update_count"],
            r["pto_status"])
        if r.get("generator"):
            line += " · %s" % r["generator"]
        if r.get("voice_chars"):
            line += " · 口播 %d 字/%ds" % (r["voice_chars"], r["voice_seconds_est"])
        print(line)
        for w in r.get("warnings", []):
            print("    ! %s" % w)
        for p in (r.get("files") or {}).values():
            print("    -> %s" % p)


if __name__ == "__main__":
    main()
