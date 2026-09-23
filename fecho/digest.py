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
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import config, db, llm, personas, pto, store


REPORT_PROMPT_VERSION = "daily-v2-status-fields"
VERIFY_PROMPT_VERSION = "assignment-v4-learned-hints"


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

# 标题里括号包着的后缀（「（Q3）」「[内部]」这类）对名字没信息量，砍掉
_TITLE_TRIM = re.compile(r"[（(【\[].*?[）)】\]]")


def task_key(task: Dict[str, Any]) -> str:
    """短名按什么存：有 issue 就按 issue 号（同一个 issue 天天叫同一个名），自由任务按任务 ID。"""
    return task.get("issue_key") or "task:%s" % task["task_id"]


def short_name(task: Dict[str, Any], persona: Dict[str, Any],
               aliases: Optional[Dict[str, str]] = None) -> str:
    """任务在日报里显示的名字：人配的别名 > 模型起的短名 > 完整标题。

    模型起的短名见 task_aliases()：起一次就存下来，起得不像名字就不用。

    不截断、不加省略号。真踩过：「agent 原生 time-off：Fecho」先按冒号砍掉了后半截，
    剩下的又超长被截成「agent 原生…」——最能认出是哪件事的「Fecho」反而没了。
    """
    manual = persona.get("task_aliases") or {}
    if task.get("issue_key") and task["issue_key"] in manual:
        return manual[task["issue_key"]]
    auto = (aliases or {}).get(task_key(task))
    if auto:
        return auto

    title = (task.get("title") or "").strip()
    name = _TITLE_TRIM.sub("", title).strip(" -—、,，。") or title
    return name or (task.get("issue_key") or "未命名")


# ---------- 任务短名 ----------
# 日报里任务的名字由模型看 issue 标题和进展来起。真踩过模型起名的坑：它会把当天还在
# 讨论的候选名当成已定（把 fecho 写成 fmjot）。所以两道保险：
#   1. 起一次就存下来，以后一直用——不会今天一个明天一个，也不会被某天的讨论带偏
#   2. 起得不像名字（太长、带链接、带竖线、就是个 issue 号）就不用，退回完整标题
ALIAS_MAX_CHARS = 12


def _valid_alias(text: str) -> Optional[str]:
    name = re.sub(r"^[\s*`\"'「『【]+|[\s*`\"'」』】。，,;；:：]+$", "", text or "")
    if not name or "\n" in name or len(name) > ALIAS_MAX_CHARS:
        return None
    if re.search(r"https?://|[|\[\]()]", name) or re.fullmatch(r"[A-Za-z]+-\d+", name):
        return None
    return name


def _alias_prompt(tasks: List[Dict[str, Any]]) -> List[dict]:
    sys = (
        "你给工作日志里的任务起短名，让人在日报里一眼认出是哪件事。\n"
        "规则：\n"
        "- 每个短名不超过 %d 个字符，是个名字，不是一句话。\n"
        "- 优先用 issue 标题里已经有的项目名、产品名、专有名词。"
        "比如标题「agent 原生 time-off：Fecho」就叫「Fecho」。\n"
        "- 标题里没有现成的名字，再根据进展内容概括这是哪件事。\n"
        "- 不要用进展里还在讨论中的候选名。\n"
        "- 不要写 issue 号、链接、emoji、引号。\n"
        "输出格式：一行一个，`[序号] 短名`，不要别的。"
    ) % ALIAS_MAX_CHARS
    blocks = []
    for i, t in enumerate(tasks, 1):
        head = ("%s %s" % (t["issue_key"], t["title"]) if t.get("issue_key")
                else "（自由任务）%s" % t.get("title", ""))
        ups = [u["content_md"].strip().splitlines()[0][:80]
               for u in t["updates"][:5] if (u.get("content_md") or "").strip()]
        blocks.append("[%d] %s\n%s" % (i, head, "\n".join("  - " + x for x in ups)))
    return [{"role": "system", "content": sys}, {"role": "user", "content": "\n\n".join(blocks)}]


