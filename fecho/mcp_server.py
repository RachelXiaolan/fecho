"""Fecho MCP server（stdio）—— agent 的入口。

单进程：直接读写本地 SQLite，不需要先起任何服务。
自己实现最小 JSON-RPC，不引第三方 MCP SDK。
stdout 只允许出现 JSON-RPC，日志一律走 stderr。

会话 id：每个 agent 会话各自拉起一个本进程，所以进程实例天然等于一次会话。
它不参与分组（任务才是主干），只做审计和配对时的上下文先验。
"""
import json
import os
import sys
import uuid
from typing import Any, Dict, Optional

from . import __version__, config, db, service

SESSION_ID = os.getenv("FECHO_SESSION_ID") or str(uuid.uuid4())
PROTOCOL = "2024-11-05"
_client_name = "unknown-agent"


def log(msg: str) -> None:
    sys.stderr.write("[fecho] %s\n" % msg)
    sys.stderr.flush()


TOOLS = [
    {
        "name": "log_progress",
        "description": (
            "记一条工作进展。**推进完一件事就调一次**，不要攒到下班再回忆。\n"
            "记的是「我们做成了什么、进展到哪」，不是对话内容，也不是用户说过的话——"
            "一句能让人看懂结果的话就够。\n"
            "系统会自己把它归到对应的任务上：正文里写了 issue 号就直接归过去，没写就拿内容"
            "和你名下在办的 Mobius issue 配对，配不上就归到同一个对话里刚才那个任务，"
            "再不行才新立一个自由任务。所以你不用操心它属于哪儿；"
            "但如果返回值说这条是猜的、而你知道它其实是别的任务，带 issue 或 task_id 重记一次。"
        ),
        "inputSchema": {"type": "object", "properties": {
            "content": {"type": "string", "description": "做成了什么、进展到哪"},
            "issue": {"type": "string", "description": "可选。确定是哪个 issue 就直接写，如 AI-2541"},
            "task_id": {"type": "string", "description": "可选。强制挂到某个已有任务"},
            "date": {"type": "string", "description": "可选，YYYY-MM-DD，补记往日时用"},
            "agent": {"type": "string", "description": "可选，覆盖来源 agent 名"}},
            "required": ["content"]},
    },
    {
        "name": "catch_up",
        "description": ("开工时调一次：拉回昨天的日报、口播稿，和现在还挂着的任务。"
                        "适合放在会话开场自动执行。"),
        "inputSchema": {"type": "object", "properties": {
            "date": {"type": "string", "description": "看哪天的稿，默认昨天"}}},
    },
    {
        "name": "my_tasks",
        "description": "我现在有哪些任务在推进，各自最近一条进展是什么。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_my_log",
        "description": "我今天推进了哪些任务、推到哪了，以及已生成的日报/口播稿（若有）。",
        "inputSchema": {"type": "object", "properties": {"date": {"type": "string"}}},
    },
    {
        "name": "end_of_day",
        "description": ("收工：把今天的进展按任务整理成日报和口播稿（调 LLM）。"
                        "输入没变时不会重复生成。"),
        "inputSchema": {"type": "object", "properties": {
            "date": {"type": "string"}, "force": {"type": "boolean"}}},
    },
    {
        "name": "mobius_login",
        "description": ("连接 Mobius。会打开浏览器让用户授权（OAuth），授权完自动接上，"
                        "不需要用户手动贴任何 key。装好 fecho 后第一件事就调它。"),
        "inputSchema": {"type": "object", "properties": {
            "assignee": {"type": "string",
                         "description": "用户在 Mobius 上的邮箱，用来确定拉谁的 issue"}}},
    },
    {
        "name": "sync_issues",
        "description": "从 Mobius 拉一次在办 issue，刷新配对用的本地缓存。",
        "inputSchema": {"type": "object", "properties": {"assignee": {"type": "string"}}},
    },
    {
        "name": "fecho_doctor",
        "description": ("自检：什么配好了、什么还缺、缺的怎么补。装完先调这个，"
                        "然后按它给的 fix 一步步做完。"),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "team_digest",
        "description": (
            "看团队某天各自推送过来的日报（需要团队配好共享 collector）。"
            "只有整理好的成品，看不到任何人的原始进展——collector 那边物理上就不存这个。"
        ),
        "inputSchema": {"type": "object", "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD，默认今天"},
            "author": {"type": "string", "description": "可选，只看某人"}}},
    },
]

