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
from typing import Any, Dict, List, Optional

from . import config, db, llm, personas, pto, store


def _cjk_len(text: str) -> int:
    return len(re.sub(r"[\s#*`>_\-\[\]()]", "", text or ""))


def speaking_seconds(text: str) -> int:
    return int(round(_cjk_len(text) / 3.6))


def fingerprint(tasks: List[Dict[str, Any]], persona: Dict[str, Any], pto_status: str) -> str:
    blob = json.dumps(
        {
            "tasks": [[t["task_id"], [u["update_id"] for u in t["updates"]]] for t in tasks],
            "persona": personas.fingerprint(persona),
            "pto": pto_status,
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
        body = "\n".join("  - %s" % u["content_md"].strip().replace("\n", " ")
                         for u in t["updates"])
        out.append(head + "\n" + body)
    return "\n\n".join(out)


def _daily_prompt(author, date, tasks, persona) -> List[dict]:
    sys = (
        "你是 %s 的日志助手。把当天推进的几个任务整理成一份本人风格的工作日志。\n"
        "硬规则：\n"
        "- **一个任务写一条**，不要把同一个任务的多条进展拆成多行。\n"
        "- 只用给到的进展事实，不许推断、不许补充没写的进展、不许夸大。\n"
        "- 有 issue 号的，在该条行尾用 (AI-1234) 标注；没有 issue 的不要编。\n"
        "- Todo 只写进展里明确提到还没做完的事；没有就写「无」。\n"
        "- 直接输出 Markdown 正文，不要解释，不要代码块包裹。\n"
        "风格要求：%s"
    ) % (persona.get("display_name") or author, persona.get("daily_style", ""))
    user = (
        "日期：%s\n作者：%s\n\n格式骨架（照这个结构，内容按实际写）：\n%s\n\n"
        "今天推进了 %d 个任务：\n\n%s"
    ) % (
        date, persona.get("display_name") or author,
        persona.get("daily_template", "").format(
            date=date, display_name=persona.get("display_name") or author),
        len(tasks), _tasks_block(tasks),
    )
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


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
    name = persona.get("display_name") or author
    lines = ["# %s 工作日志 · %s" % (date, name), "", "## Done", ""]
    for t in tasks:
        first = t["updates"][0]["content_md"].strip().splitlines()[0]
        more = "（另有 %d 条进展）" % (len(t["updates"]) - 1) if len(t["updates"]) > 1 else ""
        tag = " (%s)" % t["issue_key"] if t["issue_key"] else ""
        lines.append("- **%s**%s：%s%s" % (t["title"], tag, first, more))
    lines += ["", "## 记录", "", "- 本篇为兜底稿（LLM 不可用），内容取自进展原文，未经整理。", ""]
    return "\n".join(lines)


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

def generate(author: str, date: str, force: bool = False,
             persona_name: Optional[str] = None) -> Dict[str, Any]:
    from . import auth

    ident = auth.all_authors().get(author, {})
    persona = personas.load(persona_name or ident.get("persona") or author)
    if not persona.get("display_name"):
        persona["display_name"] = ident.get("display_name") or author

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
    model = config.LLM_MODEL

    # 日报和口播稿各自独立降级：一个挂了不该把另一个也拖成兜底稿。
    try:
        daily = llm.chat(_daily_prompt(author, date, tasks, persona), max_tokens=4000)
        daily = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", daily.strip())
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
                warnings.append("重试后 %d 字，短于目标下限，按原样输出" % n)
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
             model if daily_gen == "llm" else None, n_updates)
    _persist(author, date, "voice", voice, fp, voice_gen,
             model if voice_gen == "llm" else None, n_updates)
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


def _persist(author, date, kind, content, fp, generator, model, n) -> None:
    prev = db.get_report(author, date, kind)
    with db.cursor() as conn:
        if prev:
            conn.execute(
                "INSERT INTO report_history (author,date,kind,content_md,generator,created_at)"
                " VALUES (?,?,?,?,?,?)",
                (author, date, kind, prev["content_md"], prev["generator"], prev["created_at"]))
        conn.execute(
            "INSERT INTO reports (report_id,author,date,kind,content_md,fingerprint,"
            "generator,model,entry_count,char_count,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(author,date,kind) DO UPDATE SET content_md=excluded.content_md,"
            " fingerprint=excluded.fingerprint, generator=excluded.generator,"
            " model=excluded.model, entry_count=excluded.entry_count,"
            " char_count=excluded.char_count, created_at=excluded.created_at",
            (str(uuid.uuid4()), author, date, kind, content, fp, generator, model,
             n, _cjk_len(content), store.now_iso()))


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