def _parse_aliases(raw: str, n: int) -> Dict[int, str]:
    out: Dict[int, str] = {}
    for line in re.sub(r"^```\w*\s*|\s*```$", "", (raw or "").strip()).splitlines():
        m = re.match(r"^\s*\[(\d+)\]\s*(.+)$", line)
        if m and 1 <= int(m.group(1)) <= n:
            name = _valid_alias(m.group(2))
            if name:
                out[int(m.group(1))] = name
    return out


def task_aliases(author: str, tasks: List[Dict[str, Any]], persona: Dict[str, Any],
                 warnings: Optional[List[str]] = None) -> Dict[str, str]:
    """这个人所有任务的短名。还没起过名的，让模型起一次并存下来。

    模型不可用或起名失败，不影响出日报——那几个任务先用完整标题。
    """
    with db.cursor() as conn:
        known = {r["task_key"]: r["alias"] for r in conn.execute(
            "SELECT task_key, alias FROM task_aliases WHERE author=?", (author,)).fetchall()}
    manual = persona.get("task_aliases") or {}
    missing = [t for t in tasks
               if task_key(t) not in known and t.get("issue_key") not in manual]
    if not missing or not config.llm_configured():
        return known
    try:
        raw = llm.chat(_alias_prompt(missing), temperature=0.2, max_tokens=2000)
    except llm.LLMError as exc:
        if warnings is not None:
            warnings.append("任务短名没起成（%s），先用完整标题" % str(exc)[:120])
        return known
    now = store.now_iso()
    with db.cursor() as conn:
        for idx, name in _parse_aliases(raw, len(missing)).items():
            key = task_key(missing[idx - 1])
            conn.execute(
                "INSERT INTO task_aliases (author, task_key, alias, created_at) VALUES (?,?,?,?)"
                " ON CONFLICT (author, task_key) DO NOTHING", (author, key, name, now))
            known.setdefault(key, name)
    return known


def _daily_prompt(author, date, tasks, persona, style_md: str = "") -> List[dict]:
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
    sys += style_md          # 这个人的写作偏好（style.py），没有就是空串

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


def _assemble_daily(date, tasks, items, todos, persona, aliases=None) -> str:
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
            head = _link(t, short_name(t, persona, aliases), persona)
            summary = it.get("summary", "")
            out.append("%d. %s %s%s" % (i, icon, head, "：" + summary if summary else ""))
            for bullet_status, text in it.get("bullets", []):
                out.append("    * %s %s" % (
                    STATUS_ICON.get(bullet_status, STATUS_ICON["unknown"]), text))
    if todos:
        out += ["", "## To do", ""]
        for n, (idx, text) in enumerate(todos, 1):
            if idx and 1 <= idx <= len(tasks):
                head = _link(tasks[idx - 1], short_name(tasks[idx - 1], persona, aliases), persona)
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

def _fallback_items(tasks: List[Dict[str, Any]], indexes: Optional[List[int]] = None) -> Dict[int, Dict[str, Any]]:
    """用进展原文拼出任务条目（序号是全局序号）。整篇兜底和某一批降级都用它。"""
    items = {}
    for i in indexes or range(1, len(tasks) + 1):
        t = tasks[i - 1]
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
    return items


def _fallback_daily(author, date, tasks, persona, aliases=None) -> str:
    """LLM 挂了也要出同样结构的稿——链接、短名、图标照拼，内容用进展原文顶上。"""
    items = _fallback_items(tasks)
    return (_assemble_daily(date, tasks, items, [], persona, aliases)
            + "\n> 本篇为兜底稿（LLM 不可用），内容取自进展原文，未经整理。\n")


# ---------- 分批写日报 ----------
# 一次调用写整天，任务多的日子写不完：9/23 11 个任务 / 155 条进展，推理模型 + 非流式，
# 180 秒超时，整篇掉成兜底稿。按任务切成几批分别写，每批都远小于上限。
BATCH_TASKS = 4          # 一批最多几个任务
BATCH_UPDATES = 40       # 一批大约多少条进展（单个任务超了也不拆：它的总结要看到全部进展）
BATCH_WORKERS = 3        # 同时跑几批。总耗时约等于最慢那批，而不是几批加起来