_METHOD_LABEL = {
    "explicit": "你点名了 issue",
    "mobius-auto": "自动配到 Mobius issue",
    "task-continue": "接着已有任务",
    "new-task": "新立了自由任务",
}


def _task_line(t: Dict[str, Any]) -> str:
    return "%s %s" % (t["issue_key"], t["title"]) if t.get("issue_key") else t["title"]


def _fmt_day(data: Dict[str, Any], who: str) -> str:
    out = ["%s %s：%d 个任务 / %d 条进展" % (data["date"], who, data["task_count"],
                                            data["update_count"])]
    for t in data["tasks"]:
        out.append("\n## %s" % _task_line(t))
        for u in t["updates"]:
            out.append("- [%s] %s" % (u["source_agent"], u["content_md"].splitlines()[0]))
    return "\n".join(out)


def _fmt_report(r: Dict[str, Any]) -> str:
    out = ["已整理 %s：%d 个任务 / %d 条进展（%s%s）" % (
        r["date"], r["task_count"], r["update_count"], r["generator"],
        "/" + r["model"] if r.get("model") else "")]
    out.append("口播稿 %d 字，约 %d 秒" % (r["voice_chars"], r["voice_seconds_est"]))
    for w in r.get("warnings", []):
        out.append("! " + w)
    out += ["", "=== 日报 ===", r["daily_md"], "", "=== 口播稿 ===", r["voice_md"],
            "", "文件：", "\n".join("  " + p for p in r["files"].values())]
    tp = r.get("team_push")
    if tp:
        out.append("团队：已推送到 collector" if tp.get("pushed") else "团队：未推送（%s）" % tp.get("reason"))
    return "\n".join(out)


def call_tool(name: str, args: Dict[str, Any]) -> str:
    if name == "log_progress":
        res = service.record(
            args["content"], date=args.get("date"),
            source_agent=args.get("agent") or _client_name, session_id=SESSION_ID,
            issue=args.get("issue"), task_id=args.get("task_id"))
        t, m = res["task"], res["match"]
        head = ("重复，未写入 → %s" if res["verdict"] == "duplicate" else "已记录 → **%s**") \
            % _task_line(t)
        if m.get("via") == "same-session":
            why = "没有明确线索，按同一对话里刚才那个任务归的"
        elif m.get("score") and m["method"] not in ("explicit", "new-task"):
            why = "%s（相似度 %.2f）" % (_METHOD_LABEL.get(m["method"], m["method"]), m["score"])
        else:
            why = _METHOD_LABEL.get(m["method"], m["method"])
        lines = [head, "配对方式：%s" % why]
        if m.get("confidence") == "low":
            lines.append('⚠️ 这条是猜的。不是同一件事的话，带 issue="AI-xxxx" 或 task_id 重记一次。')
        lines.append("今天已推进 %d 个任务。" % res["today_task_count"])
        return "\n".join(lines)

    if name == "catch_up":
        c = service.catch_up(args.get("date"))
        out = []
        rep = c["report"]
        if rep.get("daily"):
            out += ["=== %s 的日报 ===" % c["date"], rep["daily"]["content_md"]]
            if rep.get("voice"):
                out += ["", "=== 口播稿 ===", rep["voice"]["content_md"]]
        else:
            out.append("（%s 没有日报）" % c["date"])
        out += ["", "=== 还挂着的任务（%d 个）===" % len(c["open_tasks"])]
        for t in c["open_tasks"]:
            out.append("- %s" % _task_line(t))
            if t.get("last_progress"):
                out.append("    最近：%s" % t["last_progress"].splitlines()[0])
        return "\n".join(out)

    if name == "my_tasks":
        tasks = service.open_tasks()
        if not tasks:
            return "当前没有在推进的任务。"
        out = ["在推进的任务（%d 个）：" % len(tasks)]
        for t in tasks:
            out.append("- %s（%d 条进展）" % (_task_line(t), t["update_count"]))
            if t.get("last_progress"):
                out.append("    最近：%s" % t["last_progress"].splitlines()[0])
        return "\n".join(out)

    if name == "get_my_log":
        data = service.day(service.whoami(), args.get("date"))
        out = [_fmt_day(data, "我")]
        rep = service.report(data["date"])
        if rep.get("daily"):
            out += ["", "--- 已生成日报 ---", rep["daily"]["content_md"]]
            if rep.get("voice"):
                out += ["", "--- 口播稿 ---", rep["voice"]["content_md"]]
        else:
            out += ["", "（尚未生成日报，可调用 end_of_day）"]
        return "\n".join(out)

    if name == "end_of_day":
        r = service.end_of_day(args.get("date"), force=bool(args.get("force")))
        if r["status"] != "generated":
            return "状态：%s（%s）" % (r["status"], r.get("reason", ""))
        return _fmt_report(r)

    if name == "mobius_login":
        from . import oauth

        url = config.MOBIUS_URL or "https://mobius.feedmob.com/api/mcp"
        if args.get("assignee"):
            config.update(mobius_assignee=args["assignee"])
        config.update(mobius_url=url)
        config.reload_module()
        started = oauth.login(url, open_browser=True, timeout=180)
        out = ["已在浏览器里打开 Mobius 授权页，请让用户点「同意」。",
               "如果没自动打开，把这个链接给用户：", started["authorize_url"], "", "等待授权…"]
        try:
            res = oauth.complete(started["ctx"])
        except oauth.OAuthError as exc:
            return "\n".join(out + ["", "❌ 授权失败：%s" % exc,
                                    "可以重试，或改用 fecho login --token <token>"])
        try:
            s = service.sync_issues()
            out.append("✅ 已连接 Mobius，并同步了 %d 个在办 issue（%s）。" % (s["count"], s["assignee"]))
        except Exception as exc:
            out.append("✅ 已连接 Mobius，但同步 issue 失败：%s" % exc)
            out.append("（如果是不知道拉谁的 issue，再调一次 mobius_login 并带上 assignee 邮箱）")
        out.append("授权有效期约 %d 分钟，过期会自动续。" %
                   max(1, (res.get("expires_at", 0) - int(__import__("time").time())) // 60))
        return "\n".join(out)

    if name == "sync_issues":
        s = service.sync_issues(args.get("assignee"))
        return "已从 Mobius 同步 %d 个在办 issue（%s），配对缓存已刷新。" % (s["count"], s["assignee"])

    if name == "fecho_doctor":
        d = service.doctor()
        out = ["Fecho %s · 作者=%s · 数据在 %s" % (
            __version__, d["config"]["author"], d["config"]["home"])]
        for c in d["checks"]:
            out.append("%s %s — %s" % ("✓" if c["ok"] else "✗", c["name"], c["detail"]))
            if c.get("fix"):
                out.append("    → %s" % c["fix"])
        out += ["", "现在能做：记进展 ✓｜自动配 Mobius %s｜出日报 %s｜团队协作 %s" % (
            "✓" if d["ready_to_match"] else "✗", "✓" if d["ready_to_report"] else "✗",
            "✓" if d["ready_for_team"] else "✗（可选）")]
        return "\n".join(out)

    if name == "team_digest":
        from . import push

        try:
            d = service.team_digest(args.get("date"), args.get("author"))
        except push.PushError as exc:
            return "连不上团队 collector：%s" % exc
        if not d["count"]:
            return "%s 团队还没有人推送过报告。" % d["date"]
        out = ["%s 团队日报（%d 人）：" % (d["date"], d["count"])]
        for a in d["authors"]:
            out.append("\n## %s" % a["author"])
            if a.get("daily"):
                out.append(a["daily"]["content_md"])
        return "\n".join(out)

    raise RuntimeError("未知工具: %s" % name)


def handle(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    global _client_name
    method, mid = msg.get("method"), msg.get("id")

    if method == "initialize":
        info = (msg.get("params") or {}).get("clientInfo") or {}
        _client_name = info.get("name") or _client_name
        log("client=%s session=%s home=%s" % (_client_name, SESSION_ID[:8], config.HOME))
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": (msg.get("params") or {}).get("protocolVersion", PROTOCOL),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fecho", "version": __version__}}}

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        try:
            text = call_tool(params.get("name"), params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": text}], "isError": False}}
        except Exception as exc:
            log("tool error: %s" % exc)
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "调用失败：%s" % exc}], "isError": True}}

    if mid is None:
        return None
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": "Method not found: %s" % method}}


def main() -> None:
    db.init()
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log("started pid=%d session=%s" % (os.getpid(), SESSION_ID[:8]))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