def _batches(tasks: List[Dict[str, Any]]) -> List[List[int]]:
    """按顺序切批，返回每批的全局任务序号（从 1 起）。"""
    out: List[List[int]] = []
    cur: List[int] = []
    n = 0
    for i, t in enumerate(tasks, 1):
        k = len(t["updates"])
        if cur and (len(cur) >= BATCH_TASKS or n + k > BATCH_UPDATES):
            out.append(cur)
            cur, n = [], 0
        cur.append(i)
        n += k
    if cur:
        out.append(cur)
    return out


# 模型常在待办前面自己写上任务名（「**Bug Hunter**：…」「**曝光权限**：…」），而名字和链接
# 是系统拼的：留着就成了「[**Binance**](…)：**曝光权限与 IPM 验证**：…」这种双重标题
_TODO_LABEL = re.compile(r"^\s*\*\*[^*\n]{1,40}\*\*\s*[:：]\s*")


def _clean_todo(text: str) -> str:
    while _TODO_LABEL.match(text):
        text = _TODO_LABEL.sub("", text, count=1)
    return text.strip()


def _write_batch(author, date, tasks, indexes, persona, style_md):
    """写一批，返回 (条目, 待办, 提示)，序号都已换回全局序号。失败抛 LLMError。"""
    sub = [tasks[i - 1] for i in indexes]
    # 预算按任务数给。固定 4000 时 28 条进展被截断在半句话上；分批后单批小得多
    budget = min(max(4000, 1200 * len(sub) + 2000), llm.MAX_TOKEN_CEILING)
    prompt = _daily_prompt(author, date, sub, persona, style_md)
    raw = llm.chat(prompt, max_tokens=budget)
    items, todos = _parse_daily(raw, len(sub))
    if len(items) < len(sub):
        # 少解析出任务块，多半是被 max_tokens 截断了——加大额度再来一次，只能加不能减
        raw = llm.chat(prompt, max_tokens=max(budget, min(budget * 2, llm.MAX_TOKEN_CEILING)))
        items, todos = _parse_daily(raw, len(sub))
    if not items:
        raise llm.LLMError("没解析出任何任务块，原样片段：%s" % raw[:200])
    notes = []
    if len(items) < len(sub):
        notes.append("有 %d 个任务没写出来，可能因长度被截断" % (len(sub) - len(items)))
    mapped = {indexes[j - 1]: v for j, v in items.items()}
    mapped_todos = []
    for j, text in todos:
        text = _clean_todo(text)
        if not text:
            continue
        if not j and len(sub) == 1:
            j = 1          # 这一批只有一个任务：待办只能出自它的进展
        mapped_todos.append((indexes[j - 1] if j and 1 <= j <= len(sub) else None, text))
    return mapped, mapped_todos, notes


def _write_daily(author, date, tasks, persona, style_md, warnings, step):
    """分批写，合并成一篇。返回 (条目, 待办, 失败的批数, 总批数)。

    某一批失败只让那一批用进展原文顶上，不拖垮整篇；每写完一批报一次进度。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    batches = _batches(tasks)
    n_updates = sum(len(t["updates"]) for t in tasks)
    total = len(batches)
    step("daily", "已写完 0/%d 批（%d 个任务 / %d 条进展）" % (total, len(tasks), n_updates))
    items: Dict[int, Dict[str, Any]] = {}
    todos: List[Tuple[Optional[int], str]] = []
    failed: List[Tuple[int, List[int], str]] = []
    done = 0
    results: Dict[int, Any] = {}
    with ThreadPoolExecutor(max_workers=min(BATCH_WORKERS, total)) as pool:
        futures = {pool.submit(_write_batch, author, date, tasks, b, persona, style_md): k
                   for k, b in enumerate(batches)}
        for fut in as_completed(futures):
            k = futures[fut]
            try:
                results[k] = fut.result()
            except llm.LLMError as exc:
                results[k] = exc
            done += 1
            step("daily", "已写完 %d/%d 批（%d 个任务 / %d 条进展）" % (done, total, len(tasks), n_updates))
    seen_todo = set()
    for k, b in enumerate(batches):            # 按原顺序合并，不按完成先后
        r = results[k]
        if isinstance(r, Exception):
            failed.append((k + 1, b, str(r)))
            items.update(_fallback_items(tasks, b))
            continue
        got, got_todos, notes = r
        items.update(got)
        for idx, text in got_todos:
            key = (idx, text.strip())
            if key not in seen_todo:
                seen_todo.add(key)
                todos.append((idx, text))
        for note in notes:
            warnings.append("第 %d/%d 批：%s" % (k + 1, total, note))
    for k, b, err in failed:
        warnings.append("日报第 %d/%d 批（任务 %s）LLM 失败（%s），这几项用了进展原文"
                        % (k, total, "、".join(str(i) for i in b), err[:120]))
    return items, todos, len(failed), total


def _fallback_voice(author, date, tasks, persona, lo: int, hi: int) -> str:
    """模型不可用时也交付可直接朗读、且满足配置区间的确定性口播稿。"""
    paragraphs = ["今天的工作按已经记录的事实整理，主要有下面这些进展。"]
    labels = {"done": "已经完成", "wip": "仍在推进", "blocked": "目前受阻",
              "unknown": "当前状态尚未确认"}
    for task in tasks[:4]:
        updates = task.get("updates") or []
        snippets = [u.get("content_md", "").strip().splitlines()[0]
                    for u in updates if u.get("content_md")]
        statuses = [u.get("completion_status", "unknown") for u in updates]
        if "blocked" in statuses:
            status = "blocked"
        elif "wip" in statuses:
            status = "wip"
        elif statuses and all(value == "done" for value in statuses):
            status = "done"
        else:
            status = "unknown"
        detail = "；".join(snippets[:3]) or task.get("title", "这项工作")
        paragraphs.append("关于%s，%s。具体记录是：%s。" % (
            task.get("title", "这项工作").rstrip("。")[:36], labels[status], detail))

    paragraphs.append("以上就是今天已经确认的工作进展，后续有新结果再继续更新。")
    safeguards = [
        "这份口播只根据今天已经记录的事实整理，不补充尚未发生或尚未确认的内容。",
        "已经完成的部分按完成说明，仍在推进和受阻的部分保留当前状态，避免把计划写成成果。",
        "如果后续补充了新进展、修正了任务归属或调整了状态，再重新生成一版即可。",
        "目前先以这份记录作为今天的工作基线，方便明天开工时继续跟进。",
        "回顾时重点看结果、阻塞原因和已经做出的决定，不需要回放完整对话。",
        "没有记录到的细节保持空白，宁可少写，也不把推测混入正式工作日志。",
    ]
    text = "\n\n".join(paragraphs)
    for sentence in safeguards:
        if _cjk_len(text) >= lo:
            break
        text += "\n\n" + sentence
    if _cjk_len(text) > hi:
        text = _clip(text, hi)
    return text


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


HINT_EXAMPLES = 3     # 每个 issue 最多带几条「人确认过归到这里」的进展
HINT_CHARS = 40


def issue_hints(author: str, keys: List[str]) -> Dict[str, Dict[str, List[str]]]:
    """从人的纠正里学：每个 issue 叫过什么名字、人确认过哪些进展属于它。

    - 短名：日报里给这个 issue 起的名字（「Bug Hunter」）
    - 并进来的任务名：人把哪些自由任务合并进了它
    - 确认过的进展：人锁定归到它的几条，新的在前
    纠正一次，下次模型就认得——不用每天在同一个地方再纠一遍。
    """
    if not keys:
        return {}
    out: Dict[str, Dict[str, List[str]]] = {k: {"names": [], "examples": []} for k in keys}
    with db.cursor() as conn:
        for r in conn.execute("SELECT task_key, alias FROM task_aliases WHERE author=?",
                              (author,)).fetchall():
            if r["task_key"] in out:
                out[r["task_key"]]["names"].append(r["alias"])
        merged = conn.execute(
            "SELECT e.from_task_id, t.issue_key FROM task_events e JOIN tasks t ON t.task_id=e.to_task_id"
            " WHERE e.author=? AND e.event_type='merge' AND e.actor='human' AND t.issue_key IS NOT NULL"
            " ORDER BY e.created_at DESC", (author,)).fetchall()
        aliases = {r["task_key"]: r["alias"] for r in conn.execute(
            "SELECT task_key, alias FROM task_aliases WHERE author=? AND task_key LIKE 'task:%'",
            (author,)).fetchall()}
        for r in merged:
            slot = out.get(r["issue_key"])
            src = db.get_task(r["from_task_id"])
            if slot is None or not src or src.get("issue_key"):
                continue
            name = aliases.get("task:%s" % src["task_id"]) or src["title"]
            if name and name not in slot["names"] and len(slot["names"]) < 4:
                slot["names"].append(name)
        rows = conn.execute(
            "SELECT t.issue_key, u.content_md FROM updates u JOIN tasks t ON t.task_id=u.task_id"
            " WHERE u.author=? AND u.assignment_locked=1 AND u.status='active'"
            " AND t.issue_key IS NOT NULL ORDER BY u.created_at DESC LIMIT 300", (author,)).fetchall()
    for r in rows:
        slot = out.get(r["issue_key"])
        if slot is not None and len(slot["examples"]) < HINT_EXAMPLES:
            slot["examples"].append(r["content_md"].strip().replace("\n", " ")[:HINT_CHARS])
    return out


def _issue_line(issue: Dict[str, Any], hint: Optional[Dict[str, List[str]]]) -> str:
    line = "- %s：%s%s" % (issue["issue_key"], issue["title"],
                          "（最近已关闭）" if issue.get("closed") else "")
    extra = []
    if hint and hint["names"]:
        extra.append("也叫：" + "、".join(hint["names"]))
    if issue.get("desc"):
        extra.append("描述：" + issue["desc"])
    if hint and hint["examples"]:
        extra.append("人确认过属于它的进展：" + "；".join("「%s」" % e for e in hint["examples"]))
    return line + "".join("\n    " + e for e in extra)


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

    hints = issue_hints(author, [i["issue_key"] for i in issues])
    listing = ("候选 issue（只能从这里选）：\n"
               + "\n".join(_issue_line(i, hints.get(i["issue_key"])) for i in issues)
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


def persona_for(author: str, persona_name: Optional[str] = None) -> Dict[str, Any]:
    """这个人出日报用的 persona。

    出日报和网页判断「这份日报要不要重出」必须拿同一份：persona 算进了日报指纹，
    两边各取各的，指纹就永远对不上。真踩过——云端这边用数据库里的显示名，
    网页那边用的是邮箱，结果日报刚生成就被标成「需要重新生成」，点多少次都消不掉。
    """
    if config.CLOUD:
        # 云端版：身份来自数据库，不是本机的 token 文件；所有人共用同一套文风
        from . import accounts

        user = accounts.get_user(author) or {}
        persona = personas.load(persona_name or "default")
        persona["display_name"] = user.get("display_name") or author.split("@")[0]
        return persona

    from . import auth

    ident = auth.all_authors().get(author, {})
    persona = personas.load(persona_name or ident.get("persona") or author)
    if not persona.get("display_name"):
        persona["display_name"] = ident.get("display_name") or author
    return persona


def by_priority(author: str, tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """日报里的任务按 Mobius 优先级排：紧急 → 高 → 中 → 低 → 没设的，自由任务最后。
    同一档里保持原来的顺序（当天进展多的在前）。"""
    from . import mobius

    prio = mobius.priorities(author)
    return sorted(tasks, key=lambda t: (
        not t.get("issue_key"), mobius.priority_rank(prio.get(t.get("issue_key") or "", 0))))


def generate(author: str, date: str, force: bool = False,
             persona_name: Optional[str] = None, keep_human: bool = True,
             progress: Optional[Callable[..., None]] = None) -> Dict[str, Any]:
    """出一天的日报和口播稿。

    keep_human：这天的日报被人亲手改过的话，不覆盖。到点出日报、补扫后自动重出都走这个；
    只有人明确点「重新生成」才传 False——覆盖掉的修改版照样留在历史版本里。
    """
    persona = persona_for(author, persona_name)
    step = progress or (lambda *a, **k: None)     # 报进度给网页的进度条；本机版直接调用时没有

    # 出稿前先把归属重判一次。记的时候手上只有当前那一条的上下文，这里能看到
    # 一整天——连 agent 明确填的 issue 号也重判，那同样是模型的判断，一样会错。
    step("verify")
    verified = verify_assignments(author, date)

    tasks = db.day_tasks(author, date)
    n_updates = sum(len(t["updates"]) for t in tasks)
    pto_status = pto.status(author, date)
    fp = fingerprint(tasks, persona, pto_status)
    # 排序放在算指纹之后：只改呈现顺序，不该让所有旧日报都被判成「输入变了」
    tasks = by_priority(author, tasks)

    existing = db.get_report(author, date, "daily")
    # 人写的或改过的日报不覆盖。事实有变时照样出一版自动的，但只放进历史版本给人对照；
    # 网页上提示「有新进展」，由人决定要不要点「重新生成」换成自动版。
    archive_only = bool(existing and existing.get("generator") == "human" and keep_human)
    if archive_only and (not tasks or _archived_fingerprint(author, date) == fp):
        return _result(author, date, "kept-human", pto_status, len(tasks), n_updates,
                       reason="这份日报是人写的，不自动覆盖；要覆盖请点「重新生成」")
    if not archive_only and existing and existing["fingerprint"] == fp and not force:
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
    step("aliases")
    aliases = task_aliases(author, tasks, persona, warnings)
    from . import style
    style_md = style.for_prompt(author)

    # 日报和口播稿各自独立降级：一个挂了不该把另一个也拖成兜底稿。
    items, todos, n_failed, n_batches = _write_daily(author, date, tasks, persona, style_md, warnings, step)
    if n_failed == n_batches:
        # 一批都没写成才算整篇兜底
        warnings.append("日报 LLM 失败（%d 批全部失败），已输出兜底稿" % n_batches)
        daily = _fallback_daily(author, date, tasks, persona, aliases)
        daily_gen = "fallback"
    else:
        # 链接和结构在这里拼死，模型碰不到——它写错 URL 的账已经吃过一次了。
        daily = _assemble_daily(date, tasks, items, todos, persona, aliases)
        if n_failed:
            daily += ("\n> 有 %d/%d 批模型没写成，那几项用的是进展原文，未经整理。\n"
                      % (n_failed, n_batches))
        daily_gen = "llm"

    if archive_only:
        if pto_status == "pto":
            daily = "> 当日为 PTO（请假），以下为期间仍产生的进展。\n\n" + daily
        _archive(author, date, daily, fp, daily_gen)
        res = _result(author, date, "kept-human", pto_status, len(tasks), n_updates,
                      reason="这份日报是人写的，不自动覆盖；自动生成的版本放进了历史版本")
        res["warnings"] = warnings
        return res

    step("voice")
    voice, voice_gen = _make_voice(author, date, daily, tasks, persona, lo, hi, warnings)

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


def _make_voice(author, date, daily, tasks, persona, lo, hi, warnings):
    """按日报写口播稿。字数超界就调结构重写，仍超界按句号裁；模型挂了走兜底稿。"""
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
        voice = _fallback_voice(author, date, tasks, persona, lo, hi)
        voice_gen = "fallback"

    if _cjk_len(voice) < lo:
        warnings.append("口播稿仍短于目标下限，已切换到满足区间的确定性兜底稿")
        voice = _fallback_voice(author, date, tasks, persona, lo, hi)
        voice_gen = "fallback"
    return voice, voice_gen


def regenerate_voice(author: str, date: str) -> Dict[str, Any]:
    """人改完日报后，按改好的日报重出口播稿。日报本身不动。"""
    daily = db.get_report(author, date, "daily")
    if not daily:
        return {"status": "empty", "reason": "这天没有日报"}
    persona = persona_for(author)
    tasks = db.day_tasks(author, date)
    lo, hi = persona.get("voice_target_chars", [config.VOICE_MIN_CHARS, config.VOICE_MAX_CHARS])
    warnings: List[str] = []
    voice, gen = _make_voice(author, date, daily["content_md"], tasks, persona, lo, hi, warnings)
    _persist(author, date, "voice", voice, daily["fingerprint"], gen,
             config.LLM_MODEL if gen == "llm" else None, daily["entry_count"], warnings)
    return {"status": "generated", "generator": gen, "warnings": warnings,
            "task_count": len(tasks), "update_count": daily["entry_count"]}


# 日报里带 issue 链接的任务名：[**名字**](https://…/issue/AI-2541)
_ISSUE_HEAD = re.compile(r"\[\*\*(.+?)\*\*\]\(https?://[^)\s]*/issue/([A-Za-z]+-\d+)\)")


def _renamed_tasks(before: str, after: str) -> Dict[str, str]:
    old = {key.upper(): name for name, key in _ISSUE_HEAD.findall(before or "")}
    new = {key.upper(): name for name, key in _ISSUE_HEAD.findall(after or "")}
    return {key: name for key, name in new.items() if key in old and old[key] != name}


def save_human_edit(author: str, date: str, content_md: str) -> Dict[str, Any]:
    """人亲手改日报。

    - 改之前的版本进历史版本（_persist 本来就这么做）
    - 新版本标成 generator=human；以后到点出日报、补扫后重出都不会覆盖它
    - 指纹沿用原来的：事实没变，不该被标成「需要重新生成」；等真有新进展进来才提示
    - 改了带 issue 链接的任务名，直接把那个任务的短名换成人写的——这是确定的事，不用模型学
    """
    content = (content_md or "").strip()
    if not content:
        raise ValueError("日报不能是空的")
    prev = db.get_report(author, date, "daily")
    if not prev:
        # 还没生成就先手写：指纹按现在的事实算，没有新进展就不提示「需要重新生成」
        tasks = db.day_tasks(author, date)
        fp = fingerprint(tasks, persona_for(author), pto.status(author, date))
        _persist(author, date, "daily", content + "\n", fp, "human", None,
                 sum(len(t["updates"]) for t in tasks))
        return {"status": "saved", "renamed": {}, "new": True}
    if content == (prev["content_md"] or "").strip():
        return {"status": "unchanged", "renamed": {}}
    renamed: Dict[str, str] = {}
    now = store.now_iso()
    with db.cursor() as conn:
        for key, name in _renamed_tasks(prev["content_md"], content).items():
            name = name.strip()
            if not name or len(name) > 40 or "http" in name:
                continue
            conn.execute(
                "INSERT INTO task_aliases (author, task_key, alias, created_at) VALUES (?,?,?,?)"
                " ON CONFLICT (author, task_key) DO UPDATE SET alias=excluded.alias",
                (author, key, name, now))
            renamed[key] = name
    _persist(author, date, "daily", content + "\n", prev["fingerprint"], "human", None,
             prev["entry_count"], prev.get("warnings") or [])
    return {"status": "saved", "renamed": renamed}


def _stamp_pto(author: str, date: str, status: str) -> None:
    with db.cursor() as conn:
        conn.execute("UPDATE updates SET pto_status=? WHERE author=? AND date=?",
                     (status, author, date))


def _archive(author: str, date: str, daily: str, fp: str, generator: str) -> None:
    """人写的日报旁边，把自动生成的版本只存进历史版本。"""
    with db.cursor() as conn:
        conn.execute(
            "INSERT INTO report_history (author,date,kind,content_md,generator,created_at,fingerprint)"
            " VALUES (?,?,?,?,?,?,?)",
            (author, date, "daily", daily, generator, store.now_iso(), fp))


def _archived_fingerprint(author: str, date: str) -> Optional[str]:
    """最近一版自动生成的日报是按什么事实写的。事实没变就不必再出一版放进历史。"""
    with db.cursor() as conn:
        row = conn.execute(
            "SELECT fingerprint FROM report_history WHERE author=? AND date=? AND kind='daily'"
            " AND generator<>'human' ORDER BY created_at DESC, id DESC LIMIT 1",
            (author, date)).fetchone()
    return row["fingerprint"] if row else None


def _persist(author, date, kind, content, fp, generator, model, n,
             warnings: Optional[List[str]] = None) -> None:
    prev = db.get_report(author, date, kind)
    with db.cursor() as conn:
        if prev:
            conn.execute(
                "INSERT INTO report_history (author,date,kind,content_md,generator,created_at,fingerprint)"
                " VALUES (?,?,?,?,?,?,?)",
                (author, date, kind, prev["content_md"], prev["generator"], prev["created_at"],
                 prev["fingerprint"]))
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
    if config.CLOUD:
        # 云端版不往服务器磁盘写：数据库才是唯一的存放处，网页也从数据库读。
        # 写了等于把所有人的日报散落在服务器上，多一份要管的副本。
        return {}
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
